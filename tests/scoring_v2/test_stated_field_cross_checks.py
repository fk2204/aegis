"""Track B — stated-application-field cross-check detectors.

Four new merchant-side detectors that fire on obvious application
contradictions and impossible ratios BEFORE the deal reaches an
underwriter:

* ``cc_sales_exceeds_revenue`` — CC sales > 115% of stated revenue
  (ELEVATED). Transplex-style case: $200K CC sales on $175K revenue.
* ``payment_load_critical``  — stated daily payment implies > 50%
  of stated revenue in monthly debt service (CRITICAL). TMF Transport
  case: $30K/day on $34,800/mo revenue.
* ``requested_amount_exceeds_capacity`` — requested / revenue > 5x
  (CRITICAL). Turnbull case: $4M requested on $107K/mo revenue.
* ``requested_amount_high_ratio``     — 3x < requested / revenue <= 5x
  (ELEVATED). Rendezvous case: $500K requested on $200K/mo (2.5x is
  under the threshold; verify the boundary).

Fixtures use ``SimpleNamespace`` cast to ``MerchantRow`` (same
pattern as ``tests/parser/test_application_vs_measured_detectors.py``)
so the tests stay decoupled from the Pydantic shape and only touch
the ``stated_*`` / ``avg_monthly_cc_sales`` / ``monthly_revenue`` /
``requested_amount`` fields the detectors read via ``getattr``. The
detectors fire on merchant data alone, so ``transactions_by_doc``
and ``classifications`` are both empty.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import cast

from aegis.merchants.models import MerchantRow
from aegis.scoring_v2.track_b import compute_risk_band


def _merchant(**stated_fields: object) -> MerchantRow:
    """Build a stand-in ``MerchantRow`` carrying only the intake-side
    attributes the four cross-check detectors read via ``getattr``.

    Duck-typed cast keeps this test independent of every other
    required column on the real Pydantic model; the detectors only
    touch the named fields.
    """
    return cast(MerchantRow, SimpleNamespace(**stated_fields))


def test_cc_sales_exceeds_revenue_fires_transplex_case() -> None:
    """Transplex-style contradiction: CC $200K/mo on stated revenue
    $150K/mo (33% excess -> well above the 15%-cushion floor).

    Expected: ``cc_sales_exceeds_revenue`` fires at ELEVATED. The
    business cannot have card-sales volume that exceeds its total
    revenue; the intake form is contradicting itself.
    """
    merchant = _merchant(
        monthly_revenue=Decimal("150000"),
        avg_monthly_cc_sales=Decimal("200000"),
    )

    result = compute_risk_band({}, {}, merchant=merchant)

    matches = [r for r in result.reasons if r.factor == "cc_sales_exceeds_revenue"]
    assert len(matches) == 1
    assert matches[0].severity == "elevated"
    assert "200,000" in matches[0].detail
    assert "150,000" in matches[0].detail


def test_payment_load_critical_fires_tmf_transport_case() -> None:
    """TMF Transport-style impossibility: $30K/day stated payment on
    $34,800/mo stated revenue.

    Implied monthly debt service = 30,000 * 21.7 = $651,000, which is
    ~1,870% of $34,800/mo revenue -- well above the 50% ceiling.

    Expected: ``payment_load_critical`` fires at CRITICAL.
    """
    merchant = _merchant(
        monthly_revenue=Decimal("34800"),
        stated_daily_payment=Decimal("30000"),
    )

    result = compute_risk_band({}, {}, merchant=merchant)

    matches = [r for r in result.reasons if r.factor == "payment_load_critical"]
    assert len(matches) == 1
    assert matches[0].severity == "critical"
    assert "30,000" in matches[0].detail
    assert "34,800" in matches[0].detail


def test_requested_amount_exceeds_capacity_fires_turnbull_case() -> None:
    """Turnbull-style ask-vs-capacity mismatch: $4M requested on
    $107K/mo stated revenue -> ratio ~= 37.4x (well above 5x).

    Expected: ``requested_amount_exceeds_capacity`` fires at CRITICAL.
    The high-ratio ELEVATED sibling must NOT also fire (mutually
    exclusive via the ``elif`` branch).
    """
    merchant = _merchant(
        monthly_revenue=Decimal("107000"),
        requested_amount=Decimal("4000000"),
    )

    result = compute_risk_band({}, {}, merchant=merchant)

    critical_matches = [
        r for r in result.reasons if r.factor == "requested_amount_exceeds_capacity"
    ]
    high_ratio_matches = [r for r in result.reasons if r.factor == "requested_amount_high_ratio"]
    assert len(critical_matches) == 1
    assert critical_matches[0].severity == "critical"
    assert len(high_ratio_matches) == 0
    assert "4,000,000" in critical_matches[0].detail


def test_requested_amount_high_ratio_fires_but_not_critical_rendezvous_case() -> None:
    """Rendezvous-style oversized ask: $500K requested on $200K/mo
    stated revenue -> ratio 2.5x. That's BELOW the 3x ELEVATED
    threshold, so the detector must stay quiet on this case.

    To confirm the ELEVATED band actually fires when it should, use a
    second merchant at 4x (still under the 5x CRITICAL cutoff).
    """
    # Rendezvous 2.5x -> nothing fires
    quiet_merchant = _merchant(
        monthly_revenue=Decimal("200000"),
        requested_amount=Decimal("500000"),
    )
    quiet_result = compute_risk_band({}, {}, merchant=quiet_merchant)
    assert not any(
        r.factor in ("requested_amount_high_ratio", "requested_amount_exceeds_capacity")
        for r in quiet_result.reasons
    )

    # 4x ratio -> ELEVATED fires, CRITICAL does NOT
    high_merchant = _merchant(
        monthly_revenue=Decimal("100000"),
        requested_amount=Decimal("400000"),
    )
    high_result = compute_risk_band({}, {}, merchant=high_merchant)

    elevated_matches = [r for r in high_result.reasons if r.factor == "requested_amount_high_ratio"]
    critical_matches = [
        r for r in high_result.reasons if r.factor == "requested_amount_exceeds_capacity"
    ]
    assert len(elevated_matches) == 1
    assert elevated_matches[0].severity == "elevated"
    assert len(critical_matches) == 0
    assert "400,000" in elevated_matches[0].detail
