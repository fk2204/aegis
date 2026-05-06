"""APR calculator using the actuarial method.

Returns APR as a fraction (0.365 = 36.5%). Convert to percent at the
disclosure-rendering boundary in Phase 4 — templates expect "36.50%" but
this module produces 0.3650.

Citations
---------
- 12 CFR § 1026 Appendix J ("Annual Percentage Rate Computations for
  Closed-End Credit Transactions") — the federal definition of APR for
  closed-end credit. We implement the actuarial method described there.
  https://www.consumerfinance.gov/rules-policy/regulations/1026/j/
- California 10 CCR § 950 et seq. (DFPI implementation of the Commercial
  Financing Disclosure Law, SB 1235) — requires APR disclosed for
  commercial financing in California to be computed by the actuarial
  method per Reg Z App J. APR is non-negotiable; simple-interest APR is
  legally insufficient.
  https://www.dfpi.ca.gov/commercial-financial-disclosure/

Method
------
Find the periodic rate `i` such that the present value of the payment
stream (discounted at `i` per unit period) equals the amount financed:

    AmtFin = Σ_k  Pmt_k / (1 + i)^t_k

where `t_k` is the time in unit periods from the disbursement date to
payment k. APR is then `i * (unit_periods_per_year)`. For MCA daily
payments the unit period is one day and APR = `i * 365`.

Float boundary
--------------
`scipy.optimize.brentq` operates on `float`. We accept and return
`Decimal` so the rest of the system stays float-free; conversion happens
only inside this function and is documented at the call site.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

# scipy ships no first-party stubs. scipy-stubs exists but is third-party
# and stale; pinning a stub package would couple us to its release cadence.
# `brentq` is a single, stable function — narrow ignore is the right trade.
from scipy.optimize import brentq  # type: ignore[import-untyped]

# Days per year for the actuarial method per Appendix J. 365 is the
# convention; leap-day handling is absorbed in the day-count.
DAYS_PER_YEAR = 365

# Quantization for the returned APR. 6 decimal places = 0.0001% precision,
# more than the regulator requires (CA DFPI requires 2dp on disclosures)
# but useful for parity diffs against the TS implementation.
_APR_QUANT = Decimal("0.000001")

# brentq search bracket for the daily rate. NPV at lower bound must be
# positive (sum of payments > principal at zero rate); NPV at upper bound
# must be negative. 5.0/day = 500% daily, beyond any conceivable MCA.
_BRACKET_LOW = 1e-12
_BRACKET_HIGH = 5.0


class APRCalculationError(ValueError):
    """Raised when the APR cannot be computed (bad input or no root)."""


def calculate_apr(
    amount_financed: Decimal,
    payments: Sequence[tuple[date, Decimal]],
    disbursement_date: date,
) -> Decimal:
    """Compute APR via the App J actuarial method.

    Parameters
    ----------
    amount_financed
        The principal disbursed to the merchant. Must be > 0.
    payments
        Sequence of (payment_date, payment_amount) tuples. All dates must
        be strictly after `disbursement_date`. Order does not matter.
    disbursement_date
        The date the merchant received the funds.

    Returns
    -------
    APR as a Decimal, expressed as a fraction (e.g. 0.365 for 36.5%).

    Raises
    ------
    APRCalculationError
        If inputs are degenerate (non-positive principal, no payments,
        any payment on or before disbursement, sum of payments not
        exceeding principal, or the optimizer cannot bracket a root).
    """
    if amount_financed <= 0:
        raise APRCalculationError("amount_financed must be > 0")
    if not payments:
        raise APRCalculationError("at least one payment is required")

    day_offsets: list[int] = []
    pmt_amounts: list[float] = []
    for p_date, p_amt in payments:
        offset = (p_date - disbursement_date).days
        if offset <= 0:
            raise APRCalculationError(
                f"payment dated {p_date} is on or before disbursement {disbursement_date}"
            )
        if p_amt <= 0:
            raise APRCalculationError(f"payment amount must be > 0 (got {p_amt})")
        day_offsets.append(offset)
        pmt_amounts.append(float(p_amt))

    principal = float(amount_financed)
    total_paid = sum(pmt_amounts)
    if total_paid <= principal:
        raise APRCalculationError(
            f"sum of payments ({total_paid}) must exceed amount financed ({principal})"
        )

    def npv(daily_rate: float) -> float:
        return (
            sum(
                amt / (1.0 + daily_rate) ** d
                for amt, d in zip(pmt_amounts, day_offsets, strict=True)
            )
            - principal
        )

    # Sanity: NPV at the low bracket must be positive (since total_paid > principal).
    # NPV at the high bracket must go negative (it does for any positive payment stream).
    try:
        daily_rate = brentq(npv, _BRACKET_LOW, _BRACKET_HIGH, xtol=1e-12, maxiter=200)
    except (ValueError, RuntimeError) as exc:
        raise APRCalculationError(f"brentq failed to converge: {exc}") from exc

    apr_float = daily_rate * DAYS_PER_YEAR
    return Decimal(repr(apr_float)).quantize(_APR_QUANT)


__all__ = ["DAYS_PER_YEAR", "APRCalculationError", "calculate_apr"]
