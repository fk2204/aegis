"""Track B — Business Risk Band — REAL VU acceptance test.

CLAUDE.md "external-integration test discipline": fixture is the
same captured VU bundle used by the counterparty foundation and
Track C. Same denominator, same patterns, same bytes.

Acceptance grades:

* The band lands where the cashflow signals say it should — not at
  some opaque score, but at the worst per-factor severity.
* Each band landing is **explainable** — reasons list names the
  specific factors and what severity they fired with.
* Track B reads the SAME revenue basis as Track C (the counterparty
  foundation pay-off).
* The schema has NO decline field (structural guard against future
  accidental wiring).
* International concentration is a band-MODIFYING factor in Track B,
  capped at ``elevated`` — never ``critical`` — because Track C's
  reframe says concentration is a durability question, not a fraud
  signal.

Plus targeted unit tests on each severity function so the threshold
rationale is documented in test form.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from aegis.counterparty import classify_bundle
from aegis.money import Money
from aegis.parser.models import ClassifiedTransaction
from aegis.scoring_v2.track_b import (
    BAND_TO_ACTION,
    BusinessRiskBand,
    compute_risk_band,
)
from aegis.scoring_v2.track_b.banding import (
    severity_for_international_concentration,
    severity_for_lowest_balance,
    severity_for_mca_positions,
    severity_for_monthly_revenue,
    severity_for_negative_days,
    severity_for_nsf,
)

_FIXTURE_PATH = (
    Path(__file__).parent.parent.parent
    / "counterparty"
    / "fixtures"
    / "vu_real_txns.json"
)


def _load_fixture() -> dict[str, Any]:
    data = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _build_bundle() -> tuple[dict[str, list[ClassifiedTransaction]], set[str]]:
    fixture = _load_fixture()
    by_doc: dict[str, list[ClassifiedTransaction]] = {}
    accts: set[str] = set()
    for doc in fixture["documents"]:
        last4 = doc["summary"]["account_last4"]
        if last4:
            accts.add(last4)
        txns: list[ClassifiedTransaction] = []
        for t in doc["transactions"]:
            running = t.get("running_balance")
            txns.append(
                ClassifiedTransaction(
                    id=UUID(t["id"]),
                    posted_date=date.fromisoformat(t["posted_date"]),
                    description=t["description"],
                    amount=Decimal(t["amount"]),
                    running_balance=(
                        Decimal(running) if running is not None else None
                    ),
                    source_page=1,
                    source_line=1,
                    category=t["category"],
                    classification_confidence=100,
                )
            )
        by_doc[doc["document_id"]] = txns
    return by_doc, accts


@pytest.fixture(scope="module")
def vu_band() -> BusinessRiskBand:
    by_doc, accts = _build_bundle()
    classifications, _ = classify_bundle(by_doc, accts)
    return compute_risk_band(by_doc, classifications)


# ─────────────────────────────────────────────────────────────────────
# Headline acceptance: band lands explainably, not opaquely
# ─────────────────────────────────────────────────────────────────────


def test_vu_band_is_elevated_driven_by_international_concentration(
    vu_band: BusinessRiskBand,
) -> None:
    """VU's cashflow is strong (no NSFs, no detected MCA, strong
    monthly revenue) but 89% of revenue is from one counterparty
    class — above the elevated floor (60%). International
    concentration is the band-driver. **Never** ``high`` — Track C
    explicitly caps concentration at ``elevated`` because international
    wires are revenue, not fraud.
    """
    # Specific band landing on the captured fixture.
    assert vu_band.band == "elevated"
    # Both moderate and elevated map to review_neutral per Q1; the
    # band is the finer signal.
    assert vu_band.action == "review_neutral"

    # The driving factor is concentration; it's the worst severity
    # observed AND it appears first in the reasons list.
    assert vu_band.reasons[0].factor == "international_concentration"
    assert vu_band.reasons[0].severity == "elevated"
    # Concentration framing names the durability question — not fraud.
    detail = vu_band.reasons[0].detail.lower()
    assert "durability" in detail
    assert "not a fraud signal" in detail


def test_vu_reasons_list_is_explainable(vu_band: BusinessRiskBand) -> None:
    """Every factor that contributed to the band is named, with a
    severity and a one-line detail. The list MUST cover the factors
    we computed; missing-data factors live elsewhere."""
    factors = [r.factor for r in vu_band.reasons]
    # Always-computable factors must be present.
    assert "true_revenue" in factors
    assert "nsf" in factors
    assert "mca_positions" in factors
    assert "international_concentration" in factors
    # Each reason has non-empty detail.
    for r in vu_band.reasons:
        assert r.detail.strip()


def test_vu_cashflow_signals_match_aggregation(
    vu_band: BusinessRiskBand,
) -> None:
    """The same revenue basis Track C uses. Foundation pay-off."""
    assert vu_band.cashflow.true_revenue_total == Decimal("364280.05")
    # Monthly revenue is normalised: $364K / period_days * 30.
    # Period is ~119 days (Feb 1 → May 31 inclusive); monthly ≈ $91K.
    assert vu_band.cashflow.monthly_revenue_estimate >= Money(Decimal("90000"))
    assert vu_band.cashflow.monthly_revenue_estimate < Money(Decimal("100000"))


def test_vu_no_nsfs_detected_on_the_bundle(vu_band: BusinessRiskBand) -> None:
    """VU's parsed categories don't include any nsf_fee. The signal
    fires as positive and the reason says so explicitly."""
    assert vu_band.cashflow.nsf_count == 0
    nsf_reasons = [r for r in vu_band.reasons if r.factor == "nsf"]
    assert len(nsf_reasons) == 1
    assert nsf_reasons[0].severity == "positive"
    assert "no nsf fees observed" in nsf_reasons[0].detail.lower()


def test_vu_no_mca_positions_detected_on_the_bundle(
    vu_band: BusinessRiskBand,
) -> None:
    """No mca_debit categories in VU's bundle → positive signal +
    explicit detail framing for what 'no detected' means (parser
    didn't see; operator still cross-references)."""
    assert vu_band.cashflow.mca_position_count == 0
    mca_reasons = [r for r in vu_band.reasons if r.factor == "mca_positions"]
    assert len(mca_reasons) == 1
    assert mca_reasons[0].severity == "positive"
    assert "no mca debit" in mca_reasons[0].detail.lower()


def test_vu_running_balance_is_sparse_so_adb_is_insufficient(
    vu_band: BusinessRiskBand,
) -> None:
    """Running_balance is on ~10% of VU's rows — below the 25%
    coverage floor. ADB / lowest_balance are None and the factors
    appear in the insufficient_data list rather than silently
    fabricated."""
    assert vu_band.cashflow.average_daily_balance is None
    assert vu_band.cashflow.lowest_balance is None
    insufficient = set(vu_band.insufficient_data_factors)
    assert "average_daily_balance" in insufficient
    assert "lowest_balance" in insufficient


# ─────────────────────────────────────────────────────────────────────
# Structural guard: no decline gate (the load-bearing safety check)
# ─────────────────────────────────────────────────────────────────────


def test_band_has_no_decline_or_score_field() -> None:
    """Track B is additive. Schema MUST NOT carry a field that wires
    into the live decline path. Mirrors the Track C guard.
    """
    fields = set(BusinessRiskBand.model_fields)
    forbidden = {
        "decline",
        "auto_decline",
        "risk_score",
        "fraud_score",
        "score",
        "verdict",
        "outcome",
    }
    leaked = fields & forbidden
    assert not leaked, (
        f"Track B output must not carry decline/score fields; leaked: {leaked}"
    )


# ─────────────────────────────────────────────────────────────────────
# Band → action mapping
# ─────────────────────────────────────────────────────────────────────


def test_band_to_action_mapping_matches_q1_decision() -> None:
    """The Q1-decided mapping (per SCORING_REDESIGN_CONTINUATION.md)."""
    assert BAND_TO_ACTION["low"]      == "auto_forward"
    assert BAND_TO_ACTION["moderate"] == "review_neutral"
    assert BAND_TO_ACTION["elevated"] == "review_neutral"
    assert BAND_TO_ACTION["high"]     == "review_decline_default"


# ─────────────────────────────────────────────────────────────────────
# Severity-threshold unit tests (each is the rationale-in-tests)
# ─────────────────────────────────────────────────────────────────────


def test_severity_for_monthly_revenue_thresholds() -> None:
    assert severity_for_monthly_revenue(Money(Decimal("0"))) == "critical"
    assert severity_for_monthly_revenue(Money(Decimal("5000"))) == "elevated"
    assert severity_for_monthly_revenue(Money(Decimal("15000"))) == "concern"
    assert severity_for_monthly_revenue(Money(Decimal("40000"))) == "neutral"
    assert severity_for_monthly_revenue(Money(Decimal("100000"))) == "positive"


def test_severity_for_nsf_is_normalised_to_monthly_rate() -> None:
    # 4 NSFs over 30 days = ~4/mo → elevated.
    assert severity_for_nsf(4, 30) == "elevated"
    # 4 NSFs over 120 days = ~1/mo → concern.
    assert severity_for_nsf(4, 120) == "concern"
    # 0 NSFs → positive regardless of period.
    assert severity_for_nsf(0, 90) == "positive"


def test_severity_for_mca_positions_thresholds() -> None:
    """Stacking is the load-bearing risk signal. Even one position
    is flagged; multiple are elevated; heavy is critical."""
    assert severity_for_mca_positions(0) == "positive"
    assert severity_for_mca_positions(1) == "concern"
    assert severity_for_mca_positions(2) == "elevated"
    assert severity_for_mca_positions(4) == "elevated"
    assert severity_for_mca_positions(5) == "critical"


def test_severity_for_lowest_balance_handles_negative_depth() -> None:
    assert severity_for_lowest_balance(Money(Decimal("1500"))) == "positive"
    assert severity_for_lowest_balance(Money(Decimal("-500"))) == "neutral"
    assert severity_for_lowest_balance(Money(Decimal("-3000"))) == "concern"
    assert severity_for_lowest_balance(Money(Decimal("-10000"))) == "elevated"
    assert severity_for_lowest_balance(Money(Decimal("-20000"))) == "critical"


def test_severity_for_negative_days_thresholds() -> None:
    assert severity_for_negative_days(0) == "positive"
    assert severity_for_negative_days(2) == "neutral"
    assert severity_for_negative_days(6) == "concern"
    assert severity_for_negative_days(15) == "elevated"


def test_severity_for_international_concentration_caps_at_elevated() -> None:
    """The Track-C reframe — international wires aren't fraud — caps
    this factor at ``elevated``. Nothing can be ``critical`` from
    concentration alone."""
    assert severity_for_international_concentration(Decimal("20")) == "neutral"
    assert severity_for_international_concentration(Decimal("40")) == "concern"
    assert severity_for_international_concentration(Decimal("75")) == "elevated"
    # Even at 100% the cap holds.
    assert severity_for_international_concentration(Decimal("100")) == "elevated"
