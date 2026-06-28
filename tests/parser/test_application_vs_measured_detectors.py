"""Unit tests for the application-vs-measured reality detectors.

Covers ``detect_impossible_payment_load`` and
``detect_stated_vs_measured_revenue_divergence`` in
``aegis.parser.patterns``.

The real "Vibration Guys" case grounds the impossible-payment-load
fixture: merchant stated $125K/day on $120K/month revenue ->
$2.75M/month implied payment load on $120K/month revenue (~2,290%).

Both detectors read merchant fields via ``getattr`` so the test
fixtures use ``SimpleNamespace`` — keeps the tests decoupled from
``MerchantRow`` Pydantic shape (Agent 2's parallel work adds the
``stated_*`` fields). Adding the fields to MerchantRow later does not
invalidate any of these tests.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import cast

from aegis.merchants.models import MerchantRow
from aegis.parser.patterns import (
    detect_impossible_payment_load,
    detect_stated_vs_measured_revenue_divergence,
)


def _merchant(**stated_fields: object) -> MerchantRow:
    """Tiny test fixture: builds a stand-in for ``MerchantRow`` carrying
    only the ``stated_*`` attrs the detector reads via ``getattr``.

    Returning a ``SimpleNamespace`` cast to ``MerchantRow`` keeps the
    test independent of (a) Agent 2's parallel work on the Pydantic
    field definitions and (b) every other required column on the real
    model. The detectors only touch the named fields, so duck typing
    is the correct contract here.
    """
    return cast(MerchantRow, SimpleNamespace(**stated_fields))


# ─────────────────────────────────────────────────────────────────────
# detect_impossible_payment_load
# ─────────────────────────────────────────────────────────────────────


def test_impossible_payment_load_fires_on_vibration_guys_case() -> None:
    """Real case: $125K/day stated on $120K/month measured revenue.

    Implied monthly debit = $125,000 * 22 = $2,750,000.
    Threshold = $120,000 * 1.5 = $180,000.
    Detector MUST fire (severity 85, code = impossible_payment_load).
    """
    merchant = _merchant(stated_daily_payment=Decimal("125000"))
    true_revenue_monthly = Decimal("120000")

    pat = detect_impossible_payment_load(merchant, true_revenue_monthly)

    assert pat is not None
    assert pat.code == "impossible_payment_load"
    assert pat.severity == 85
    assert "125,000" in pat.detail
    assert "2,750,000" in pat.detail
    assert "120,000" in pat.detail
    assert "insolvent" in pat.detail.lower()


def test_impossible_payment_load_does_not_fire_below_threshold() -> None:
    """Stated daily $1,000 on $50K/month revenue.

    Implied monthly = $22,000; threshold = $75,000. Below the 150%
    line — detector should stay quiet so legitimate small-payment
    deals aren't flagged.
    """
    merchant = _merchant(stated_daily_payment=Decimal("1000"))
    true_revenue_monthly = Decimal("50000")

    assert detect_impossible_payment_load(merchant, true_revenue_monthly) is None


def test_impossible_payment_load_fires_just_above_threshold() -> None:
    """Boundary case: implied monthly slightly above 1.5 x revenue
    SHOULD fire. Picks a daily payment whose implied monthly is exactly
    51% above true revenue ($510 daily * 22 = $11,220 vs $7,400 revenue
    * 1.5 = $11,100 threshold)."""
    merchant = _merchant(stated_daily_payment=Decimal("510"))
    true_revenue_monthly = Decimal("7400")

    pat = detect_impossible_payment_load(merchant, true_revenue_monthly)

    assert pat is not None
    assert pat.code == "impossible_payment_load"


def test_impossible_payment_load_does_not_fire_at_or_just_below_150pct() -> None:
    """Exactly 150% should not fire (boundary is strict ``>``)."""
    # $500/day * 22 = $11,000 implied = exactly 150% of $7,333.33...
    # Use $10,000 revenue, $681.81/day -> $14,999.82, threshold $15,000.
    merchant = _merchant(stated_daily_payment=Decimal("681.81"))
    true_revenue_monthly = Decimal("10000")

    assert detect_impossible_payment_load(merchant, true_revenue_monthly) is None


def test_impossible_payment_load_returns_none_on_missing_stated_field() -> None:
    """Pre-Agent-2 merchant has no ``stated_daily_payment`` attribute.

    ``getattr(..., None)`` returns ``None`` and the detector skips.
    """
    merchant = _merchant()  # no stated_daily_payment attribute
    assert detect_impossible_payment_load(merchant, Decimal("120000")) is None


def test_impossible_payment_load_returns_none_on_none_stated() -> None:
    """``stated_daily_payment`` exists but is None — also skips."""
    merchant = _merchant(stated_daily_payment=None)
    assert detect_impossible_payment_load(merchant, Decimal("120000")) is None


def test_impossible_payment_load_returns_none_on_zero_or_negative_revenue() -> None:
    """Zero / negative measured revenue — division-by-zero guard."""
    merchant = _merchant(stated_daily_payment=Decimal("125000"))

    assert detect_impossible_payment_load(merchant, Decimal("0")) is None
    assert detect_impossible_payment_load(merchant, Decimal("-100")) is None


def test_impossible_payment_load_returns_none_on_zero_stated() -> None:
    """Stated daily of zero — no signal."""
    merchant = _merchant(stated_daily_payment=Decimal("0"))
    assert detect_impossible_payment_load(merchant, Decimal("120000")) is None


# ─────────────────────────────────────────────────────────────────────
# detect_stated_vs_measured_revenue_divergence
# ─────────────────────────────────────────────────────────────────────


def test_revenue_divergence_fires_on_50pct_overstatement() -> None:
    """Application says $150K/month, statements show $80K/month.

    Divergence = |150,000 - 80,000| / 80,000 = 87.5% > 40% threshold.
    """
    merchant = _merchant(monthly_revenue=Decimal("150000"))
    measured = Decimal("80000")

    pat = detect_stated_vs_measured_revenue_divergence(merchant, measured)

    assert pat is not None
    assert pat.code == "stated_vs_measured_revenue_divergence"
    assert pat.severity == 60
    assert "150,000" in pat.detail
    assert "80,000" in pat.detail
    assert "verify" in pat.detail.lower()


def test_revenue_divergence_fires_on_understatement() -> None:
    """Application understates revenue — also a flag (audit trail).

    Stated $40K, measured $80K -> 50% divergence (under). Detector
    uses absolute divergence so this fires the same as overstatement.
    """
    merchant = _merchant(monthly_revenue=Decimal("40000"))
    measured = Decimal("80000")

    pat = detect_stated_vs_measured_revenue_divergence(merchant, measured)
    assert pat is not None
    assert pat.severity == 60


def test_revenue_divergence_does_not_fire_below_40pct() -> None:
    """30% divergence — within statement-period / seasonal variance."""
    merchant = _merchant(monthly_revenue=Decimal("130000"))
    measured = Decimal("100000")

    # |130k - 100k| / 100k = 30%, below 40% threshold
    assert detect_stated_vs_measured_revenue_divergence(merchant, measured) is None


def test_revenue_divergence_does_not_fire_at_exactly_40pct() -> None:
    """Boundary: exactly 40% should not fire (strict ``>``)."""
    merchant = _merchant(monthly_revenue=Decimal("140000"))
    measured = Decimal("100000")

    assert detect_stated_vs_measured_revenue_divergence(merchant, measured) is None


def test_revenue_divergence_fires_just_above_40pct() -> None:
    """Boundary: 41% divergence SHOULD fire."""
    merchant = _merchant(monthly_revenue=Decimal("141000"))
    measured = Decimal("100000")

    pat = detect_stated_vs_measured_revenue_divergence(merchant, measured)
    assert pat is not None


def test_revenue_divergence_returns_none_on_missing_stated() -> None:
    """Pre-Agent-2 merchant has no ``monthly_revenue`` attribute."""
    merchant = _merchant()
    assert detect_stated_vs_measured_revenue_divergence(merchant, Decimal("100000")) is None


def test_revenue_divergence_returns_none_on_none_stated() -> None:
    merchant = _merchant(monthly_revenue=None)
    assert detect_stated_vs_measured_revenue_divergence(merchant, Decimal("100000")) is None


def test_revenue_divergence_returns_none_on_zero_or_negative_measured() -> None:
    """Division-by-zero / sign-flip guard on measured revenue."""
    merchant = _merchant(monthly_revenue=Decimal("100000"))

    assert detect_stated_vs_measured_revenue_divergence(merchant, Decimal("0")) is None
    assert detect_stated_vs_measured_revenue_divergence(merchant, Decimal("-100")) is None


def test_revenue_divergence_returns_none_on_zero_stated() -> None:
    merchant = _merchant(monthly_revenue=Decimal("0"))
    assert detect_stated_vs_measured_revenue_divergence(merchant, Decimal("100000")) is None


# ─────────────────────────────────────────────────────────────────────
# Track B integration — the detectors flow into the reasons list
# ─────────────────────────────────────────────────────────────────────


def test_compute_risk_band_surfaces_impossible_payment_load_as_critical_reason() -> None:
    """Wiring smoke-test: when ``merchant`` is threaded through
    ``compute_risk_band`` and the merchant's stated daily payment
    crosses the insolvency threshold, the band's ``reasons`` MUST
    include a ``critical`` row with factor ``impossible_payment_load``.

    Uses the real "Vibration Guys" shape: stated $125K/day payment on
    bank-measured revenue ~$120K/month. To get a $120K monthly
    estimate from the bundle we deposit $120K spread over a 30-day
    period (one deposit per period boundary -> Track B normalises to
    $120K/month).
    """
    from datetime import date
    from uuid import uuid4

    from aegis.counterparty.models import CounterpartyClassification
    from aegis.parser.models import ClassifiedTransaction
    from aegis.scoring_v2.track_b import compute_risk_band

    early = ClassifiedTransaction(
        id=uuid4(),
        posted_date=date(2026, 2, 1),
        description="ACME CUSTOMER WIRE",
        amount=Decimal("60000.00"),
        running_balance=Decimal("60000.00"),
        source_page=1,
        source_line=1,
        category="wire_in",
        classification_confidence=98,
    )
    late = ClassifiedTransaction(
        id=uuid4(),
        posted_date=date(2026, 3, 1),
        description="ACME CUSTOMER WIRE",
        amount=Decimal("60000.00"),
        running_balance=Decimal("120000.00"),
        source_page=1,
        source_line=2,
        category="wire_in",
        classification_confidence=98,
    )
    txns = {"doc_1": [early, late]}
    classifs = {
        early.id: CounterpartyClassification(
            transaction_id=early.id,
            counterparty="end_customer",
            confidence=98,
            reason="customer wire",
        ),
        late.id: CounterpartyClassification(
            transaction_id=late.id,
            counterparty="end_customer",
            confidence=98,
            reason="customer wire",
        ),
    }
    merchant = _merchant(stated_daily_payment=Decimal("125000"))

    band = compute_risk_band(txns, classifs, merchant=merchant)

    critical_factors = [r.factor for r in band.reasons if r.severity == "critical"]
    assert "impossible_payment_load" in critical_factors
    # Highest-severity reason must be the impossible-payment-load reason
    # (sort order puts the critical row at the top).
    assert band.reasons[0].factor == "impossible_payment_load"
    assert band.band == "high"
    assert band.action == "review_decline_default"


def test_compute_risk_band_omits_application_reality_reasons_when_merchant_none() -> None:
    """Baseline: legacy callers don't pass a merchant -> the two
    application-vs-measured factors never appear in ``reasons``."""
    from datetime import date
    from uuid import uuid4

    from aegis.counterparty.models import CounterpartyClassification
    from aegis.parser.models import ClassifiedTransaction
    from aegis.scoring_v2.track_b import compute_risk_band

    deposit = ClassifiedTransaction(
        id=uuid4(),
        posted_date=date(2026, 2, 14),
        description="ACME CUSTOMER WIRE",
        amount=Decimal("60000.00"),
        running_balance=Decimal("60000.00"),
        source_page=1,
        source_line=1,
        category="wire_in",
        classification_confidence=98,
    )
    txns = {"doc_1": [deposit]}
    classifs = {
        deposit.id: CounterpartyClassification(
            transaction_id=deposit.id,
            counterparty="end_customer",
            confidence=98,
            reason="customer wire",
        ),
    }

    band = compute_risk_band(txns, classifs)  # no merchant kwarg

    factors = {r.factor for r in band.reasons}
    assert "impossible_payment_load" not in factors
    assert "stated_vs_measured_revenue_divergence" not in factors
