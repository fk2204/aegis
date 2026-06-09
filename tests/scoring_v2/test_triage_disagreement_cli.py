"""CLI tests for ``scripts/triage_disagreement.py`` (R1.6 Step 2
cutover-prep follow-up).

Covers:
  * ``list`` — filtering by ``--category``, default vs ``--all``, ordering
  * ``show`` — every column rendered
  * ``decide`` — happy path with ``--yes``, interactive-prompt accept,
    interactive-prompt reject, double-decide refusal without ``--force``,
    ``--force`` override, ``--dry-run`` no-write, KeyError on missing id,
    ValueError on bad decision
  * ``summary`` — open + triaged + per-decision tallies

The CLI is exercised through ``main(argv, repo=fake, out=buf,
prompt=fake_prompt)`` so no Supabase client is ever instantiated.
"""

from __future__ import annotations

import io
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

# The CLI lives in ``scripts/`` which is not a package; load via its
# absolute path so pytest can resolve the import regardless of cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import triage_disagreement as cli  # noqa: E402

from aegis.scoring_v2.shadow_disagreements import (  # noqa: E402
    CATEGORY_AGREEMENT,
    CATEGORY_AMBIGUOUS,
    CATEGORY_INSUFFICIENT,
    CATEGORY_NEW_BETTER,
    CATEGORY_OLD_BETTER,
    InMemoryScoringDisagreementRepository,
    ScoringDisagreementRecord,
    ScoringDisagreementWriteError,
    record_disagreement,
)

_FIXED_TS = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)


def _make_repo() -> InMemoryScoringDisagreementRepository:
    """Build a repo populated with one row per relevant category.

    The four rows exercise the bucket-ordering contract from migration
    038 (regression sentinel first, then new-better, ambiguous,
    agreement). The fifth (insufficient-new-data) is included to make
    the summary aggregation realistic.
    """
    repo = InMemoryScoringDisagreementRepository()

    def _make(
        category: str,
        rationale: str,
        *,
        run_at: datetime,
    ) -> ScoringDisagreementRecord:
        is_old = category == CATEGORY_OLD_BETTER
        return record_disagreement(
            repo,
            merchant_id=uuid4(),
            deal_id=None,
            category=category,
            legacy_fraud_score=70,
            legacy_tier="F" if is_old else "C",
            legacy_recommendation="decline" if is_old else "approve",
            legacy_hard_declines=["fraud_score_critical"] if is_old else None,
            track_a_verdict="clean",
            track_b_band="low",
            track_c_panel={
                "revenue_basis": "120000.00",
                "international_share_pct": 5.0,
            },
            evidence={
                "rationale": rationale,
                "live_hard_reasons": ["fraud_score_critical"] if is_old else [],
                "track_b_factors": [],
            },
            comparison_run_at=run_at,
        )

    _make(
        CATEGORY_OLD_BETTER,
        "REGRESSION: live declined but new tracks clean",
        run_at=_FIXED_TS,
    )
    _make(
        CATEGORY_NEW_BETTER,
        "new Track A FAIL caught integrity issue live missed",
        run_at=_FIXED_TS,
    )
    _make(
        CATEGORY_AMBIGUOUS,
        "live approve + new A=review; operator judgment",
        run_at=_FIXED_TS,
    )
    _make(
        CATEGORY_AGREEMENT,
        "live approve + new tracks clean — both green-light",
        run_at=_FIXED_TS,
    )
    _make(
        CATEGORY_INSUFFICIENT,
        "no live + no new signals",
        run_at=_FIXED_TS - timedelta(days=1),
    )
    return repo


def _ns(**overrides: Any) -> Any:
    """Build a defaults-applied argparse.Namespace for direct cmd_* calls."""
    import argparse

    base: dict[str, Any] = {
        "cmd": None,
        "target": "dev",
        "category": None,
        "limit": cli._DEFAULT_LIMIT,
        "all": False,
        "id": None,
        "decision": None,
        "by": None,
        "notes": None,
        "force": False,
        "yes": False,
        "dry_run": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------
# list
# ---------------------------------------------------------------------


def test_list_returns_all_open_rows_ordered_regression_first() -> None:
    repo = _make_repo()
    buf = io.StringIO()
    rc = cli.cmd_list(_ns(), repo, buf)
    assert rc == 0

    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    # Five rows in the repo; all open (none triaged).
    assert len(lines) == 5
    # First line is the regression sentinel (REGRESSION short name).
    assert "REGRESSION" in lines[0]
    # Insufficient (oldest) is last after the within-bucket date sort.
    assert "insufficient" in lines[-1]


def test_list_filters_by_category() -> None:
    repo = _make_repo()
    buf = io.StringIO()
    rc = cli.cmd_list(_ns(category=CATEGORY_OLD_BETTER), repo, buf)
    assert rc == 0

    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1
    assert "REGRESSION" in lines[0]


def test_list_truncates_to_limit() -> None:
    repo = _make_repo()
    buf = io.StringIO()
    rc = cli.cmd_list(_ns(limit=2), repo, buf)
    assert rc == 0

    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    assert len(lines) == 2


def test_list_all_includes_triaged_rows() -> None:
    repo = _make_repo()
    # Triage the first open row.
    first = repo.list_open()[0]
    repo.record_triage_decision(
        record_id=first.id,
        decision="accept-new",
        by="filip",
    )

    # Default list (open-only) should drop it.
    buf_open = io.StringIO()
    cli.cmd_list(_ns(), repo, buf_open)
    open_lines = [line for line in buf_open.getvalue().splitlines() if line.strip()]
    assert len(open_lines) == 4

    # --all should bring it back.
    buf_all = io.StringIO()
    cli.cmd_list(_ns(all=True), repo, buf_all)
    all_lines = [line for line in buf_all.getvalue().splitlines() if line.strip()]
    assert len(all_lines) == 5


def test_list_empty_repo_prints_friendly_message() -> None:
    repo = InMemoryScoringDisagreementRepository()
    buf = io.StringIO()
    rc = cli.cmd_list(_ns(), repo, buf)
    assert rc == 0
    assert "no matching rows" in buf.getvalue()


# ---------------------------------------------------------------------
# show
# ---------------------------------------------------------------------


def test_show_prints_every_required_column() -> None:
    repo = _make_repo()
    record = repo.list_open()[0]
    buf = io.StringIO()
    rc = cli.cmd_show(_ns(id=record.id), repo, buf)
    assert rc == 0

    output = buf.getvalue()
    # Every label we render in cmd_show.
    for needle in (
        "id",
        "merchant_id",
        "deal_id",
        "comparison_run_at",
        "category",
        "LEGACY",
        "NEW",
        "EVIDENCE",
        "TRIAGE",
        "fraud_score",
        "tier",
        "track_a_verdict",
        "track_b_band",
        "track_c_panel",
        "triaged_by",
    ):
        assert needle in output, f"missing {needle!r} in show output"


def test_show_returns_1_when_id_not_found() -> None:
    repo = _make_repo()
    buf = io.StringIO()
    missing = UUID("99999999-9999-4999-8999-999999999999")
    rc = cli.cmd_show(_ns(id=missing), repo, buf)
    assert rc == 1
    assert "no row with id=" in buf.getvalue()


# ---------------------------------------------------------------------
# decide
# ---------------------------------------------------------------------


def test_decide_happy_path_with_yes_flag() -> None:
    repo = _make_repo()
    record = repo.list_open()[0]
    buf = io.StringIO()
    rc = cli.cmd_decide(
        _ns(id=record.id, decision="accept-new", by="filip", yes=True),
        repo,
        buf,
    )
    assert rc == 0
    assert "triage recorded" in buf.getvalue()

    # The row must now be marked triaged in the repo.
    updated = repo.get(record.id)
    assert updated is not None
    assert updated.triaged_at is not None
    assert updated.triaged_by == "filip"
    assert updated.triage_decision == "accept-new"


def test_decide_with_interactive_prompt_accept() -> None:
    repo = _make_repo()
    record = repo.list_open()[0]
    buf = io.StringIO()
    rc = cli.cmd_decide(
        _ns(id=record.id, decision="accept-new", by="filip"),
        repo,
        buf,
        prompt=lambda _msg: "y",
    )
    assert rc == 0
    updated = repo.get(record.id)
    assert updated is not None
    assert updated.triaged_at is not None


def test_decide_with_interactive_prompt_reject() -> None:
    repo = _make_repo()
    record = repo.list_open()[0]
    buf = io.StringIO()
    rc = cli.cmd_decide(
        _ns(id=record.id, decision="accept-new", by="filip"),
        repo,
        buf,
        prompt=lambda _msg: "",
    )
    assert rc == 0
    assert "aborted" in buf.getvalue()
    # Row must remain untriaged.
    untouched = repo.get(record.id)
    assert untouched is not None
    assert untouched.triaged_at is None


def test_decide_double_triage_refused_without_force() -> None:
    repo = _make_repo()
    record = repo.list_open()[0]
    repo.record_triage_decision(
        record_id=record.id,
        decision="accept-new",
        by="filip",
    )
    buf = io.StringIO()
    rc = cli.cmd_decide(
        _ns(id=record.id, decision="accept-old", by="filip", yes=True),
        repo,
        buf,
    )
    assert rc == 3
    assert "already triaged" in buf.getvalue()
    # First decision must still be in place — no overwrite.
    untouched = repo.get(record.id)
    assert untouched is not None
    assert untouched.triage_decision == "accept-new"


def test_decide_double_triage_allowed_with_force() -> None:
    repo = _make_repo()
    record = repo.list_open()[0]
    repo.record_triage_decision(
        record_id=record.id,
        decision="accept-new",
        by="filip",
    )
    buf = io.StringIO()
    rc = cli.cmd_decide(
        _ns(
            id=record.id,
            decision="accept-old",
            by="filip",
            yes=True,
            force=True,
        ),
        repo,
        buf,
    )
    assert rc == 0
    overwritten = repo.get(record.id)
    assert overwritten is not None
    assert overwritten.triage_decision == "accept-old"


def test_decide_dry_run_does_not_write() -> None:
    repo = _make_repo()
    record = repo.list_open()[0]
    buf = io.StringIO()
    rc = cli.cmd_decide(
        _ns(id=record.id, decision="accept-new", by="filip", dry_run=True),
        repo,
        buf,
    )
    assert rc == 0
    assert "[dry-run]" in buf.getvalue()
    untouched = repo.get(record.id)
    assert untouched is not None
    assert untouched.triaged_at is None


def test_decide_returns_1_when_id_missing() -> None:
    repo = _make_repo()
    buf = io.StringIO()
    rc = cli.cmd_decide(
        _ns(
            id=UUID("99999999-9999-4999-8999-999999999999"),
            decision="accept-new",
            by="filip",
            yes=True,
        ),
        repo,
        buf,
    )
    assert rc == 1


def test_decide_rejects_invalid_decision_string() -> None:
    repo = _make_repo()
    record = repo.list_open()[0]
    buf = io.StringIO()
    rc = cli.cmd_decide(
        _ns(id=record.id, decision="not-a-valid-decision", by="filip", yes=True),
        repo,
        buf,
    )
    assert rc == 2
    assert "invalid --decision" in buf.getvalue()


def test_decide_rejects_blank_by() -> None:
    repo = _make_repo()
    record = repo.list_open()[0]
    buf = io.StringIO()
    rc = cli.cmd_decide(
        _ns(id=record.id, decision="accept-new", by="   ", yes=True),
        repo,
        buf,
    )
    assert rc == 2
    assert "--by must be" in buf.getvalue()


def test_decide_surfaces_write_failure() -> None:
    """A repo that raises ``ScoringDisagreementWriteError`` returns 4."""

    class FailingRepo(InMemoryScoringDisagreementRepository):
        def record_triage_decision(self, **kwargs: Any) -> ScoringDisagreementRecord:
            raise ScoringDisagreementWriteError("simulated write failure")

    repo = FailingRepo()
    record = record_disagreement(
        repo,
        merchant_id=uuid4(),
        deal_id=None,
        category=CATEGORY_OLD_BETTER,
        legacy_fraud_score=70,
        legacy_tier="F",
        legacy_recommendation="decline",
        legacy_hard_declines=["fraud_score_critical"],
        track_a_verdict="clean",
        track_b_band="low",
        track_c_panel=None,
        evidence={"rationale": "test"},
        comparison_run_at=_FIXED_TS,
    )
    buf = io.StringIO()
    rc = cli.cmd_decide(
        _ns(id=record.id, decision="accept-new", by="filip", yes=True),
        repo,
        buf,
    )
    assert rc == 4
    assert "write failed" in buf.getvalue()


# ---------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------


def test_summary_counts_open_and_triaged() -> None:
    repo = _make_repo()
    # Triage one row of each of two categories.
    open_old = next(
        r for r in repo.list_open() if r.category == CATEGORY_OLD_BETTER
    )
    open_ambig = next(
        r for r in repo.list_open() if r.category == CATEGORY_AMBIGUOUS
    )
    repo.record_triage_decision(
        record_id=open_old.id,
        decision="accept-new",
        by="filip",
    )
    repo.record_triage_decision(
        record_id=open_ambig.id,
        decision="both-valid",
        by="filip",
    )

    buf = io.StringIO()
    rc = cli.cmd_summary(_ns(), repo, buf)
    assert rc == 0

    output = buf.getvalue()
    # The triaged old-better row leaves zero open in that category.
    assert "old-caught-something-new-misses" in output
    # cutover-blocker line should call out 0 regression rows remaining.
    assert "cutover-blocker: 0 untriaged" in output
    # Header columns present.
    for col in ("open", "triaged", "accept-new", "accept-old", "both-valid", "needs-rule-change"):
        assert col in output


def test_summary_internal_counts_helper_buckets_correctly() -> None:
    """Exercise ``_summary_counts`` directly so the per-decision tally is pinned."""
    repo = _make_repo()
    r_old = next(r for r in repo.list_open() if r.category == CATEGORY_OLD_BETTER)
    r_new = next(r for r in repo.list_open() if r.category == CATEGORY_NEW_BETTER)
    repo.record_triage_decision(record_id=r_old.id, decision="accept-new", by="op")
    repo.record_triage_decision(record_id=r_new.id, decision="accept-old", by="op")

    counts = cli._summary_counts(repo.list_all())

    old_bucket = counts[CATEGORY_OLD_BETTER]
    assert old_bucket["open"] == 0
    assert old_bucket["triaged"] == 1
    assert old_bucket["accept-new"] == 1
    assert old_bucket["accept-old"] == 0

    new_bucket = counts[CATEGORY_NEW_BETTER]
    assert new_bucket["open"] == 0
    assert new_bucket["triaged"] == 1
    assert new_bucket["accept-old"] == 1


# ---------------------------------------------------------------------
# main(argv, repo=...) dispatch
# ---------------------------------------------------------------------


def test_main_dispatches_list_via_argv() -> None:
    repo = _make_repo()
    buf = io.StringIO()
    rc = cli.main(["list"], repo=repo, out=buf)
    assert rc == 0
    assert "REGRESSION" in buf.getvalue()


def test_main_dispatches_decide_via_argv() -> None:
    repo = _make_repo()
    record = repo.list_open()[0]
    buf = io.StringIO()
    rc = cli.main(
        [
            "decide",
            "--id",
            str(record.id),
            "--decision",
            "accept-new",
            "--by",
            "filip",
            "--yes",
        ],
        repo=repo,
        out=buf,
    )
    assert rc == 0
    updated = repo.get(record.id)
    assert updated is not None
    assert updated.triage_decision == "accept-new"


def test_main_dispatches_summary_via_argv() -> None:
    repo = _make_repo()
    buf = io.StringIO()
    rc = cli.main(["summary"], repo=repo, out=buf)
    assert rc == 0
    assert "cutover-blocker" in buf.getvalue()


def test_main_dispatches_show_via_argv() -> None:
    repo = _make_repo()
    record = repo.list_open()[0]
    buf = io.StringIO()
    rc = cli.main(
        ["show", "--id", str(record.id)],
        repo=repo,
        out=buf,
    )
    assert rc == 0
    assert "EVIDENCE" in buf.getvalue()


def test_main_with_limit_zero_means_unlimited() -> None:
    """--limit 0 is the operator's 'no truncation' opt-out."""
    repo = _make_repo()
    buf = io.StringIO()
    rc = cli.main(["list", "--limit", "0"], repo=repo, out=buf)
    assert rc == 0
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    assert len(lines) == 5  # all five rows present


# ---------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------


def test_format_list_row_includes_short_uuid_and_category() -> None:
    repo = _make_repo()
    rows = repo.list_open()
    line = cli._format_list_row(rows[0])
    short_mid = str(rows[0].merchant_id).split("-", 1)[0]
    assert short_mid in line
    # Regression rows render with the loud short name.
    assert "REGRESSION" in line


def test_evidence_summary_truncates_long_rationale() -> None:
    repo = _make_repo()
    record = repo.list_open()[0]
    long_evidence = {"rationale": "x" * 500}
    record_with_long = record.model_copy(update={"evidence": long_evidence})
    summary = cli._evidence_summary(record_with_long, width=80)
    assert len(summary) <= 80
    assert summary.endswith("…")


def test_evidence_summary_handles_missing_evidence() -> None:
    repo = _make_repo()
    record = repo.list_open()[0]
    record_no_evidence = record.model_copy(update={"evidence": None})
    summary = cli._evidence_summary(record_no_evidence)
    assert "no evidence" in summary


def test_evidence_summary_falls_back_to_keys_when_no_rationale() -> None:
    repo = _make_repo()
    record = repo.list_open()[0]
    record_no_rationale = record.model_copy(
        update={"evidence": {"factors": ["intl"], "share_pct": 50}}
    )
    summary = cli._evidence_summary(record_no_rationale)
    assert "evidence keys" in summary
    assert "factors" in summary
    assert "share_pct" in summary


# ---------------------------------------------------------------------
# Defensive: repository contract surface
# ---------------------------------------------------------------------


def test_in_memory_repo_has_required_methods() -> None:
    """The CLI relies on these methods; pin the contract."""
    repo = InMemoryScoringDisagreementRepository()
    assert callable(repo.list_open)
    assert callable(repo.list_all)
    assert callable(repo.get)
    assert callable(repo.record_triage_decision)


def test_record_triage_decision_validates_decision_enum() -> None:
    repo = _make_repo()
    record = repo.list_open()[0]
    with pytest.raises(ValueError):
        repo.record_triage_decision(
            record_id=record.id,
            decision="not-a-valid-value",
            by="filip",
        )


def test_record_triage_decision_validates_by_not_blank() -> None:
    repo = _make_repo()
    record = repo.list_open()[0]
    with pytest.raises(ValueError):
        repo.record_triage_decision(
            record_id=record.id,
            decision="accept-new",
            by="   ",
        )


def test_record_triage_decision_raises_keyerror_for_unknown_id() -> None:
    repo = _make_repo()
    with pytest.raises(KeyError):
        repo.record_triage_decision(
            record_id=UUID("99999999-9999-4999-8999-999999999999"),
            decision="accept-new",
            by="filip",
        )
