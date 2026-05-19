"""Phase 9 synthetic-corpus regression tests.

Runs ``analyze_patterns`` over selected synthetic-corpus fixtures and
asserts that the new Phase 9 detectors (counterparty concentration,
payroll-present, ai_generated_score) behave correctly on realistic
transaction streams. Complements ``test_corpus.py`` (which only
verifies the parser's aggregate math).

These tests intentionally do NOT modify the manifest files. They only
read the transactions array and run patterns over it, so they are
allowed to make additive assertions about the new detector outputs
without changing the corpus contract.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from aegis.parser.models import ClassifiedTransaction
from aegis.parser.patterns import analyze_patterns

CORPUS_ROOT = Path(__file__).parent.parent / "fixtures" / "corpus" / "synthetic"


def _load(name: str) -> tuple[list[ClassifiedTransaction], date, date]:
    manifest_path = CORPUS_ROOT / f"{name}.manifest.json"
    with manifest_path.open("r", encoding="utf-8") as f:
        manifest = json.load(f)
    summary = manifest["summary"]
    period_start = date.fromisoformat(summary["period_start"])
    period_end = date.fromisoformat(summary["period_end"])
    rows = [
        ClassifiedTransaction(
            id=uuid4(),
            posted_date=date.fromisoformat(t["posted_date"]),
            description=t["description"],
            amount=Decimal(t["amount"]),
            running_balance=(
                Decimal(t["running_balance"]) if t.get("running_balance") else None
            ),
            source_page=t["source_page"],
            source_line=t["source_line"],
            category=t["category"],
            classification_confidence=100,
        )
        for t in manifest["transactions"]
    ]
    return rows, period_start, period_end


def _has_fixture(name: str) -> bool:
    return (CORPUS_ROOT / f"{name}.manifest.json").exists()


# Tests are skipped when their target fixtures are missing (e.g. when a
# user trims the corpus locally). The reference shipped corpus contains
# all of these.

CUSTOMER_CONCENTRATION_FIXTURE = "customer_concentration_chase_business_10008"
CLEAN_FIXTURE = "clean_profitable_chase_business_10001"


@pytest.mark.skipif(
    not _has_fixture(CUSTOMER_CONCENTRATION_FIXTURE),
    reason="customer_concentration synthetic fixture not present",
)
def test_customer_concentration_fixture_triggers_detector() -> None:
    rows, ps, pe = _load(CUSTOMER_CONCENTRATION_FIXTURE)
    res = analyze_patterns(rows, period_start=ps, period_end=pe)
    codes = {p.code for p in res.patterns}
    # The dedicated "customer_concentration" scenario in synthetic
    # corpus carries >30% from a single payer by construction. Detector
    # should fire and counterparty signals should be populated.
    assert "customer_concentration" in codes, (
        f"customer_concentration fixture did not trigger detector; "
        f"got patterns={codes}"
    )
    sig = res.counterparty_signals
    assert sig.top_counterparty_pct is not None
    assert sig.top_counterparty_pct > 30


@pytest.mark.skipif(
    not _has_fixture(CLEAN_FIXTURE),
    reason="clean_profitable synthetic fixture not present",
)
def test_clean_fixture_no_phase9_hard_decline_triggers() -> None:
    rows, ps, pe = _load(CLEAN_FIXTURE)
    res = analyze_patterns(rows, period_start=ps, period_end=pe)
    # A clean profitable merchant should NOT trigger acceleration_clause
    # or unauthorized_withdrawal_dispute. (customer_concentration may
    # fire on a small fixture with a single payer — we permit it.)
    assert res.acceleration_clause_triggered is False
    assert res.unauthorized_withdrawal_dispute is False


@pytest.mark.skipif(
    not _has_fixture(CLEAN_FIXTURE),
    reason="clean_profitable synthetic fixture not present",
)
def test_clean_fixture_ai_generated_score_below_strong_threshold() -> None:
    rows, ps, pe = _load(CLEAN_FIXTURE)
    res = analyze_patterns(rows, period_start=ps, period_end=pe)
    # Synthetic generator emits ALL-CAPS descriptions with embedded
    # trace numbers — should look real to the ai_generated heuristic.
    assert res.ai_generated_score < 85, (
        f"clean fixture flagged as AI-generated strongly (score="
        f"{res.ai_generated_score}); heuristic may need tuning"
    )
