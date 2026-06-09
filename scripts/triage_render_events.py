"""Operator-facing CLI for ``disclosure_render_events`` (U21 — surfaces the
persistence shipped in U16 / migration 042).

Background
----------
U3 (commit ``924d799``) caught ``APRDisclosureError`` in
``api/routes/disclosures.py`` and surfaced an in-memory
``disclosure_status="needs_review"`` payload but deferred persistence.
U16 (migration 042 + ``compliance/render_events.py``) closed that gap.
This script is the operator-side complement: read the queue from the
command line.

Read-only — no ``decide`` subcommand
------------------------------------
Render events are diagnostic only. They do NOT have a triage state
machine the way ``scoring_shadow_disagreements`` (R1.6) does. The
operator does not "resolve" a row; once the deal's APR calculator
converges next time, a new ``ok`` event is recorded alongside the
failing one. The reference R1.6 CLI (`scripts/triage_disagreement.py`)
ships ``list / show / decide / summary``; this one ships only
``list / show / summary`` because the equivalent of decide makes no
sense for render events.

Sub-commands
------------
* ``list [--status <name>] [--limit N]``
  Print recent render events one per line. ``--status`` narrows to a
  single bucket (``needs_review`` / ``apr_compute_failed`` / ``ok`` /
  ``template_render_failed``); default lists every status. ``--limit``
  defaults to 25.

* ``show --id <UUID>``
  Pretty-print every column on a single row including the ``details``
  and ``metadata`` JSONB. Read-only.

* ``summary``
  Aggregate counts by status across the most recent window.

Common flags
------------
* ``--target {dev|staging|prod}`` (default ``dev``) — mirrors
  ``scripts/triage_disagreement.py`` and ``scripts/apply_migrations.py``.
  Validates that ``MIGRATIONS_DB_URL_<TARGET>`` is populated in
  ``.env``/``.env.local`` before touching Supabase.

Operating-principle compliance
------------------------------
* Per principle #3 (never echo credentials), this script resolves the
  DSN/secret env vars internally; it never prints their values. A
  ``--target`` mismatch prints only the env var NAME, never any partial
  value.
* No production writes — no confirmation prompts needed.

Usage
-----
    uv run python scripts/triage_render_events.py list
    uv run python scripts/triage_render_events.py list \\
        --status needs_review --limit 10
    uv run python scripts/triage_render_events.py show \\
        --id 9b8e1c2d-…
    uv run python scripts/triage_render_events.py summary
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, TextIO
from uuid import UUID

REPO_ROOT = Path(__file__).resolve().parent.parent

# Operator-side tooling — bypass the prod-boot data-residency guard the
# app modules pull in via ``aegis.config``. This script never invokes
# Bedrock; it only reads the render-event table. Same pattern as
# ``scripts/triage_disagreement.py``.
os.environ.setdefault("AEGIS_DATA_RESIDENCY_CONFIRMED", "true")

if TYPE_CHECKING:
    from aegis.compliance.render_events import (
        DisclosureRenderEventRecord,
        DisclosureRenderEventRepository,
    )

# DSN env-var convention mirrors scripts/apply_migrations.py.
_DSN_ENV_BY_TARGET: dict[str, str] = {
    "dev": "MIGRATIONS_DB_URL_DEV",
    "staging": "MIGRATIONS_DB_URL_STAGING",
    "prod": "MIGRATIONS_DB_URL_PROD",
}

# Default page size for ``list``. Operator queues are typically <100.
_DEFAULT_LIMIT: int = 25

# Default window for ``list`` / ``summary`` — 14 days. Mirrors the
# /ui/disclosure-events route's default.
_DEFAULT_WINDOW_DAYS: int = 14

# Status-reason truncation for the per-line ``list`` output.
_REASON_WIDTH: int = 60


# ---------------------------------------------------------------------
# Env loading + config resolution (mirrors triage_disagreement.py)
# ---------------------------------------------------------------------


def _load_dotenv_local() -> None:
    """Load ``.env`` and ``.env.local`` without overwriting existing keys."""
    for path in (REPO_ROOT / ".env", REPO_ROOT / ".env.local"):
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def _validate_target_dsn(target: str) -> None:
    """Confirm the per-target DSN env var is populated.

    Per operating-principle #3, this function prints only the env var
    NAME on failure, never any partial value.
    """
    env_var = _DSN_ENV_BY_TARGET.get(target)
    if env_var is None:
        raise SystemExit(
            f"unknown target {target!r}; expected dev|staging|prod"
        )
    if not os.environ.get(env_var, "").strip():
        raise SystemExit(
            f"{env_var} is not set. Add it to .env.local (the runner uses "
            "it to confirm the target environment matches the Supabase "
            "client's SUPABASE_URL)."
        )


# ---------------------------------------------------------------------
# Repository acquisition
# ---------------------------------------------------------------------


def _build_default_repo() -> DisclosureRenderEventRepository:
    """Construct the live Supabase-backed repository.

    Imported inside the function so unit tests that inject a fake repo
    via ``main(argv, repo=...)`` don't trigger the Supabase client's
    startup config validation.
    """
    from aegis.compliance.render_events import (
        SupabaseDisclosureRenderEventRepository,
    )

    return SupabaseDisclosureRenderEventRepository()


# ---------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------


def _short_uuid(value: UUID | None) -> str:
    """First 8 chars of a UUID, or ``—`` for ``None``."""
    if value is None:
        return "—"
    return str(value).split("-", 1)[0]


def _truncate_reason(value: str | None, width: int = _REASON_WIDTH) -> str:
    if value is None or not value.strip():
        return "—"
    cleaned = value.strip()
    if len(cleaned) > width:
        return cleaned[: width - 1] + "…"
    return cleaned


def _format_list_row(record: DisclosureRenderEventRecord) -> str:
    """One-line operator-friendly row for ``list``.

    Format::

        {rendered_at}  {status:22s}  {state:2s}  deal={deal_8}
        merchant={merchant_8}  {reason}
    """
    ts = record.rendered_at.strftime("%Y-%m-%d %H:%M")
    state = record.state or "—"
    return (
        f"{ts}  {record.status:<22s}  {state:<2s}  "
        f"deal={_short_uuid(record.deal_id)}  "
        f"merchant={_short_uuid(record.merchant_id)}  "
        f"{_truncate_reason(record.status_reason)}"
    )


def _format_show(record: DisclosureRenderEventRecord) -> str:
    """Verbose pretty-print of every column on one row."""
    lines = [
        f"id              : {record.id}",
        f"deal_id         : {record.deal_id or '—'}",
        f"merchant_id     : {record.merchant_id or '—'}",
        f"state           : {record.state or '—'}",
        f"template_path   : {record.template_path or '—'}",
        f"status          : {record.status}",
        f"status_reason   : {record.status_reason or '—'}",
        f"recipient_email : {record.recipient_email or '—'}",
        f"rendered_at     : {record.rendered_at.isoformat()}",
        f"rendered_by     : {record.rendered_by or '—'}",
        "",
        "DETAILS:",
        f"{json.dumps(record.details, indent=2, default=str)}",
        "",
        "METADATA:",
        f"{json.dumps(record.metadata, indent=2, default=str)}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------


def cmd_list(
    args: argparse.Namespace,
    repo: DisclosureRenderEventRepository,
    out: TextIO,
) -> int:
    """List render-event rows in the recent window.

    Default: every status, last 14 days. ``--status`` narrows to one
    bucket; ``--limit`` caps the count.
    """
    from datetime import UTC, datetime, timedelta

    today = datetime.now(UTC).date()
    from_date = today - timedelta(days=_DEFAULT_WINDOW_DAYS)
    rows = repo.list_in_window(
        from_date=from_date,
        to_date=today,
        status=args.status,
        limit=args.limit if args.limit is not None else _DEFAULT_LIMIT,
    )

    if not rows:
        out.write("(no matching rows)\n")
        return 0

    for row in rows:
        out.write(_format_list_row(row) + "\n")
    return 0


def cmd_show(
    args: argparse.Namespace,
    repo: DisclosureRenderEventRepository,
    out: TextIO,
) -> int:
    """Print every column on the row identified by ``--id``."""
    record = repo.get(args.id)
    if record is None:
        out.write(f"no row with id={args.id}\n")
        return 1
    out.write(_format_show(record) + "\n")
    return 0


def cmd_summary(
    args: argparse.Namespace,
    repo: DisclosureRenderEventRepository,
    out: TextIO,
) -> int:
    """Aggregate counts by status across the recent window.

    Columns: ``status | count``. The actionable buckets
    (``needs_review`` + ``apr_compute_failed``) are echoed as the
    operator's triage backlog at the bottom.
    """
    del args  # No flags on ``summary``.

    from datetime import UTC, datetime, timedelta

    today = datetime.now(UTC).date()
    from_date = today - timedelta(days=_DEFAULT_WINDOW_DAYS)
    all_rows = repo.list_in_window(
        from_date=from_date,
        to_date=today,
        status=None,
        limit=10_000,
    )
    counts = _summary_counts(all_rows)

    statuses = [
        "ok",
        "needs_review",
        "apr_compute_failed",
        "template_render_failed",
    ]
    header = f"{'status':<24s}  {'count':>6s}"
    out.write(header + "\n")
    out.write("-" * len(header) + "\n")
    for s in statuses:
        out.write(f"{s:<24s}  {counts.get(s, 0):>6d}\n")
    # Any status outside the known set still surfaces (operator may
    # have added a new one mid-investigation; never silently swallow).
    extras = sorted(set(counts) - set(statuses))
    for s in extras:
        out.write(f"{s:<24s}  {counts[s]:>6d}\n")

    actionable = counts.get("needs_review", 0) + counts.get(
        "apr_compute_failed", 0
    )
    out.write("\n")
    out.write(
        f"triage backlog: {actionable} actionable "
        "(needs_review + apr_compute_failed) "
        f"in the last {_DEFAULT_WINDOW_DAYS} days.\n"
    )
    return 0


# ---------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------


def _summary_counts(
    rows: list[DisclosureRenderEventRecord],
) -> dict[str, int]:
    """Bucket every row by ``status``."""
    out: dict[str, int] = {}
    for row in rows:
        out[row.status] = out.get(row.status, 0) + 1
    return out


# ---------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Operator CLI for disclosure_render_events (U21 / U16). "
            "Sub-commands: list, show, summary. Read-only — render "
            "events have no triage state machine."
        ),
    )
    parser.add_argument(
        "--target",
        choices=("dev", "staging", "prod"),
        default="dev",
        help=(
            "Which environment's DSN to validate before connecting. "
            "Default: dev. (The Supabase client itself reads "
            "SUPABASE_URL / SUPABASE_SERVICE_KEY; --target enforces the "
            "operator-side intent matches the available DSN env var.)"
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # list
    p_list = sub.add_parser(
        "list",
        help="List render-event rows in the recent window.",
    )
    p_list.add_argument(
        "--status",
        type=str,
        default=None,
        choices=(
            "ok",
            "needs_review",
            "apr_compute_failed",
            "template_render_failed",
        ),
        help="Narrow to a single status bucket.",
    )
    p_list.add_argument(
        "--limit",
        type=int,
        default=_DEFAULT_LIMIT,
        help=(
            f"Truncate to N rows (default {_DEFAULT_LIMIT}). "
            "Pass 0 for unlimited."
        ),
    )

    # show
    p_show = sub.add_parser(
        "show",
        help="Pretty-print every column on one row.",
    )
    p_show.add_argument("--id", type=UUID, required=True, help="Row UUID.")

    # summary
    sub.add_parser(
        "summary",
        help="Aggregate counts by status across the recent window.",
    )

    return parser


# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------


def main(
    argv: list[str] | None = None,
    *,
    repo: DisclosureRenderEventRepository | None = None,
    out: TextIO | None = None,
) -> int:
    """CLI entry point.

    ``repo``, ``out`` are injection seams for the tests in
    ``tests/scripts/test_triage_render_events_cli.py``. In production
    callers should leave them ``None`` so the script wires the live
    Supabase repository and stdout.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    write_out: TextIO = out if out is not None else sys.stdout

    # Limit==0 means "no truncation" (operator convention).
    if getattr(args, "limit", None) == 0:
        args.limit = None

    # Env + target validation only when we'll actually hit a live DB.
    # Test path injects a repo, so skip the env dance.
    if repo is None:
        _load_dotenv_local()
        _validate_target_dsn(args.target)
        repo = _build_default_repo()

    if args.cmd == "list":
        return cmd_list(args, repo, write_out)
    if args.cmd == "show":
        return cmd_show(args, repo, write_out)
    if args.cmd == "summary":
        return cmd_summary(args, repo, write_out)

    parser.print_help()
    return 2


# Public surface for tests.
__all__ = [
    "_DEFAULT_LIMIT",
    "_DEFAULT_WINDOW_DAYS",
    "_format_list_row",
    "_format_show",
    "_summary_counts",
    "cmd_list",
    "cmd_show",
    "cmd_summary",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
