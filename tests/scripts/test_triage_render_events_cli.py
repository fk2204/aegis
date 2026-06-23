"""CLI tests for ``scripts/triage_render_events.py`` (U21).

Covers:

  * ``list`` — default lists everything in the window; ``--status`` filter
    narrows; empty repo prints the friendly empty message.
  * ``show`` — every column rendered; missing id returns rc=1.
  * ``summary`` — per-status counts + actionable-backlog tally.

The CLI is exercised through ``main(argv, repo=fake, out=buf)`` so no
Supabase client is ever instantiated. Mirrors the pattern in
``tests/scoring_v2/test_triage_disagreement_cli.py``.
"""

from __future__ import annotations

import argparse
import io
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

# The CLI lives in ``scripts/`` which is not a package; load via its
# absolute path so pytest can resolve the import regardless of cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from aegis.compliance.render_events import (  # noqa: E402
    RENDER_EVENT_STATUS_APR_FAILED,
    RENDER_EVENT_STATUS_NEEDS_REVIEW,
    RENDER_EVENT_STATUS_OK,
    DisclosureRenderEventRecord,
    InMemoryDisclosureRenderEventRepository,
)
from scripts import triage_render_events as cli  # noqa: E402

# Pin one day before today (UTC) so the rendered_at timestamp always
# lands inside the CLI's _DEFAULT_WINDOW_DAYS-day rolling window. Tests
# that pass an explicit window use _FIXED_TS.date() as both endpoints,
# so a moving date doesn't break them either.
_FIXED_TS = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(
    days=1
)


def _make_repo() -> InMemoryDisclosureRenderEventRepository:
    """Repo populated with one row per actionable status + an ok row.

    Four rows total: needs_review, apr_compute_failed, apr_compute_failed,
    ok. The two apr_compute_failed rows let summary's actionable
    backlog tally show a non-trivial sum.
    """
    repo = InMemoryDisclosureRenderEventRepository()

    def _make(status: str, reason: str | None) -> DisclosureRenderEventRecord:
        return repo.record(
            deal_id=uuid4(),
            merchant_id=uuid4(),
            state="CA",
            template_path="compliance/templates/ca_sb1235.html.j2",
            status=status,
            status_reason=reason,
            details={"term_days": 180, "factor": "1.35"},
            recipient_email=None,
            rendered_by="api",
            rendered_at=_FIXED_TS,
            metadata={"render_mode": "preview"},
        )

    _make(RENDER_EVENT_STATUS_NEEDS_REVIEW, "deal not yet APR-converged")
    _make(RENDER_EVENT_STATUS_APR_FAILED, "brentq failed to converge")
    _make(RENDER_EVENT_STATUS_APR_FAILED, "discount rate out of bracket")
    _make(RENDER_EVENT_STATUS_OK, None)
    return repo


def _ns(**overrides: object) -> argparse.Namespace:
    """Build a defaults-applied argparse.Namespace for direct cmd_* calls."""
    base: dict[str, object] = {
        "cmd": None,
        "target": "dev",
        "status": None,
        "limit": cli._DEFAULT_LIMIT,
        "id": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------
# list
# ---------------------------------------------------------------------


def test_list_returns_every_row_in_window_by_default() -> None:
    """Default ``list`` (no ``--status``) returns every status — 4 rows."""
    repo = _make_repo()
    buf = io.StringIO()
    rc = cli.cmd_list(_ns(), repo, buf)
    assert rc == 0

    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    assert len(lines) == 4
    # Status text appears in each row.
    joined = "\n".join(lines)
    assert "needs_review" in joined
    assert "apr_compute_failed" in joined
    assert "ok" in joined


def test_list_filters_by_status() -> None:
    """``--status apr_compute_failed`` returns only that bucket."""
    repo = _make_repo()
    buf = io.StringIO()
    rc = cli.cmd_list(_ns(status=RENDER_EVENT_STATUS_APR_FAILED), repo, buf)
    assert rc == 0

    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    assert len(lines) == 2
    for line in lines:
        assert "apr_compute_failed" in line


def test_list_truncates_to_limit() -> None:
    """``--limit 1`` cuts the output to a single line."""
    repo = _make_repo()
    buf = io.StringIO()
    rc = cli.cmd_list(_ns(limit=1), repo, buf)
    assert rc == 0

    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1


def test_list_empty_repo_prints_friendly_message() -> None:
    """An empty repo prints the friendly empty-state line, not a header."""
    repo = InMemoryDisclosureRenderEventRepository()
    buf = io.StringIO()
    rc = cli.cmd_list(_ns(), repo, buf)
    assert rc == 0
    assert "no matching rows" in buf.getvalue()


# ---------------------------------------------------------------------
# show
# ---------------------------------------------------------------------


def test_show_prints_every_required_column() -> None:
    """``show`` renders every column label so the operator can diagnose."""
    repo = _make_repo()
    rows = repo.list_in_window(
        from_date=_FIXED_TS.date(),
        to_date=_FIXED_TS.date(),
    )
    assert rows, "fixture must produce at least one row"
    target = rows[0]

    buf = io.StringIO()
    rc = cli.cmd_show(_ns(id=target.id), repo, buf)
    assert rc == 0

    output = buf.getvalue()
    for needle in (
        "id",
        "deal_id",
        "merchant_id",
        "state",
        "template_path",
        "status",
        "status_reason",
        "rendered_at",
        "rendered_by",
        "DETAILS",
        "METADATA",
    ):
        assert needle in output, f"missing {needle!r} in show output"


def test_show_returns_1_when_id_not_found() -> None:
    """``show`` returns rc=1 + a friendly message on an unknown UUID."""
    repo = _make_repo()
    buf = io.StringIO()
    missing = UUID("99999999-9999-4999-8999-999999999999")
    rc = cli.cmd_show(_ns(id=missing), repo, buf)
    assert rc == 1
    assert "no row with id=" in buf.getvalue()


# ---------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------


def test_summary_counts_each_status_bucket() -> None:
    """summary prints per-status counts AND the actionable-backlog total.

    Fixture: 1 needs_review + 2 apr_compute_failed + 1 ok → backlog = 3.
    """
    repo = _make_repo()
    buf = io.StringIO()
    rc = cli.cmd_summary(_ns(), repo, buf)
    assert rc == 0

    output = buf.getvalue()
    # Per-status table.
    assert "needs_review" in output
    assert "apr_compute_failed" in output
    assert "ok" in output
    # Counts: 1 needs_review, 2 apr_compute_failed, 1 ok.
    # The exact substring "apr_compute_failed       2" is fragile across
    # padding, so just assert the actionable tally line.
    assert "triage backlog: 3 actionable" in output


def test_summary_counts_helper_is_correct() -> None:
    """``_summary_counts`` returns the per-status tally directly."""
    repo = _make_repo()
    all_rows = repo.list_in_window(
        from_date=_FIXED_TS.date(),
        to_date=_FIXED_TS.date(),
        limit=10_000,
    )
    counts = cli._summary_counts(all_rows)
    assert counts.get(RENDER_EVENT_STATUS_NEEDS_REVIEW) == 1
    assert counts.get(RENDER_EVENT_STATUS_APR_FAILED) == 2
    assert counts.get(RENDER_EVENT_STATUS_OK) == 1


# ---------------------------------------------------------------------
# main() entry point — injection path bypasses Supabase entirely.
# ---------------------------------------------------------------------


def test_main_dispatches_list_subcommand_via_injection() -> None:
    """``main(argv, repo=fake, out=buf)`` dispatches without DSN env."""
    repo = _make_repo()
    buf = io.StringIO()
    rc = cli.main(["list", "--status", "apr_compute_failed"], repo=repo, out=buf)
    assert rc == 0
    out = buf.getvalue()
    assert "apr_compute_failed" in out
    assert "needs_review" not in out


def test_main_dispatches_summary_subcommand_via_injection() -> None:
    """``main(["summary"], repo=fake, out=buf)`` returns rc=0 + tally."""
    repo = _make_repo()
    buf = io.StringIO()
    rc = cli.main(["summary"], repo=repo, out=buf)
    assert rc == 0
    assert "triage backlog" in buf.getvalue()
