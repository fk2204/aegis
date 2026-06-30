"""Persist + read counterparty classifications on ``transactions``.

Companion to ``aegis.counterparty.classify``. The classifier is pure
(dictionary lookup + bundle matcher, no I/O); this module owns the
DB side: persisting fresh classifications back to ``transactions``
after a scoring run AND reading the override-aware view of those
classifications on the next scoring run.

The columns this module reads / writes were added in migration 102:

  * ``counterparty_class``        TEXT (CHECK enum)
  * ``counterparty_confidence``   INTEGER 0..100
  * ``counterparty_reason``       TEXT
  * ``counterparty_overridden``   BOOLEAN NOT NULL DEFAULT FALSE
  * ``counterparty_override_by``  TEXT
  * ``counterparty_override_at``  TIMESTAMPTZ

Override-aware semantics:

* ``persist_classifications`` writes ``counterparty_class``,
  ``counterparty_confidence``, and ``counterparty_reason`` for any
  transaction NOT marked ``counterparty_overridden=TRUE``. Overrides
  are operator-set; the scorer must not clobber them.
* ``load_override_aware_classifications`` reads the persisted columns
  for a merchant's transactions and returns a partial
  ``Mapping[UUID, CounterpartyClassification]`` covering ONLY the
  rows where ``counterparty_overridden=TRUE``. The scoring path
  classifies everything via ``classify_bundle`` first, then merges
  these overrides on top — operator > classifier on the few rows the
  operator touched.

Both functions are best-effort: any DB failure is logged at WARN and
returns the safe default (empty result / no write). The classifier
output remains valid in-memory regardless.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any, cast
from uuid import UUID

from aegis.counterparty.models import (
    CounterpartyClass,
    CounterpartyClassification,
)
from aegis.db import get_supabase

_log = logging.getLogger(__name__)

# Supabase REST batch size — the same chunk size the
# ``audit_archiver`` insert path uses (verified well below the 1 MB
# request-body cap for a 6-column payload).
_BATCH_SIZE: int = 500

# Valid CounterpartyClass values — mirrors the Pydantic Literal in
# ``aegis.counterparty.models``. Used to skip writing an override that
# would fail the migration-102 CHECK constraint.
_VALID_CLASSES: frozenset[str] = frozenset(
    {
        "processor",
        "own_account",
        "own_account_unconfirmed",
        "card_paydown",
        "international_client",
        "end_customer",
        "book_wire_unresolved",
        "unknown",
    }
)


def persist_classifications(
    classifications: Mapping[UUID, CounterpartyClassification],
) -> int:
    """Batch-upsert per-transaction classifications.

    Writes the three non-override columns
    (``counterparty_class``, ``counterparty_confidence``,
    ``counterparty_reason``) for every transaction in
    ``classifications``. Does NOT touch the override columns —
    the migration-102 CHECK constraint enforces override integrity
    independently.

    Override-aware: transactions whose ``counterparty_overridden=TRUE``
    are skipped before the write so the operator's override is
    preserved verbatim. The check is a single SELECT against the
    transaction id set; the WRITE is one upsert call per
    ``_BATCH_SIZE`` chunk.

    Returns the number of rows actually written (post-override filter,
    post-batching). Returns 0 on any DB failure — failures are logged
    at WARN; the in-memory classifier output remains valid for the
    rest of the scoring pass.
    """
    if not classifications:
        return 0

    txn_ids = [str(tid) for tid in classifications]

    try:
        sb = get_supabase()
        overridden_resp = (
            sb.table("transactions")
            .select("id")
            .in_("id", txn_ids)
            .eq("counterparty_overridden", True)
            .execute()
        )
    except Exception as exc:
        # ``get_supabase`` raises when env vars are missing (tests /
        # CLI scripts without Supabase config). Treat as "no DB, nothing
        # to persist" — the in-memory classifier output is intact for
        # the rest of the scoring pass.
        _log.warning(
            "counterparty.persist.overridden_query_failed n_txns=%d exc=%s",
            len(txn_ids),
            exc,
        )
        return 0

    overridden_ids: set[str] = set()
    for raw_row in overridden_resp.data or []:
        if not isinstance(raw_row, dict):
            continue
        row_id = cast(dict[str, Any], raw_row).get("id")
        if row_id is not None:
            overridden_ids.add(str(row_id))

    rows_to_write: list[dict[str, Any]] = []
    for txn_id, cc in classifications.items():
        if str(txn_id) in overridden_ids:
            continue
        rows_to_write.append(
            {
                "id": str(txn_id),
                "counterparty_class": cc.counterparty,
                "counterparty_confidence": int(cc.confidence),
                "counterparty_reason": cc.reason or "",
            }
        )

    if not rows_to_write:
        return 0

    written = 0
    for chunk in _chunked(rows_to_write, _BATCH_SIZE):
        try:
            sb.table("transactions").upsert(
                cast(Any, chunk),
                on_conflict="id",
            ).execute()
            written += len(chunk)
        except Exception as exc:
            _log.warning(
                "counterparty.persist.upsert_failed chunk_size=%d exc=%s",
                len(chunk),
                exc,
            )
            # Continue to the next chunk — partial-success on a
            # multi-chunk write is better than rolling back successful
            # chunks for an unrelated chunk's failure.
            continue
    return written


def load_override_aware_classifications(
    transaction_ids: Iterable[UUID],
) -> dict[UUID, CounterpartyClassification]:
    """Return the operator-overridden classifications for the given txns.

    Reads the migration-102 columns and returns a partial mapping
    covering ONLY ``counterparty_overridden=TRUE`` rows. The caller
    merges this on top of the live ``classify_bundle`` output so the
    operator's overrides take precedence.

    Returns an empty dict on DB failure (logged at WARN) — the caller's
    classifier output remains the source of truth.
    """
    ids = [str(t) for t in transaction_ids]
    if not ids:
        return {}

    try:
        sb = get_supabase()
        resp = (
            sb.table("transactions")
            .select(
                "id,counterparty_class,counterparty_confidence,"
                "counterparty_reason,counterparty_overridden"
            )
            .in_("id", ids)
            .eq("counterparty_overridden", True)
            .execute()
        )
    except Exception as exc:
        # ``get_supabase`` raises when env vars are missing (tests /
        # unit harnesses without Supabase config); the query itself can
        # raise on network blips. Both collapse to "no overrides
        # available" so the in-memory classifier output stays the
        # source of truth.
        _log.warning(
            "counterparty.load_overrides.query_failed n_txns=%d exc=%s",
            len(ids),
            exc,
        )
        return {}

    out: dict[UUID, CounterpartyClassification] = {}
    for raw_row in resp.data or []:
        if not isinstance(raw_row, dict):
            continue
        row = cast(dict[str, Any], raw_row)
        cls = row.get("counterparty_class")
        if cls not in _VALID_CLASSES:
            # Defensive: a stored value outside the enum would mean
            # the CHECK constraint was bypassed. Skip rather than
            # raise; the live classifier's value is the safer default.
            continue
        try:
            txn_uuid = UUID(str(row["id"]))
        except (KeyError, ValueError):
            continue
        confidence = row.get("counterparty_confidence")
        if not isinstance(confidence, int) or not (0 <= confidence <= 100):
            continue
        reason = row.get("counterparty_reason") or ""
        out[txn_uuid] = CounterpartyClassification(
            transaction_id=txn_uuid,
            counterparty=cast(CounterpartyClass, cls),
            confidence=confidence,
            reason=str(reason)[:64],
            other_account_last4=None,
            paired_transaction_id=None,
        )
    return out


def record_override(
    *,
    transaction_id: UUID,
    counterparty_class: str,
    reason: str,
    operator: str,
) -> bool:
    """Persist an operator override of one transaction's counterparty.

    Sets the migration-102 override columns
    (``counterparty_overridden=TRUE``, ``counterparty_override_by``,
    ``counterparty_override_at``) alongside the new
    ``counterparty_class`` and a reason. The CHECK constraint
    enforces enum membership; we mirror it client-side so a bad input
    fails loud rather than as a Supabase 23514.

    Returns True on a successful write, False on any failure
    (validation OR DB) with the cause logged.
    """
    from datetime import UTC, datetime

    if counterparty_class not in _VALID_CLASSES:
        _log.warning(
            "counterparty.override.invalid_class txn=%s class=%s",
            transaction_id,
            counterparty_class,
        )
        return False

    payload = {
        "counterparty_class": counterparty_class,
        "counterparty_reason": reason[:1000] if reason else "operator_override",
        "counterparty_overridden": True,
        "counterparty_override_by": operator,
        "counterparty_override_at": datetime.now(UTC).isoformat(),
    }

    sb = get_supabase()
    try:
        sb.table("transactions").update(cast(Any, payload)).eq("id", str(transaction_id)).execute()
        return True
    except Exception as exc:
        _log.warning(
            "counterparty.override.write_failed txn=%s exc=%s",
            transaction_id,
            exc,
        )
        return False


def _chunked(rows: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    """Split ``rows`` into chunks of at most ``size``. Returns the list
    of chunks (last chunk may be shorter). Empty input → empty list.
    """
    if not rows:
        return []
    return [rows[i : i + size] for i in range(0, len(rows), size)]


__all__ = [
    "load_override_aware_classifications",
    "persist_classifications",
    "record_override",
]
