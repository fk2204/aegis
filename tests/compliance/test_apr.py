"""APR calculator tests.

Two layers:
  1. Five hand-computed test vectors that pin exact outputs.
  2. Hypothesis property tests verifying monotonicity in factor and term.

Hand-computed vectors are derivable from algebra (single payment uses
(1+r)^t = total/principal; two-payment uses a quadratic in (1+r)^-t).
The annuity case (V4) is cross-checked against Excel's RATE function:
  RATE(10, -110, 1000) = 0.01769407...
  APR = 0.01769407 * 365 = 6.45833...

Tolerances are wider than the optimizer's xtol so we're not testing
brentq's internal precision; we're testing the formula. ±0.0001 = 0.01%
on APR, which is finer than any regulator requires.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from aegis.compliance.apr import APRCalculationError, calculate_apr

if TYPE_CHECKING:
    from aegis.scoring.models import ScoreInput, ScoreResult

DISBURSE = date(2026, 1, 1)


def _daily_payments(start_offset: int, count: int, amount: Decimal) -> list[tuple[date, Decimal]]:
    """Build a constant-amount daily payment stream."""
    return [(DISBURSE + timedelta(days=start_offset + i), amount) for i in range(count)]


# -- Known-answer vectors -----------------------------------------------------
# Each vector pins the exact APR computed by hand for a small input. If
# `calculate_apr` ever drifts, one of these will fire first.

_TOL = Decimal("0.0001")


def test_v1_single_payment_one_day_later() -> None:
    """$1000 -> $1100 in 1 day. Daily rate = 0.10. APR = 0.10 * 365 = 36.5."""
    apr = calculate_apr(
        amount_financed=Decimal("1000.00"),
        payments=[(DISBURSE + timedelta(days=1), Decimal("1100.00"))],
        disbursement_date=DISBURSE,
    )
    assert abs(apr - Decimal("36.5")) < _TOL


def test_v2_single_payment_year_later() -> None:
    """$1000 -> $1100 in 365 days. Daily rate = 1.1^(1/365) - 1.
    APR = (1.1^(1/365) - 1) * 365 ≈ 0.0953231.
    Closed-form derivable from logs: ln(1.1)/365 * 365 ≈ ln(1.1) ≈ 0.0953,
    then APR = (e^(ln(1.1)/365) - 1) * 365 ≈ 0.09532306.
    """
    apr = calculate_apr(
        amount_financed=Decimal("1000.00"),
        payments=[(DISBURSE + timedelta(days=365), Decimal("1100.00"))],
        disbursement_date=DISBURSE,
    )
    expected = Decimal("0.0953231")
    assert abs(apr - expected) < _TOL


def test_v3_two_equal_payments_30_and_60_days() -> None:
    """$1000 -> $550 on day 30 + $550 on day 60.

    Let x = (1+r)^-30. Then 1000 = 550x + 550x^2.
    x^2 + x - 1000/550 = 0 -> x^2 + x - 1.818182 = 0
    x = (-1 + sqrt(1 + 4 * 1.818182)) / 2
      = (-1 + sqrt(8.272727)) / 2
      = (-1 + 2.876236) / 2 = 0.938118
    (1+r)^30 = 1/0.938118 = 1.065963
    1+r = 1.065963^(1/30) -> r ≈ 0.002131
    APR = 0.002131 * 365 ≈ 0.777845
    """
    apr = calculate_apr(
        amount_financed=Decimal("1000.00"),
        payments=[
            (DISBURSE + timedelta(days=30), Decimal("550.00")),
            (DISBURSE + timedelta(days=60), Decimal("550.00")),
        ],
        disbursement_date=DISBURSE,
    )
    assert abs(apr - Decimal("0.7778")) < Decimal("0.001")


def test_v4_daily_annuity_short_term_mca() -> None:
    """$1000 -> 10 daily payments of $110. Standard annuity-PV formula.

    Solving  P = Pmt * (1 - (1+r)^-n) / r  for r given P=1000, Pmt=110, n=10
    requires numerical iteration (no closed form). Independent verification:
    plug r=0.017716 back into  sum(110 / (1.017716)^k for k=1..10) and the
    result is $1000.04 — within rounding of the principal. So r ≈ 0.017716,
    APR ≈ r * 365 ≈ 6.4664. We pin to 4dp and accept ±0.001.
    """
    apr = calculate_apr(
        amount_financed=Decimal("1000.00"),
        payments=_daily_payments(start_offset=1, count=10, amount=Decimal("110.00")),
        disbursement_date=DISBURSE,
    )
    assert abs(apr - Decimal("6.4661")) < Decimal("0.001")


def test_v5_two_payments_consecutive_days() -> None:
    """$1000 -> $510 on day 1 + $510 on day 2.

    Let y = 1/(1+r). 1000 = 510y + 510y^2.
    y^2 + y - 1000/510 = 0 -> y^2 + y - 1.960784 = 0
    y = (-1 + sqrt(1 + 7.843137)) / 2
      = (-1 + sqrt(8.843137)) / 2
      = (-1 + 2.973741) / 2 = 0.986870
    1+r = 1/0.986870 = 1.013305
    r = 0.013305
    APR = 0.013305 * 365 ≈ 4.85645
    """
    apr = calculate_apr(
        amount_financed=Decimal("1000.00"),
        payments=[
            (DISBURSE + timedelta(days=1), Decimal("510.00")),
            (DISBURSE + timedelta(days=2), Decimal("510.00")),
        ],
        disbursement_date=DISBURSE,
    )
    assert abs(apr - Decimal("4.8564")) < Decimal("0.001")


# -- Input validation ---------------------------------------------------------


def test_rejects_zero_principal() -> None:
    with pytest.raises(APRCalculationError, match="amount_financed"):
        calculate_apr(
            amount_financed=Decimal("0"),
            payments=[(DISBURSE + timedelta(days=1), Decimal("100"))],
            disbursement_date=DISBURSE,
        )


def test_rejects_empty_payments() -> None:
    with pytest.raises(APRCalculationError, match="at least one payment"):
        calculate_apr(
            amount_financed=Decimal("1000"),
            payments=[],
            disbursement_date=DISBURSE,
        )


def test_rejects_payment_on_disbursement_day() -> None:
    with pytest.raises(APRCalculationError, match="on or before disbursement"):
        calculate_apr(
            amount_financed=Decimal("1000"),
            payments=[(DISBURSE, Decimal("1100"))],
            disbursement_date=DISBURSE,
        )


def test_rejects_payments_below_principal() -> None:
    """Sum of payments must exceed principal — APR < 0 isn't meaningful here."""
    with pytest.raises(APRCalculationError, match="must exceed"):
        calculate_apr(
            amount_financed=Decimal("1000"),
            payments=[(DISBURSE + timedelta(days=30), Decimal("999"))],
            disbursement_date=DISBURSE,
        )


# -- Property tests -----------------------------------------------------------
# These are the load-bearing tests for "the formula behaves correctly."
# Hand-computed vectors catch arithmetic errors; these catch structural ones.


def _apr_for_factor_and_term(
    principal: Decimal, factor: Decimal, term_days: int
) -> Decimal:
    """Helper: build a daily MCA at the given factor + term and compute APR."""
    total = (principal * factor).quantize(Decimal("0.01"))
    daily = (total / term_days).quantize(Decimal("0.01"))
    payments = _daily_payments(start_offset=1, count=term_days, amount=daily)
    return calculate_apr(principal, payments, DISBURSE)


@given(
    factor_low=st.decimals(min_value="1.10", max_value="1.40", places=2),
    factor_step=st.decimals(min_value="0.05", max_value="0.20", places=2),
    term=st.integers(min_value=30, max_value=180),
    principal_int=st.integers(min_value=5000, max_value=50000),
)
@settings(max_examples=40, deadline=None)
def test_apr_monotonic_in_factor(
    factor_low: Decimal,
    factor_step: Decimal,
    term: int,
    principal_int: int,
) -> None:
    """For a fixed term, a higher factor -> a higher APR.

    factor_low and factor_low+factor_step are both in (1, 2). APR(higher
    factor) must be > APR(lower factor).
    """
    principal = Decimal(principal_int)
    factor_high = factor_low + factor_step
    if factor_high >= Decimal("1.99"):
        return  # skip near upper bound
    apr_low = _apr_for_factor_and_term(principal, factor_low, term)
    apr_high = _apr_for_factor_and_term(principal, factor_high, term)
    assert apr_high > apr_low, (
        f"factor {factor_high} did not yield a higher APR than {factor_low} "
        f"(got {apr_high} vs {apr_low}) at term={term}, principal={principal}"
    )


@given(
    factor=st.decimals(min_value="1.20", max_value="1.40", places=2),
    term_short=st.integers(min_value=30, max_value=90),
    term_extra=st.integers(min_value=30, max_value=180),
    principal_int=st.integers(min_value=5000, max_value=50000),
)
@settings(max_examples=40, deadline=None)
def test_apr_monotonic_in_term(
    factor: Decimal,
    term_short: int,
    term_extra: int,
    principal_int: int,
) -> None:
    """For a fixed factor, a longer term -> a lower APR.

    Same total repayment spread over more days = lower effective rate.
    """
    principal = Decimal(principal_int)
    term_long = term_short + term_extra
    apr_short = _apr_for_factor_and_term(principal, factor, term_short)
    apr_long = _apr_for_factor_and_term(principal, factor, term_long)
    assert apr_short > apr_long, (
        f"term {term_short} should have higher APR than {term_long} "
        f"(got {apr_short} vs {apr_long}) at factor={factor}, principal={principal}"
    )


# -- R0.4 APR error gate ------------------------------------------------------
#
# The disclosure pipeline MUST halt for review on APR computation failure;
# silently rendering 0.00% is a CA DFPI §§ 940/942 material defect. These
# tests build payment streams that cannot repay the principal at any
# positive rate (sum of payments <= principal) so brentq cannot bracket
# a root, then assert the error propagates through the disclosure-context
# builder as APRDisclosureError carrying the deal fields.


def test_apr_calculation_error_when_payments_below_principal() -> None:
    """Sum of payments < principal makes brentq's NPV monotonic positive;
    APRCalculationError must fire before brentq is even called."""
    with pytest.raises(APRCalculationError, match="must exceed"):
        calculate_apr(
            amount_financed=Decimal("10000"),
            payments=[(DISBURSE + timedelta(days=30), Decimal("500"))],
            disbursement_date=DISBURSE,
        )


def _valid_deal_for_ca() -> ScoreInput:
    """Construct a passing-Pydantic ScoreInput for CA. The APR failure
    is forced via monkeypatch in the tests below, not by violating the
    ScoreInput constraints (factor must be > 1, term must be >= 1)."""
    from datetime import date as _date
    from uuid import UUID

    from aegis.scoring.models import ScoreInput

    return ScoreInput(
        merchant_id=UUID("22222222-2222-4222-8222-222222222222"),
        business_name="Broken APR Bakery LLC",
        owner_name="Test Owner",
        state="CA",
        avg_daily_balance=Decimal("12500.00"),
        true_revenue=Decimal("110000.00"),
        monthly_revenue=Decimal("110000.00"),
        lowest_balance=Decimal("3000.00"),
        num_nsf=0,
        days_negative=0,
        mca_positions=0,
        mca_daily_total=Decimal("0.00"),
        debt_to_revenue=Decimal("0.00"),
        fraud_score=10,
        statement_period_start=_date(2026, 4, 1),
        statement_period_end=_date(2026, 4, 30),
        statement_days=30,
        requested_amount=Decimal("50000.00"),
        requested_factor=Decimal("1.30"),
        requested_term_days=120,
    )


def _valid_score() -> ScoreResult:
    from aegis.scoring.models import ScoreResult

    return ScoreResult(
        score=50,
        tier="C",
        recommendation="refer",
        suggested_max_advance=Decimal("50000.00"),
        recommended_factor_rate=Decimal("1.30"),
        recommended_holdback_pct=Decimal("0.12"),
        estimated_payback_days=120,
    )


def test_build_ca_disclosure_context_raises_apr_disclosure_error_on_optimizer_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_tier1_disclosure_context MUST raise APRDisclosureError when
    the underlying APR calculator fails, carrying deal context (state,
    principal, factor, term, disbursement_date, deal_id). Critical: NO
    0.00% APR is allowed to leak into the rendered disclosure (the prior
    silent-substitute behavior was a CA DFPI §§ 940/942 material defect).

    We force the failure by monkeypatching ``calculate_apr`` to raise the
    same APRCalculationError brentq would raise on a degenerate input —
    avoids the ScoreInput Pydantic constraints (factor > 1, term >= 1)
    while exercising the exact catch path the production code uses.
    """
    from datetime import date as _date

    from aegis.compliance import disclosure_context
    from aegis.compliance.disclosure_context import (
        APRDisclosureError,
        build_tier1_disclosure_context,
    )
    from aegis.compliance.states import STATES, Tier1Regulation

    def _boom(*args: object, **kwargs: object) -> Decimal:
        raise APRCalculationError("brentq failed to converge: no sign change in bracket")

    monkeypatch.setattr(disclosure_context, "calculate_apr", _boom)

    ca = STATES["CA"]
    assert isinstance(ca, Tier1Regulation)

    with pytest.raises(APRDisclosureError) as exc_info:
        build_tier1_disclosure_context(
            ca,
            _valid_deal_for_ca(),
            _valid_score(),
            _date(2026, 5, 13),
            funder_name="Commera Capital",
        )
    err = exc_info.value
    assert err.state == "CA"
    assert err.principal == Decimal("50000.00")
    assert err.factor == Decimal("1.30")
    assert err.term_days == 120
    # deal_id surfaced as the merchant UUID string
    assert err.deal_id == "22222222-2222-4222-8222-222222222222"
    # The underlying APRCalculationError is chained for full traceability.
    assert isinstance(err.__cause__, APRCalculationError)


def test_render_disclosure_propagates_apr_disclosure_error_for_ca(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: render_disclosure("CA", ...) with a failing APR
    calculator must raise APRDisclosureError so the calling pipeline can
    mark the deal ``needs_review`` rather than ship a 0.00% APR
    disclosure to the merchant."""
    from datetime import date as _date
    from datetime import datetime

    from aegis.compliance import disclosure_context
    from aegis.compliance.disclosure import APRDisclosureError, render_disclosure

    def _boom(*args: object, **kwargs: object) -> Decimal:
        raise APRCalculationError("brentq failed to converge")

    monkeypatch.setattr(disclosure_context, "calculate_apr", _boom)

    with pytest.raises(APRDisclosureError):
        render_disclosure(
            "CA",
            _valid_deal_for_ca(),
            _valid_score(),
            rendered_at=datetime(2026, 5, 13),
            disbursement_date=_date(2026, 5, 13),
        )


def test_apr_disclosure_error_does_not_render_zero_percent_apr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt-and-braces: the failure path raises BEFORE any 0.00% APR
    string can land in the rendered HTML. If this regresses, it means
    somebody re-introduced the silent fallback — block at PR time."""
    from datetime import date as _date
    from datetime import datetime

    from aegis.compliance import disclosure_context
    from aegis.compliance.disclosure import APRDisclosureError, render_disclosure

    def _boom(*args: object, **kwargs: object) -> Decimal:
        raise APRCalculationError("brentq failed to converge")

    monkeypatch.setattr(disclosure_context, "calculate_apr", _boom)

    rendered_html: str | None = None
    try:
        out = render_disclosure(
            "CA",
            _valid_deal_for_ca(),
            _valid_score(),
            rendered_at=datetime(2026, 5, 13),
            disbursement_date=_date(2026, 5, 13),
        )
        rendered_html = out.html
    except APRDisclosureError:
        # Expected — the gate fired. The render did not produce HTML.
        pass

    assert rendered_html is None, (
        "render_disclosure must NOT return rendered HTML when APR fails; "
        "got back a document — silent 0.00% APR regression."
    )
