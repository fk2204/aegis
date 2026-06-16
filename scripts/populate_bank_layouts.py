"""Seed the ``bank_layouts`` table (migration 059) with every bank
AEGIS has already successfully parsed.

Use case: ``bank_layouts`` is the layout-learning surface — for each
bank, a ``successful_parses`` counter, ``last_seen`` timestamp, JSONB
``layout_fingerprint`` accumulated across parses, and operator-authored
``extraction_hints``. The pipeline auto-grows this table on every
successful parse via
``SupabaseBankLayoutRepository.upsert_success(bank_name, fingerprint)``
(see ``parser/pipeline.py`` lines 392-405). What it does NOT do is
backfill: any bank parsed before migration 059 landed (or before
``upsert_success`` started firing) has no row, so its hints are never
used.

This script walks every document at ``parse_status in (proceed, review)``,
groups them by ``bank_name``, computes ``(count, max(uploaded_at))`` per
bank, then UPSERTs into ``bank_layouts``. Hints + fingerprint stay
intact — we only touch ``successful_parses`` and ``last_seen``, and
only when the historical values exceed what's already in the row (so
the script is safe to re-run, never reduces values, and never races
with a live ``upsert_success`` to a lower count).

Per CLAUDE.md operating-principles §1 the script is DRY-RUN by default.
Add ``--apply`` to persist.

CSV-like summary to stdout (one row per bank), full counts to stderr.

Exit codes (mirror ``scripts/track_a_historical_lookback.py``):

  * ``0`` — every bank seeded / refreshed cleanly.
  * ``1`` — runtime error (Supabase init failed, settings missing).
  * ``3`` — at least one bank failed to upsert (write error per row).

Usage (on the prod box, with ``/etc/aegis/aegis.env`` sourced)::

    set -a; source /etc/aegis/aegis.env; set +a
    cd /opt/aegis

    # dry-run: tally per bank, report, never write
    .venv/bin/python scripts/populate_bank_layouts.py

    # apply: upsert each bank, never reduce existing values
    .venv/bin/python scripts/populate_bank_layouts.py --apply
"""

from __future__ import annotations

import argparse
import csv
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, cast

from aegis.db import get_supabase

# Exit codes — keep aligned with sibling scripts.
EXIT_OK: Final[int] = 0
EXIT_RUNTIME_ERROR: Final[int] = 1
EXIT_ISSUES_FOUND: Final[int] = 3

# Statuses that count as "successfully parsed" for layout-learning.
# Matches the parser-side gate in ``parser/pipeline.py``: ``upsert_success``
# fires for ``proceed`` and ``review`` (both have reliable extractions);
# ``manual_review`` did not (classification confidence too low) so it does
# NOT contribute a fingerprint.
_PARSE_OK_STATUSES: Final[tuple[str, ...]] = ("proceed", "review")

# Cap on rows scanned from ``documents``. 10 000 = ~28 years at 30
# statements/month — defensive against a runaway scan but never expected
# to bite in practice.
_DOCUMENTS_SCAN_CAP: Final[int] = 10000


# ─────────────────────────────────────────────────────────────────────
# Pure-data row shapes
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BankAggregate:
    """One bank's historical totals derived from documents table.

    ``last_seen`` is an ISO-8601 timestamp string so the upsert payload
    can hand it to PostgREST verbatim.
    """

    bank_name: str
    parse_count: int
    last_seen: str


@dataclass(frozen=True)
class UpsertResult:
    """Outcome of one upsert attempt — one row per bank in the summary."""

    bank_name: str
    parse_count: int
    last_seen: str
    action: str  # "insert" | "update" | "skip_no_op" | "error"
    detail: str  # explanation when action == "error" or "skip_no_op"

    @property
    def is_issue(self) -> bool:
        return self.action == "error"


_CSV_HEADER: Final[tuple[str, ...]] = (
    "bank_name",
    "parse_count",
    "last_seen",
    "action",
    "detail",
)


# ─────────────────────────────────────────────────────────────────────
# Source-of-truth aggregation from documents
# ─────────────────────────────────────────────────────────────────────


def aggregate_documents(rows: list[dict[str, Any]]) -> list[BankAggregate]:
    """Group ``documents`` rows by case-insensitive bank_name and emit
    one ``BankAggregate`` per bank.

    Documents with NULL / empty ``bank_name`` are dropped — they
    contribute nothing to layout-learning (the pipeline keys on
    ``bank_name`` for hints lookup, so a null bank can't be primed).
    ``last_seen`` is the max ``uploaded_at`` across the group.

    The group's stored display ``bank_name`` uses the casing of the
    most-recent (largest ``uploaded_at``) entry — that's the casing the
    operator most recently saw on a real statement.
    """
    by_lower: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        bank_name_raw = row.get("bank_name")
        if not isinstance(bank_name_raw, str):
            continue
        normalised = bank_name_raw.strip()
        if not normalised:
            continue
        by_lower.setdefault(normalised.lower(), []).append(row)

    out: list[BankAggregate] = []
    for group in by_lower.values():
        # Sort descending so the first entry is the most-recent — drives
        # both ``last_seen`` and the display casing.
        group.sort(key=lambda r: cast(str, r.get("uploaded_at") or ""), reverse=True)
        most_recent = group[0]
        display_name = cast(str, most_recent["bank_name"]).strip()
        last_seen = cast(str, most_recent.get("uploaded_at") or "")
        if not last_seen:
            # Fall back to a string we can still ISO-parse — should
            # never fire because documents.uploaded_at is NOT NULL, but
            # the script must never crash on a single anomalous row.
            last_seen = datetime.now().isoformat()
        out.append(
            BankAggregate(
                bank_name=display_name,
                parse_count=len(group),
                last_seen=last_seen,
            )
        )
    # Sort high-volume banks first so the summary reads naturally.
    out.sort(key=lambda agg: (-agg.parse_count, agg.bank_name.lower()))
    return out


# ─────────────────────────────────────────────────────────────────────
# Read paths — documents source + existing bank_layouts state
# ─────────────────────────────────────────────────────────────────────


def _fetch_parsed_documents(client: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    """Pull every ``proceed`` / ``review`` document's
    ``(bank_name, uploaded_at)``.

    Two select() calls with PostgREST's ``in`` operator would be
    equivalent; the explicit loop here keeps the SQL trivially
    auditable from the script source.
    """
    rows: list[dict[str, Any]] = []
    for status in _PARSE_OK_STATUSES:
        result = (
            client.table("documents")
            .select("bank_name, uploaded_at, parse_status")
            .eq("parse_status", status)
            .limit(_DOCUMENTS_SCAN_CAP)
            .execute()
        )
        rows.extend(cast(list[dict[str, Any]], result.data or []))
    return rows


def _fetch_existing_layout(
    client: Any,  # noqa: ANN401
    bank_name: str,
) -> dict[str, Any] | None:
    """Read the current ``bank_layouts`` row for ``bank_name`` (case-
    insensitive ilike, matching ``SupabaseBankLayoutRepository`` semantics).
    Returns the raw row dict or ``None`` if absent.
    """
    result = (
        client.table("bank_layouts")
        .select("id, bank_name, successful_parses, last_seen")
        .ilike("bank_name", bank_name)
        .limit(1)
        .execute()
    )
    data = cast(list[dict[str, Any]], result.data or [])
    return data[0] if data else None


# ─────────────────────────────────────────────────────────────────────
# Write path — never-shrink upsert
# ─────────────────────────────────────────────────────────────────────


def _compute_update(
    existing: dict[str, Any] | None,
    aggregate: BankAggregate,
) -> tuple[str, dict[str, Any], str]:
    """Decide the upsert action for one bank.

    Returns ``(action, payload, detail)``. ``action`` is one of:
      * ``"insert"``     — no row exists; write a fresh one.
      * ``"update"``     — row exists, our historical values exceed the
                           stored ones for at least one column.
      * ``"skip_no_op"`` — row exists and both columns are already
                           >= our historical values. Live traffic has
                           already overtaken the backfill; nothing to do.

    Never reduces ``successful_parses`` or ``last_seen``: a concurrent
    ``upsert_success`` running while this script reads stale-then-writes
    must not lose its increment. Reading via
    ``MAX(stored, historical)`` keeps the script idempotent across
    re-runs and concurrent worker activity.
    """
    if existing is None:
        return (
            "insert",
            {
                "bank_name": aggregate.bank_name,
                "successful_parses": aggregate.parse_count,
                "last_seen": aggregate.last_seen,
                "layout_fingerprint": {},
            },
            f"no prior row; seeding with {aggregate.parse_count} parses",
        )

    stored_count = int(existing.get("successful_parses") or 0)
    stored_last_seen = str(existing.get("last_seen") or "")
    new_count = max(stored_count, aggregate.parse_count)
    # Lexicographic max works because both timestamps are ISO-8601 UTC.
    new_last_seen = max(stored_last_seen, aggregate.last_seen)

    if new_count == stored_count and new_last_seen == stored_last_seen:
        return (
            "skip_no_op",
            {},
            f"stored count={stored_count} last_seen={stored_last_seen} already covers historical",
        )

    payload: dict[str, Any] = {}
    if new_count != stored_count:
        payload["successful_parses"] = new_count
    if new_last_seen != stored_last_seen:
        payload["last_seen"] = new_last_seen
    detail = (
        f"stored count={stored_count} → {new_count}, "
        f"last_seen={stored_last_seen!r} → {new_last_seen!r}"
    )
    return ("update", payload, detail)


def apply_aggregate(
    client: Any,  # noqa: ANN401
    aggregate: BankAggregate,
    *,
    apply_writes: bool,
) -> UpsertResult:
    """Resolve one bank's action and (in apply mode) execute the write.

    Dry-run mode returns the same ``UpsertResult`` shape but skips the
    Supabase mutation — useful for previewing the diff before commit.
    """
    try:
        existing = _fetch_existing_layout(client, aggregate.bank_name)
    except Exception as exc:
        return UpsertResult(
            bank_name=aggregate.bank_name,
            parse_count=aggregate.parse_count,
            last_seen=aggregate.last_seen,
            action="error",
            detail=f"read failed: {type(exc).__name__}: {exc}",
        )

    action, payload, detail = _compute_update(existing, aggregate)

    if action == "skip_no_op":
        return UpsertResult(
            bank_name=aggregate.bank_name,
            parse_count=aggregate.parse_count,
            last_seen=aggregate.last_seen,
            action=action,
            detail=detail,
        )

    if not apply_writes:
        return UpsertResult(
            bank_name=aggregate.bank_name,
            parse_count=aggregate.parse_count,
            last_seen=aggregate.last_seen,
            action=action,
            detail=f"DRY-RUN — would {action}: {detail}",
        )

    try:
        if action == "insert":
            client.table("bank_layouts").insert(payload).execute()
        else:
            assert existing is not None  # narrowing: skip_no_op + insert both bail above
            client.table("bank_layouts").update(payload).eq("id", existing["id"]).execute()
    except Exception as exc:
        return UpsertResult(
            bank_name=aggregate.bank_name,
            parse_count=aggregate.parse_count,
            last_seen=aggregate.last_seen,
            action="error",
            detail=f"{action} failed: {type(exc).__name__}: {exc}",
        )

    return UpsertResult(
        bank_name=aggregate.bank_name,
        parse_count=aggregate.parse_count,
        last_seen=aggregate.last_seen,
        action=action,
        detail=detail,
    )


# ─────────────────────────────────────────────────────────────────────
# CSV output
# ─────────────────────────────────────────────────────────────────────


def write_csv(rows: list[UpsertResult], stream: object) -> None:
    """Emit the per-bank action summary as a CSV — one row per bank."""
    writer = csv.writer(stream)  # type: ignore[arg-type]
    writer.writerow(_CSV_HEADER)
    for r in rows:
        writer.writerow(
            (
                r.bank_name,
                r.parse_count,
                r.last_seen,
                r.action,
                r.detail,
            )
        )


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Seed bank_layouts with every successfully-parsed document's "
            "bank_name + parse count + last_seen. DRY-RUN by default; "
            "pass --apply to persist."
        )
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Actually upsert each bank into bank_layouts. Default is "
            "dry-run (read documents, compute diff, print, no writes)."
        ),
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    try:
        client = get_supabase()
    except Exception as exc:
        print(
            f"ERROR: could not initialise Supabase client: {exc}",
            file=sys.stderr,
        )
        traceback.print_exc(file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    try:
        doc_rows = _fetch_parsed_documents(client)
    except Exception as exc:
        print(f"ERROR: document fetch failed: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    aggregates = aggregate_documents(doc_rows)
    results = [apply_aggregate(client, agg, apply_writes=args.apply) for agg in aggregates]

    write_csv(results, sys.stdout)

    inserts = sum(1 for r in results if r.action == "insert")
    updates = sum(1 for r in results if r.action == "update")
    skips = sum(1 for r in results if r.action == "skip_no_op")
    issues = sum(1 for r in results if r.is_issue)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(
        f"# mode={mode} banks={len(results)} inserts={inserts} "
        f"updates={updates} skips={skips} issues={issues}",
        file=sys.stderr,
    )
    return EXIT_ISSUES_FOUND if issues > 0 else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
