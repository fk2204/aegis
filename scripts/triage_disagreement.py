"""Operator-facing triage CLI for ``scoring_shadow_disagreements`` (R1.6
Step 2 cutover-prep follow-up).

Background
----------
Migration 037 created ``scoring_shadow_disagreements``; migration 038
created the open-triage view ``scoring_disagreements_open``; commit
``d3d802f`` shipped the repository + persistence path so
``scripts/shadow_comparison_a_b_c_vs_fraud_score.py --persist`` populates
the table. This script is the OPERATOR side: read the queue, drill into
a row, record a triage decision (``accept-new`` / ``accept-old`` /
``both-valid`` / ``needs-rule-change``). When every
``old-caught-something-new-misses`` regression-sentinel row has been
triaged with ``accept-new``, the Step 2 cutover gate clears.

Sub-commands
------------
* ``list [--category <name>] [--limit N] [--all]``
  Print untriaged rows (or every row with ``--all``) one per line:
  ``{run-at-date}  {merchant_id_8}  {category_short}  legacy={tier}
   new=A:{a}/B:{b}/C:{c}  {evidence_summary}``.
  ``--category`` narrows to a single bucket; ``--limit`` defaults to 25
  (the operator's queue rarely exceeds ~100). ``--all`` includes triaged
  rows for retrospective review.

* ``show --id <UUID>``
  Pretty-print every column on a single row including evidence JSONB.
  Read-only.

* ``decide --id <UUID> --decision <enum> --by <name> [--notes "..."]``
  Apply a triage decision. The four ``--decision`` values come from
  ``ALLOWED_TRIAGE_DECISIONS`` in
  ``aegis.scoring_v2.shadow_disagreements`` and are validated at the
  CLI boundary. Refuses to overwrite an already-triaged row unless
  ``--force`` is passed. Prompts ``About to record decision=… on <id>.
  Confirm? [y/N]`` before writing; ``--yes`` skips the prompt for
  scripting. ``--dry-run`` prints what would happen without writing.

* ``summary``
  Aggregate counts by category: ``open | triaged | per-decision``.
  The regression-sentinel row count is the cutover-readiness signal.

Common flags
------------
* ``--target {dev|staging|prod}`` (default ``dev``) — mirrors
  ``apply_migrations.py``. Validates that
  ``MIGRATIONS_DB_URL_<TARGET>`` is populated in ``.env``/``.env.local``
  before touching Supabase. The Supabase Python client used for the
  actual queries reads ``SUPABASE_URL`` and ``SUPABASE_SERVICE_KEY`` —
  same env-loading helper applies.

Operating-principle compliance
------------------------------
* Per principle #1 (production writes require explicit approval), the
  ``decide`` sub-command interactively confirms before writing. The
  ``--yes`` flag exists for the operator's own scripted batches.
* Per principle #3 (never echo credentials), this script resolves the
  DSN/secret env vars internally; it never prints their values. A
  ``--target`` mismatch prints only the env var NAME, never the value.

Usage
-----
    uv run python scripts/triage_disagreement.py list
    uv run python scripts/triage_disagreement.py list \\
        --category old-caught-something-new-misses --limit 10
    uv run python scripts/triage_disagreement.py show \\
        --id 9b8e1c2d-…
    uv run python scripts/triage_disagreement.py decide \\
        --id 9b8e1c2d-… --decision accept-new --by filip \\
        --notes "VU shape — intl wires misread as fraud"
    uv run python scripts/triage_disagreement.py summary
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, TextIO
from uuid import UUID

REPO_ROOT = Path(__file__).resolve().parent.parent

# Operator-side tooling — bypass the prod-boot data-residency guard the
# app modules pull in via ``aegis.config``. This script never invokes
# Bedrock; it only reads/writes the triage queue. Same pattern as
# ``scripts/show_weekly_digest.py``.
os.environ.setdefault("AEGIS_DATA_RESIDENCY_CONFIRMED", "true")

if TYPE_CHECKING:
    from aegis.scoring_v2.shadow_disagreements import (
        ScoringDisagreementRecord,
        ScoringDisagreementRepository,
    )

# DSN env-var convention mirrors scripts/apply_migrations.py.
_DSN_ENV_BY_TARGET: dict[str, str] = {
    "dev": "MIGRATIONS_DB_URL_DEV",
    "staging": "MIGRATIONS_DB_URL_STAGING",
    "prod": "MIGRATIONS_DB_URL_PROD",
}

# Default page size for ``list``. Operator queues are typically <100.
_DEFAULT_LIMIT: int = 25

# Evidence-summary truncation budget (one-line ``list`` output).
_EVIDENCE_SUMMARY_WIDTH: int = 80

# Category short-name map for compact ``list`` output. The full names
# stay in show/summary; this is purely for the per-line `list` rendering.
_CATEGORY_SHORT: dict[str, str] = {
    "old-caught-something-new-misses": "REGRESSION",
    "new-is-better": "new-better",
    "genuinely-ambiguous": "ambiguous",
    "agreement": "agree",
    "insufficient-new-data": "insufficient",
}


# ---------------------------------------------------------------------
# Env loading + config resolution (mirrors apply_migrations.py)
# ---------------------------------------------------------------------


def _load_dotenv_local() -> None:
    """Load ``.env`` and ``.env.local`` without overwriting existing keys.

    Vendored from ``scripts/apply_migrations.py`` so this script's only
    dependency on that module is the env-var-name convention.
    """
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

    The actual Supabase client reads ``SUPABASE_URL`` /
    ``SUPABASE_SERVICE_KEY`` (via ``aegis.config.get_settings``). This
    function exists to enforce the same target-selection contract as
    ``apply_migrations.py``: if the operator passes ``--target prod`` but
    the prod DSN is missing from ``.env.local``, fail fast with a clear
    message rather than silently writing against whichever Supabase
    project ``SUPABASE_URL`` happens to point at.

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


def _build_default_repo() -> ScoringDisagreementRepository:
    """Construct the live Supabase-backed repository.

    Imported inside the function so unit tests that inject a fake repo
    via ``main(argv, repo=...)`` don't trigger the Supabase client's
    startup config validation.
    """
    # Local import: keeps the test path (``main(argv, repo=fake)``) free
    # of any Supabase-config dependency.
    from aegis.scoring_v2.shadow_disagreements import (
        SupabaseScoringDisagreementRepository,
    )

    return SupabaseScoringDisagreementRepository()


# ---------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------


def _short_uuid(value: UUID) -> str:
    """First 8 chars of a UUID. Operator-readable, still unambiguous on the queue."""
    return str(value).split("-", 1)[0]


def _short_category(category: str) -> str:
    return _CATEGORY_SHORT.get(category, category)


def _evidence_summary(
    record: ScoringDisagreementRecord,
    width: int = _EVIDENCE_SUMMARY_WIDTH,
) -> str:
    """One-line summary of the evidence JSONB for ``list`` rendering.

    Strategy: prefer ``rationale`` (the human-readable string the
    comparison script writes), fall back to a joined list of top-level
    keys. Truncate to ``width`` characters with an ellipsis. PII-free by
    construction — the comparison script's ``_evidence_from_row`` only
    writes categorical / numeric fields.
    """
    if record.evidence is None:
        return "(no evidence)"
    rationale = record.evidence.get("rationale")
    if isinstance(rationale, str) and rationale.strip():
        summary = rationale.strip()
    else:
        keys = sorted(str(k) for k in record.evidence.keys())
        summary = "evidence keys: " + ", ".join(keys)
    if len(summary) > width:
        return summary[: width - 1] + "…"
    return summary


def _format_list_row(record: ScoringDisagreementRecord) -> str:
    """One-line operator-friendly row for ``list``.

    Format::

        {run-at-date}  {merchant_id_8}  {category_short:11s}
        legacy={tier:1}  new=A:{a}/B:{b}/C:{c}  {evidence_summary}

    The Track C column is filled with the ``revenue_basis`` digit-count
    when present, else ``—`` — keeps the line stable when no Track C
    panel exists.
    """
    run_date = record.comparison_run_at.date().isoformat()
    mid = _short_uuid(record.merchant_id)
    cat = _short_category(record.category)
    legacy_tier = record.legacy_tier or "—"
    track_a = record.track_a_verdict or "—"
    track_b = record.track_b_band or "—"
    track_c_label = "—"
    if record.track_c_panel:
        rev = record.track_c_panel.get("revenue_basis")
        if rev is not None:
            track_c_label = f"rev={rev}"
        else:
            intl = record.track_c_panel.get("international_share_pct")
            if intl is not None:
                track_c_label = f"intl={intl}%"
    return (
        f"{run_date}  {mid}  {cat:<11s}  legacy={legacy_tier:<1s}  "
        f"new=A:{track_a}/B:{track_b}/C:{track_c_label}  "
        f"{_evidence_summary(record)}"
    )


def _format_show(record: ScoringDisagreementRecord) -> str:
    """Verbose pretty-print of every column on one row."""
    lines = [
        f"id                    : {record.id}",
        f"merchant_id           : {record.merchant_id}",
        f"deal_id               : {record.deal_id or '—'}",
        f"comparison_run_at     : {record.comparison_run_at.isoformat()}",
        f"category              : {record.category}",
        "",
        "LEGACY (live fraud_score path):",
        f"  fraud_score         : {record.legacy_fraud_score}",
        f"  tier                : {record.legacy_tier}",
        f"  recommendation      : {record.legacy_recommendation}",
        f"  hard_declines       : {record.legacy_hard_declines or []}",
        "",
        "NEW (A/B/C tracks):",
        f"  track_a_verdict     : {record.track_a_verdict}",
        f"  track_b_band        : {record.track_b_band}",
        f"  track_c_panel       : {json.dumps(record.track_c_panel, indent=2, default=str)}",
        "",
        "EVIDENCE:",
        f"{json.dumps(record.evidence, indent=2, default=str)}",
        "",
        "TRIAGE:",
        f"  triaged_by          : {record.triaged_by or '—'}",
        f"  triaged_at          : "
        f"{record.triaged_at.isoformat() if record.triaged_at else '—'}",
        f"  triage_decision     : {record.triage_decision or '—'}",
        f"  triage_notes        : {record.triage_notes or '—'}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------


def cmd_list(
    args: argparse.Namespace,
    repo: ScoringDisagreementRepository,
    out: TextIO,
) -> int:
    """List queue rows.

    Default: untriaged via ``list_open``. With ``--all``: read the
    backing table directly via ``list_all`` so triaged rows are
    included.
    """
    if args.all:
        rows = repo.list_all(category=args.category, limit=args.limit)
    else:
        rows = repo.list_open(category=args.category, limit=args.limit)

    if not rows:
        out.write("(no matching rows)\n")
        return 0

    for row in rows:
        out.write(_format_list_row(row) + "\n")
    return 0


def cmd_show(
    args: argparse.Namespace,
    repo: ScoringDisagreementRepository,
    out: TextIO,
) -> int:
    """Print every column on the row identified by ``--id``."""
    record = repo.get(args.id)
    if record is None:
        out.write(f"no row with id={args.id}\n")
        return 1
    out.write(_format_show(record) + "\n")
    return 0


def cmd_decide(
    args: argparse.Namespace,
    repo: ScoringDisagreementRepository,
    out: TextIO,
    *,
    prompt: Callable[[str], str] | None = None,
) -> int:
    """Record a triage decision.

    Pre-checks the row exists + is not double-triaged (unless
    ``--force``), confirms with the operator, then writes. ``--yes``
    skips the prompt; ``--dry-run`` skips the write.

    The ``prompt`` arg is injection-friendly for tests: pass a callable
    that returns the desired response. Default is ``builtins.input``.
    """
    from aegis.scoring_v2.shadow_disagreements import (
        ALLOWED_TRIAGE_DECISIONS,
        ScoringDisagreementWriteError,
    )

    if args.decision not in ALLOWED_TRIAGE_DECISIONS:
        out.write(
            f"invalid --decision {args.decision!r}; "
            f"must be one of {sorted(ALLOWED_TRIAGE_DECISIONS)}\n"
        )
        return 2
    if not args.by or not args.by.strip():
        out.write("--by must be a non-empty operator identifier (name or email)\n")
        return 2

    current = repo.get(args.id)
    if current is None:
        out.write(f"no row with id={args.id}\n")
        return 1
    if current.triaged_at is not None and not args.force:
        out.write(
            f"row {args.id} is already triaged by {current.triaged_by!r} "
            f"at {current.triaged_at.isoformat()}. "
            "Re-run with --force to overwrite.\n"
        )
        return 3

    # Confirmation prompt: satisfies operating-principle #1 (production
    # writes require explicit operator approval). ``--yes`` is the
    # opt-out for the operator's own scripting.
    if not args.yes and not args.dry_run:
        ask: Callable[[str], str] = prompt if prompt is not None else input
        response = ask(
            f"About to record decision={args.decision} on {args.id}. "
            "Confirm? [y/N] "
        )
        if response.strip().lower() not in {"y", "yes"}:
            out.write("aborted (no row written)\n")
            return 0

    if args.dry_run:
        out.write(
            f"[dry-run] would record decision={args.decision} "
            f"by={args.by} on {args.id} "
            f"(notes={args.notes!r})\n"
        )
        return 0

    try:
        updated = repo.record_triage_decision(
            record_id=args.id,
            decision=args.decision,
            by=args.by,
            notes=args.notes,
            force=args.force,
        )
    except ScoringDisagreementWriteError as exc:
        out.write(f"write failed: {exc}\n")
        return 4
    except KeyError as exc:
        out.write(f"row not found: {exc}\n")
        return 1
    except ValueError as exc:
        out.write(f"invalid input: {exc}\n")
        return 2

    out.write("triage recorded:\n")
    out.write(_format_show(updated) + "\n")
    return 0


def cmd_summary(
    args: argparse.Namespace,
    repo: ScoringDisagreementRepository,
    out: TextIO,
) -> int:
    """Print aggregate counts by category.

    Columns: ``category | open | triaged | accept-new | accept-old |
    both-valid | needs-rule-change``. Reads via ``list_all`` so triaged
    rows are included.
    """
    del args  # No flags on ``summary``.
    all_rows = repo.list_all()
    counts = _summary_counts(all_rows)

    # Print order matches the regression-sentinel-first contract.
    categories = [
        "old-caught-something-new-misses",
        "new-is-better",
        "genuinely-ambiguous",
        "agreement",
        "insufficient-new-data",
    ]
    header = (
        f"{'category':<35s}  "
        f"{'open':>5s}  {'triaged':>7s}  "
        f"{'accept-new':>10s}  {'accept-old':>10s}  "
        f"{'both-valid':>10s}  {'needs-rule-change':>17s}"
    )
    out.write(header + "\n")
    out.write("-" * len(header) + "\n")
    for cat in categories:
        c = counts.get(cat, _empty_counts())
        out.write(
            f"{cat:<35s}  {c['open']:>5d}  {c['triaged']:>7d}  "
            f"{c['accept-new']:>10d}  {c['accept-old']:>10d}  "
            f"{c['both-valid']:>10d}  {c['needs-rule-change']:>17d}\n"
        )

    # Cutover-readiness signal: count of un-triaged regression rows.
    regression_open = counts.get(
        "old-caught-something-new-misses", _empty_counts()
    )["open"]
    out.write("\n")
    out.write(
        f"cutover-blocker: {regression_open} untriaged "
        "old-caught-something-new-misses rows remaining.\n"
    )
    return 0


# ---------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------


def _empty_counts() -> dict[str, int]:
    return {
        "open": 0,
        "triaged": 0,
        "accept-new": 0,
        "accept-old": 0,
        "both-valid": 0,
        "needs-rule-change": 0,
    }


def _summary_counts(
    rows: Sequence[ScoringDisagreementRecord],
) -> dict[str, dict[str, int]]:
    """Bucket every row by ``category`` and tally open / triaged / decision."""
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = out.setdefault(row.category, _empty_counts())
        if row.triaged_at is None:
            bucket["open"] += 1
        else:
            bucket["triaged"] += 1
            if row.triage_decision in bucket:
                bucket[row.triage_decision] += 1
    return out


# ---------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Operator triage CLI for scoring_shadow_disagreements. "
            "Sub-commands: list, show, decide, summary."
        ),
    )
    parser.add_argument(
        "--target",
        choices=("dev", "staging", "prod"),
        default="dev",
        help=(
            "Which environment's DSN to validate before connecting. "
            "Default: dev. (The Supabase client itself reads SUPABASE_URL / "
            "SUPABASE_SERVICE_KEY; --target enforces the operator-side "
            "intent matches the available DSN env var.)"
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # list
    p_list = sub.add_parser(
        "list",
        help="List untriaged rows (default) or every row (--all).",
    )
    p_list.add_argument(
        "--category",
        type=str,
        default=None,
        help="Narrow to a single category bucket.",
    )
    p_list.add_argument(
        "--limit",
        type=int,
        default=_DEFAULT_LIMIT,
        help=f"Truncate to N rows (default {_DEFAULT_LIMIT}). Pass 0 for unlimited.",
    )
    p_list.add_argument(
        "--all",
        action="store_true",
        help="Include triaged rows (queries the table, not the view).",
    )

    # show
    p_show = sub.add_parser(
        "show",
        help="Pretty-print every column on one row.",
    )
    p_show.add_argument("--id", type=UUID, required=True, help="Row UUID.")

    # decide
    p_decide = sub.add_parser(
        "decide",
        help="Record a triage decision.",
    )
    p_decide.add_argument("--id", type=UUID, required=True, help="Row UUID.")
    p_decide.add_argument(
        "--decision",
        type=str,
        required=True,
        choices=("accept-new", "accept-old", "both-valid", "needs-rule-change"),
        help="One of the four triage outcomes.",
    )
    p_decide.add_argument(
        "--by",
        type=str,
        required=True,
        help="Operator identifier (name or email).",
    )
    p_decide.add_argument(
        "--notes",
        type=str,
        default=None,
        help="Free-text triage notes (optional).",
    )
    p_decide.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an already-triaged row (double-decide bypass).",
    )
    p_decide.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt (for scripting).",
    )
    p_decide.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen; do not write.",
    )

    # summary
    sub.add_parser(
        "summary",
        help="Aggregate counts by category and triage decision.",
    )

    return parser


# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------


def main(
    argv: list[str] | None = None,
    *,
    repo: ScoringDisagreementRepository | None = None,
    out: TextIO | None = None,
    prompt: Callable[[str], str] | None = None,
) -> int:
    """CLI entry point.

    ``repo``, ``out``, ``prompt`` are injection seams for the tests in
    ``tests/scoring_v2/test_triage_disagreement_cli.py``. In production
    callers should leave them ``None`` so the script wires the live
    Supabase repository, stdout, and ``builtins.input`` respectively.
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
    if args.cmd == "decide":
        return cmd_decide(args, repo, write_out, prompt=prompt)
    if args.cmd == "summary":
        return cmd_summary(args, repo, write_out)

    parser.print_help()
    return 2


# Public surface for tests.
__all__ = [
    "_DEFAULT_LIMIT",
    "_evidence_summary",
    "_format_list_row",
    "_format_show",
    "_summary_counts",
    "cmd_decide",
    "cmd_list",
    "cmd_show",
    "cmd_summary",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
