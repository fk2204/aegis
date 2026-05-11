"""Pass 2 — classify each validated transaction into a category.

Runs after the deterministic validation gate. Operates on transactions
that have already passed Pydantic + reconciliation, so the only thing the
LLM is doing here is labeling. Misclassifying one row is recoverable
(low confidence triggers manual review); misclassifying many shows up as
a low average confidence and the whole document goes to review.

Batched at 50 transactions per Bedrock call to stay well under output
token limits and to bound cost.
"""

from __future__ import annotations

import json
from statistics import mean
from typing import Any, Final, get_args

from aegis.llm import LLMClient
from aegis.parser.models import ClassifiedTransaction, Transaction, TransactionCategory
from aegis.parser.prompts import CLASSIFICATION_PROMPT_HEADER

_BATCH_SIZE: Final[int] = 50

_VALID_CATEGORIES: Final[frozenset[str]] = frozenset(get_args(TransactionCategory))


class ClassificationError(RuntimeError):
    """Raised when the LLM response cannot be turned into ClassifiedTransactions."""


def classify_transactions(
    transactions: list[Transaction],
    llm: LLMClient,
    batch_size: int = _BATCH_SIZE,
) -> list[ClassifiedTransaction]:
    """Classify a flat list of validated transactions in batches."""
    if not transactions:
        return []
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")

    by_id: dict[str, Transaction] = {str(t.id): t for t in transactions}
    classifications: dict[str, tuple[str, int]] = {}

    for start in range(0, len(transactions), batch_size):
        batch = transactions[start : start + batch_size]
        prompt = _build_batch_prompt(batch)
        try:
            raw = llm.classify_batch_json(prompt)
        except ValueError as exc:
            raise ClassificationError(f"LLM returned malformed JSON: {exc}") from exc

        for entry in _coerce_classifications(raw):
            classifications[entry["id"]] = (entry["category"], entry["confidence"])

    return _merge(transactions, classifications, by_id)


def avg_classification_confidence(
    classified: list[ClassifiedTransaction],
) -> int:
    """Return the integer mean of classification_confidence across rows.

    Empty list -> 100 (vacuously confident, nothing to be wrong about).
    Pipeline reads this and compares against CLASSIFICATION_CONFIDENCE_FLOOR.
    """
    if not classified:
        return 100
    return round(mean(c.classification_confidence for c in classified))


def per_category_confidence(
    classified: list[ClassifiedTransaction],
) -> dict[str, int]:
    """Average classification_confidence grouped by category.

    Returns int means keyed by category. Categories with zero rows are absent.
    Pipeline uses this to escalate on low confidence in high-impact categories
    (mca_debit, nsf_fee) regardless of the overall average.
    """
    buckets: dict[str, list[int]] = {}
    for txn in classified:
        buckets.setdefault(txn.category, []).append(txn.classification_confidence)
    return {cat: round(mean(vals)) for cat, vals in buckets.items()}


def _build_batch_prompt(batch: list[Transaction]) -> str:
    payload = [
        {
            "id": str(t.id),
            "posted_date": t.posted_date.isoformat(),
            "description": t.description,
            "amount": str(t.amount),
        }
        for t in batch
    ]
    return CLASSIFICATION_PROMPT_HEADER + "\n" + json.dumps(payload)


def _coerce_classifications(raw: dict[str, Any]) -> list[dict[str, Any]]:
    items = raw.get("classifications")
    if not isinstance(items, list):
        raise ClassificationError(
            "classification JSON missing top-level `classifications` array"
        )
    out: list[dict[str, Any]] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        eid = entry.get("id")
        cat = entry.get("category")
        conf = entry.get("confidence")
        if not isinstance(eid, str) or not isinstance(cat, str):
            continue
        if cat not in _VALID_CATEGORIES:
            cat = "other"
        try:
            conf_int = int(conf) if conf is not None else 0
        except (TypeError, ValueError):
            conf_int = 0
        conf_int = max(0, min(100, conf_int))
        out.append({"id": eid, "category": cat, "confidence": conf_int})
    return out


def _merge(
    transactions: list[Transaction],
    classifications: dict[str, tuple[str, int]],
    by_id: dict[str, Transaction],
) -> list[ClassifiedTransaction]:
    result: list[ClassifiedTransaction] = []
    for txn in transactions:
        category, confidence = classifications.get(str(txn.id), ("other", 0))
        result.append(
            ClassifiedTransaction(
                id=txn.id,
                posted_date=txn.posted_date,
                description=txn.description,
                amount=txn.amount,
                running_balance=txn.running_balance,
                source_page=txn.source_page,
                source_line=txn.source_line,
                category=category,
                classification_confidence=confidence,
            )
        )
    _ = by_id  # currently unused; kept so future per-id sanity checks can land here
    return result


__all__ = [
    "ClassificationError",
    "avg_classification_confidence",
    "classify_transactions",
    "per_category_confidence",
]
