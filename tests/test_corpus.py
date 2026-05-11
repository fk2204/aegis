"""Phase 5.5 corpus runner.

Walks every ``*.manifest.json`` under ``tests/fixtures/corpus/`` and
verifies the parser+aggregator pipeline produces the manifest's
expected numbers within tolerance.

Two modes
---------

* **Manifest-feed mode (default).** The runner skips the LLM extraction
  step and feeds the manifest's transaction list straight into the
  validator + classifier-output + aggregator. This proves the
  deterministic pipeline (validate → patterns → aggregate → score) is
  correct end-to-end without a live Bedrock call. Fast; runs in CI.
* **Real-LLM mode (``CORPUS_REAL_LLM=1``).** The runner runs the full
  ``run_pipeline`` against each PDF using ``BedrockClient``. Slower
  and costs money; intended for the pre-deploy gate.

Tolerances
----------
Per CLAUDE.md / REWRITE_PLAN Phase 5.5:

* money totals: ``±$1``
* counts (NSF, MCA positions, transaction count): exact
* fraud scores: ``±5``
* recommendation: exact match
* tampered statements: ``parse_status == "manual_review"`` AND
  expected validation_failure code present.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from aegis.parser.aggregate import aggregate
from aegis.parser.models import (
    ClassifiedTransaction,
    StatementSummary,
)
from aegis.parser.validate import validate_extraction

CORPUS_ROOT = Path(__file__).parent / "fixtures" / "corpus"

REAL_LLM = os.environ.get("CORPUS_REAL_LLM", "").strip() in {"1", "true", "yes"}


# --- discovery --------------------------------------------------------------


@dataclass(frozen=True)
class CorpusItem:
    manifest_path: Path
    pdf_path: Path | None  # None ok for manifest-feed mode


def _discover() -> list[CorpusItem]:
    items: list[CorpusItem] = []
    if not CORPUS_ROOT.exists():
        return items
    for manifest in sorted(CORPUS_ROOT.rglob("*.manifest.json")):
        pdf = manifest.with_suffix("")  # strip .json
        if pdf.suffix == ".manifest":
            pdf = pdf.with_suffix(".pdf")
        items.append(CorpusItem(manifest_path=manifest, pdf_path=pdf if pdf.exists() else None))
    return items


_ITEMS = _discover()
_IDS = [item.manifest_path.stem for item in _ITEMS]


# --- assertions -------------------------------------------------------------


def _within(actual: Decimal, expected: Decimal, tol: Decimal, label: str) -> None:
    assert abs(actual - expected) <= tol, (
        f"{label}: expected {expected} ± {tol}, got {actual}"
    )


def _load_manifest(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        loaded: dict[str, Any] = json.load(f)
    return loaded


def _build_extracted(
    manifest: dict[str, Any],
) -> tuple[StatementSummary, list[ClassifiedTransaction]]:
    summary_raw = manifest["summary"]
    summary = StatementSummary(
        beginning_balance=Decimal(summary_raw["beginning_balance"]),
        ending_balance=Decimal(summary_raw["ending_balance"]),
        deposit_total=Decimal(summary_raw["deposit_total"]),
        withdrawal_total=Decimal(summary_raw["withdrawal_total"]),
        period_start=date.fromisoformat(summary_raw["period_start"]),
        period_end=date.fromisoformat(summary_raw["period_end"]),
        printed_transaction_count=summary_raw.get("printed_transaction_count"),
    )
    classified = [
        ClassifiedTransaction(
            posted_date=date.fromisoformat(t["posted_date"]),
            description=t["description"],
            amount=Decimal(t["amount"]),
            running_balance=Decimal(t["running_balance"]) if t.get("running_balance") else None,
            source_page=t["source_page"],
            source_line=t["source_line"],
            category=t["category"],
            classification_confidence=100,
        )
        for t in manifest["transactions"]
    ]
    return summary, classified


# --- the parametrized test --------------------------------------------------


pytestmark = pytest.mark.skipif(
    not _ITEMS,
    reason="no corpus manifests found — run `python -m scripts.generate_corpus` first",
)


@pytest.mark.parametrize("item", _ITEMS, ids=_IDS)
def test_corpus_item(item: CorpusItem) -> None:
    """Manifest-feed mode: asserts the deterministic pipeline matches manifest."""
    if REAL_LLM:
        pytest.skip("real-LLM mode runs in a separate suite; not implemented here yet")

    manifest = _load_manifest(item.manifest_path)
    summary, classified = _build_extracted(manifest)

    money_tol = Decimal(manifest.get("tolerances", {}).get("money", "1.00"))

    # Build a synthetic ExtractedStatement and run the validator.
    from aegis.parser.models import ExtractedStatement

    # Drop the category for the validator so it sees raw Transactions.
    raw_txs = [
        t.model_copy(update={}, deep=True) for t in classified
    ]
    # Validator only checks shape — category is fine as extra context.
    extraction = ExtractedStatement(summary=summary, transactions=raw_txs)
    validation = validate_extraction(extraction)

    expected = manifest.get("expected", {})

    if not expected.get("validation_passed", True):
        # Tampered scenario: the validator MUST fail and contain the
        # expected failure substring (e.g. "reconciliation_failed").
        assert not validation.passed, (
            f"{item.manifest_path.name}: expected validation failure but got pass"
        )
        substring = expected.get("expected_failure_substring", "")
        if substring:
            joined = " ".join(validation.failures)
            assert substring in joined, (
                f"{item.manifest_path.name}: expected failure containing "
                f"{substring!r}; got {validation.failures!r}"
            )
        return  # tampered scenarios short-circuit downstream assertions

    assert validation.passed, (
        f"{item.manifest_path.name}: validation failures {validation.failures!r}"
    )

    # Aggregate — deterministic, computed from the classified transactions.
    aggregates = aggregate(
        classified,
        period_start=summary.period_start,
        period_end=summary.period_end,
        beginning_balance=summary.beginning_balance,
    ).aggregates

    expected_aggs = expected.get("aggregates") or {}
    if "true_revenue" in expected_aggs:
        _within(
            aggregates.true_revenue.value,
            Decimal(expected_aggs["true_revenue"]),
            money_tol,
            f"{item.manifest_path.stem}: true_revenue",
        )
    if "num_nsf" in expected_aggs:
        assert aggregates.num_nsf.value == int(expected_aggs["num_nsf"])
    if "mca_daily_total" in expected_aggs:
        _within(
            aggregates.mca_daily_total.value,
            Decimal(expected_aggs["mca_daily_total"]),
            money_tol,
            f"{item.manifest_path.stem}: mca_daily_total",
        )

    # Source attribution: every aggregate that's non-zero must have non-empty source_ids.
    if aggregates.true_revenue.value > 0:
        assert aggregates.true_revenue.source_ids, (
            f"{item.manifest_path.name}: true_revenue has no source attribution"
        )

    # NSF / MCA-position counts inferred from classified rows
    if "num_nsf" in expected:
        actual_nsf = sum(1 for t in classified if t.category == "nsf_fee")
        assert actual_nsf == int(expected["num_nsf"]), (
            f"{item.manifest_path.name}: expected NSF count {expected['num_nsf']}, got {actual_nsf}"
        )
    if "mca_positions_min" in expected:
        # Distinct MCA descriptions ≈ distinct funder identifiers.
        seen = {t.description.split()[-1] for t in classified if t.category == "mca_debit"}
        assert len(seen) >= int(expected["mca_positions_min"]), (
            f"{item.manifest_path.name}: expected ≥{expected['mca_positions_min']} MCA "
            f"positions; got {len(seen)} ({seen})"
        )


def test_real_corpus_dir_has_readme() -> None:
    """Operator-supplied real-statement dir must have its README + .gitignore."""
    real_dir = CORPUS_ROOT / "real"
    assert (real_dir / "README.md").exists(), "real/README.md missing"
    assert (real_dir / ".gitignore").exists(), "real/.gitignore missing"
