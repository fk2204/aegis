"""Backfill the ``decisions`` table from pre-existing documents (mp Phase 2).

Per master plan §2 principle 3 + the plan's refinement (4):

- Use the **original** decision timestamp from ``documents.parsed_at``
  (never ``NOW()``) so the snapshot reflects when the call was made.
- Do NOT re-run the scorer to fill ``score_factors``. Re-deriving means
  "today's scorer's verdict on old inputs", which defeats the snapshot
  contract.
- Mark every backfill row with ``backfill_quality='minimal'`` — only
  the anchors (deal_id, decision, state_code, cfdl_tier, decided_at,
  decided_by) are populated. ``score``, ``score_factors``,
  ``contributing_transaction_uuids``, ``bank_statement_pdf_sha256``,
  ``apr_*``, and ``ofac_*`` stay null/empty because they were never
  captured at the time.
- ``aegis_version='backfill'``, ``rule_pack_version='pre-snapshot-table'``,
  ``decided_by='backfill_2026_05'``.
- Idempotent: the partial unique index on
  ``(deal_id) WHERE decided_by = 'backfill_2026_05'`` lets us re-run
  the script without producing duplicates.

Maps ``documents.parse_status`` to ``decisions.decision``:

    proceed         -> approve
    review          -> manual_review
    manual_review   -> manual_review
    pending / error -> skipped (no decision was made)

States: ``decisions.state_code`` is NOT NULL. If a document's merchant
has no state (or no merchant link), the row is skipped — a row without
state_code can't be loaded against the state matrix.

Run:
    uv run python -m scripts.backfill_decisions          # backfill
    uv run python -m scripts.backfill_decisions --dry-run  # report only
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Final

from aegis.db import get_supabase
from aegis.logger import get_logger

_log = get_logger(__name__)

# Per the plan refinement (4): identifiers for the backfill batch.
BACKFILL_DECIDED_BY: Final[str] = "backfill_2026_05"
BACKFILL_AEGIS_VERSION: Final[str] = "backfill"
BACKFILL_RULE_PACK_VERSION: Final[str] = "pre-snapshot-table"

# Map documents.parse_status -> decisions.decision. Documents in `pending`
# or `error` never reached a decision and are skipped.
_STATUS_TO_DECISION: Final[dict[str, str]] = {
    "proceed": "approve",
    "review": "manual_review",
    "manual_review": "manual_review",
}


def _eligible_documents(client: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    """Fetch (document, merchant.state, analysis.id) for each document
    whose parse_status maps to a decision.

    ``client`` is supabase-py's untyped Client. ANN401 noqa because
    supabase-py ships without type stubs; typing it as ``Any`` is the
    project-wide convention (see existing usage in aegis.audit).

    Single denormalized read so the script is one round-trip per row in
    the resulting list, not three.
    """
    # supabase-py's .select() with foreign-key embeds is the path. We
    # only need a few columns from each linked row.
    result = (
        client.table("documents")
        .select(
            "id, parse_status, parsed_at, "
            "merchants(id, state), "
            "analyses(id)"
        )
        .in_("parse_status", list(_STATUS_TO_DECISION.keys()))
        .execute()
    )
    return list(result.data or [])


def _build_row(doc: dict[str, Any]) -> dict[str, Any] | None:
    """Translate a document row into a minimal decisions row.

    Returns ``None`` if the document is missing the data we require
    (state or parsed_at).
    """
    merchants = doc.get("merchants")
    if not merchants or not merchants.get("state"):
        return None

    state_code = merchants["state"].upper()
    parse_status = doc["parse_status"]
    decision = _STATUS_TO_DECISION.get(parse_status)
    if decision is None:
        return None

    decided_at = doc.get("parsed_at")
    if decided_at is None:
        # Document was never parsed — skip; nothing to backfill.
        return None

    analyses = doc.get("analyses") or []
    # analyses is a list because the FK is set up that way; in practice
    # one document has one analysis at most. Take the first if present.
    analysis_id: str | None = None
    if isinstance(analyses, list) and analyses:
        analysis_id = analyses[0].get("id")
    elif isinstance(analyses, dict):
        analysis_id = analyses.get("id")

    return {
        "deal_id": doc["id"],
        "decided_at": decided_at,
        "decided_by": BACKFILL_DECIDED_BY,
        "decision": decision,
        "decision_reason_codes": [],
        "score": None,
        "score_factors": {},
        "analysis_id": analysis_id,
        "contributing_transaction_uuids": [],
        "bank_statement_pdf_sha256": None,
        "state_code": state_code,
        # cfdl_tier = 3 (defensive default) — without re-running the
        # router we don't know which tier applied at the time, and
        # claiming Tier 1 retroactively would be a regulator-defense
        # liability. Tier 3 = "defensive disclosure was the posture".
        "cfdl_tier": 3,
        "disclosure_template_path": None,
        "disclosure_template_sha256": None,
        "disclosure_pdf_sha256": None,
        "apr_calculated": None,
        "apr_method": None,
        "ofac_cache_timestamp": None,
        "ofac_cache_sha256": None,
        "aegis_version": BACKFILL_AEGIS_VERSION,
        "rule_pack_version": BACKFILL_RULE_PACK_VERSION,
        "backfill_quality": "minimal",
    }


def backfill(*, dry_run: bool = False) -> dict[str, int]:
    """Run the backfill. Returns counts: candidates / written / skipped.

    Idempotent: re-running on the same DB produces no duplicate rows
    thanks to the partial unique index from migration 015.
    """
    client = get_supabase()
    docs = _eligible_documents(client)
    counts = {"candidates": len(docs), "written": 0, "skipped": 0}

    rows: list[dict[str, Any]] = []
    for doc in docs:
        row = _build_row(doc)
        if row is None:
            counts["skipped"] += 1
            continue
        rows.append(row)

    if not rows:
        _log.info(
            "backfill_decisions.no_rows",
            extra={
                "candidates": counts["candidates"],
                "skipped": counts["skipped"],
            },
        )
        return counts

    if dry_run:
        _log.info(
            "backfill_decisions.dry_run",
            extra={
                "would_write": len(rows),
                "skipped": counts["skipped"],
            },
        )
        counts["written"] = 0
        return counts

    # ON CONFLICT DO NOTHING via the partial unique index makes this
    # idempotent. supabase-py exposes upsert(); we use insert() with
    # explicit on_conflict to keep behavior visible.
    try:
        client.table("decisions").upsert(
            rows,
            on_conflict="deal_id",  # backed by uq_decisions_backfill_per_deal
            ignore_duplicates=True,
        ).execute()
    except Exception:
        _log.exception("backfill_decisions.write_failed")
        raise

    counts["written"] = len(rows)
    _log.info(
        "backfill_decisions.done",
        extra={
            "candidates": counts["candidates"],
            "written": counts["written"],
            "skipped": counts["skipped"],
        },
    )
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else "")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the row set but write nothing.",
    )
    args = parser.parse_args(argv)
    counts = backfill(dry_run=args.dry_run)
    mode = "DRY-RUN" if args.dry_run else "WROTE"
    print(
        f"[backfill_decisions] {mode} "
        f"candidates={counts['candidates']} "
        f"written={counts['written']} "
        f"skipped={counts['skipped']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
