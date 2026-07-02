#!/usr/bin/env python3
"""Backfill migration-102 counterparty classifications for merchants
whose analyzed documents have unclassified transactions.

Rationale
---------
Migration 102 added ``transactions.counterparty_class`` and companion
columns; ``build_unified_tracks_view`` (dossier renderer) is the code
path that classifies + persists as a side effect of every dossier
open. Legacy documents that predate migration 102 — and merchants no
operator has opened since — carry NULL classifications. Track B /
Track C fall back to unclassified aggregates for those merchants,
which weakens the risk-band signal.

This script walks every merchant with at least one ``proceed`` doc,
runs the classifier + persistence pass directly (bypasses the dossier
render path so it stays cheap), and stages the writes 15 seconds
apart per merchant to keep Supabase load flat.

Usage
-----

    # Dry-run — no writes, only counts:
    uv run python scripts/backfill_counterparty_classifications.py --dry-run

    # Apply — actually persists:
    uv run python scripts/backfill_counterparty_classifications.py --apply

    # Cap the scan (useful for smoke-tests):
    uv run python scripts/backfill_counterparty_classifications.py --apply --limit 20

Requires ``.env`` at repo root with ``SUPABASE_URL``, ``SUPABASE_KEY``,
and ``AEGIS_DATA_RESIDENCY_CONFIRMED=true``.

Idempotent — merchants whose transactions are already fully classified
are skipped without a write.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any, cast
from uuid import UUID

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

_env_file = _REPO_ROOT / ".env"
if _env_file.exists():
    _raw = _env_file.read_bytes()
    if _raw.startswith(b"\xff\xfe"):
        _text = _raw.decode("utf-16-le")
    elif _raw.startswith(b"\xfe\xff"):
        _text = _raw.decode("utf-16-be")
    elif _raw.startswith(b"\xef\xbb\xbf"):
        _text = _raw[3:].decode("utf-8")
    else:
        _text = _raw.decode("utf-8", errors="replace")
    for _line in _text.splitlines():
        _line = _line.strip().lstrip("﻿")
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from aegis.counterparty import classify_bundle  # noqa: E402
from aegis.counterparty.persistence import (  # noqa: E402
    load_override_aware_classifications,
    persist_classifications,
)
from aegis.db import get_supabase  # noqa: E402

_STAGGER_SECONDS: int = 15


def _rows(result: object) -> list[dict[str, Any]]:
    data = cast(Any, result).data
    return cast(list[dict[str, Any]], data or [])


def _load_merchants_with_unclassified_txns(
    limit: int | None,
) -> list[dict[str, Any]]:
    """Return merchant ids that own at least one proceed document whose
    transactions are missing ``counterparty_class``.

    Cheap two-step query: first the doc set (small), then a NULL scan on
    the transactions of those docs (still small — a few hundred rows on
    the current corpus).
    """
    sb = get_supabase()
    docs = _rows(
        sb.table("documents").select("id,merchant_id").eq("parse_status", "proceed").execute()
    )
    if not docs:
        return []

    docs_by_merchant: dict[str, list[str]] = {}
    for d in docs:
        docs_by_merchant.setdefault(str(d["merchant_id"]), []).append(str(d["id"]))

    unclassified_merchants: list[dict[str, Any]] = []
    for merchant_id, doc_ids in docs_by_merchant.items():
        result = _rows(
            sb.table("transactions")
            .select("id")
            .in_("document_id", doc_ids)
            .is_("counterparty_class", "null")
            .limit(1)
            .execute()
        )
        if result:
            unclassified_merchants.append({"merchant_id": merchant_id, "doc_ids": doc_ids})
            if limit is not None and len(unclassified_merchants) >= limit:
                break
    return unclassified_merchants


def _load_transactions_by_doc(
    doc_ids: list[str],
) -> tuple[dict[str, list[Any]], set[str]]:
    """Load transaction rows for the given documents grouped by document,
    plus the union of ``account_last4`` values across their analyses.

    Returns ``(transactions_by_doc, accounts)``. Uses the same
    ``list_transactions`` model shape ``classify_bundle`` expects.
    """
    from aegis.api.deps import get_repository  # local — heavy imports

    docs = get_repository()
    transactions_by_doc: dict[str, list[Any]] = {}
    for doc_id_str in doc_ids:
        doc_uuid = UUID(doc_id_str)
        try:
            txns = docs.list_transactions(doc_uuid)
        except Exception:  # pragma: no cover — never block backfill on one doc
            txns = []
        if txns:
            transactions_by_doc[doc_id_str] = list(txns)

    accounts: set[str] = set()
    sb = get_supabase()
    if doc_ids:
        analyses = _rows(
            sb.table("analyses")
            .select("document_id,account_last4")
            .in_("document_id", doc_ids)
            .execute()
        )
        for a in analyses:
            last4 = a.get("account_last4")
            if last4:
                accounts.add(str(last4))

    return transactions_by_doc, accounts


def _classify_and_persist_for_merchant(
    merchant_id: str,
    doc_ids: list[str],
    *,
    apply: bool,
) -> tuple[int, int]:
    """Classify + persist for one merchant. Returns ``(classified, written)``.

    ``classified`` counts the classifier's output rows; ``written`` is
    the number of rows persisted (zero on dry-run). Overridden rows
    are preserved by ``persist_classifications`` server-side.
    """
    transactions_by_doc, accounts = _load_transactions_by_doc(doc_ids)
    if not transactions_by_doc:
        return 0, 0

    classifications, _summary = classify_bundle(transactions_by_doc, accounts)

    all_txn_ids = [t.id for txns in transactions_by_doc.values() for t in txns]
    overrides = load_override_aware_classifications(all_txn_ids)
    if overrides:
        merged: dict[UUID, Any] = dict(classifications)
        merged.update(overrides)
        classifications = merged

    classified = len(classifications)
    written = 0
    if apply and classifications:
        written = persist_classifications(classifications)
    return classified, written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Persist counterparty rows.")
    mode.add_argument("--dry-run", action="store_true", help="Report scope only. Default.")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of merchants to backfill.",
    )
    parser.add_argument(
        "--stagger",
        type=int,
        default=_STAGGER_SECONDS,
        help=f"Seconds between per-merchant runs (default {_STAGGER_SECONDS}).",
    )
    args = parser.parse_args()
    apply = bool(args.apply)
    stagger: int = max(0, int(args.stagger))
    limit: int | None = args.limit

    print(f"# mode={'APPLY' if apply else 'DRY-RUN'}  stagger={stagger}s")
    merchants = _load_merchants_with_unclassified_txns(limit)
    total = len(merchants)
    print(f"Merchants with unclassified transactions: {total}")
    if total == 0:
        print("Nothing to do.")
        return 0

    total_classified = 0
    total_written = 0
    for i, m in enumerate(merchants, start=1):
        merchant_id: str = m["merchant_id"]
        doc_ids: list[str] = m["doc_ids"]
        try:
            classified, written = _classify_and_persist_for_merchant(
                merchant_id, doc_ids, apply=apply
            )
        except Exception as exc:  # pragma: no cover — never block the batch
            print(f"  [{i}/{total}] merchant={merchant_id[:8]}  ERROR: {exc}")
            continue
        print(
            f"  [{i}/{total}] merchant={merchant_id[:8]}  "
            f"docs={len(doc_ids)}  classified={classified}  written={written}"
        )
        total_classified += classified
        total_written += written
        if stagger and i < total:
            time.sleep(stagger)

    print()
    print(
        f"# RESULT mode={'APPLY' if apply else 'DRY-RUN'}\n"
        f"  merchants:        {total}\n"
        f"  total_classified: {total_classified}\n"
        f"  total_written:    {total_written}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
