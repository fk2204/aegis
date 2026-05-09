"""Anti-double-dipping computation for NY renewal MCAs (§ 600.6(b)(3)(v)).

Source
------
Regulatory text (verbatim from docs/compliance/02_new_york.md, Row 1
"Funding Provided", NY-specific double-dipping disclosure under
23 NYCRR § 600.6(b)(3)(v)):

    Does the renewal financing include any amount that is used to pay
    unpaid finance charges or fees, also known as double dipping?
    {Yes, enter amount}. If the amount is zero, the answer would be No.

The regulation requires the disclosure but does not legislate the
computation — the provider (or broker on the provider's behalf)
determines the dollar amount using a defensible accounting convention.
This module implements **principal-first amortization** with pro-rata
allocation across the portion of the prior balance being paid off by
the renewal:

  unpaid_principal = prior_funded_amount - prior_amount_repaid
  unpaid_finance_charge_in_remaining
      = (prior_total_payback - prior_funded_amount)
      * (unpaid_principal / prior_funded_amount)

  remaining_balance = prior_total_payback - prior_amount_repaid
  fraction_paid_by_renewal
      = renewal_amount_used_to_pay_prior / remaining_balance

  double_dipping_amount
      = unpaid_finance_charge_in_remaining * fraction_paid_by_renewal

Worked example (the test asserts this number exactly):
  prior_funded_amount=$100,000, prior_total_payback=$130,000,
  prior_amount_repaid=$80,000, renewal_amount_used_to_pay_prior=$50,000
  → double_dipping_amount = $6,000.00

Edge-case behavior:
  * Zero or negative ``prior_funded_amount`` raises ``DoubleDippingInputError``.
  * ``prior_total_payback < prior_funded_amount`` raises (no finance charge).
  * ``prior_amount_repaid >= prior_funded_amount`` → result is $0
    (principal already fully paid; nothing of the remaining balance can
    be principal in the principal-first model, so no embedded finance
    charge to disclose).
  * ``prior_amount_repaid >= prior_total_payback`` → result is $0
    (nothing left to pay).
  * ``renewal_amount_used_to_pay_prior > remaining_balance`` → capped at
    remaining_balance; you can't double-dip on more than what's there.

Decimal-only math throughout — the provider may overdisclose under
NY § 600.4 but underdisclosure beyond tolerance is a violation.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


class DoubleDippingInputError(ValueError):
    """Raised when inputs to ``compute_double_dipping_amount`` are invalid."""


_TWOPLACES = Decimal("0.01")


def compute_double_dipping_amount(
    prior_funded_amount: Decimal,
    prior_total_payback: Decimal,
    prior_amount_repaid: Decimal,
    renewal_amount_used_to_pay_prior: Decimal,
) -> Decimal:
    """Compute the dollar amount of unpaid finance charge embedded in
    the prior-position payoff that the renewal will collect.

    All inputs are Decimal dollars (positive). Result is quantized to
    cents (HALF_UP), matching disclosure-form formatting.
    """
    _require_decimal(prior_funded_amount, "prior_funded_amount")
    _require_decimal(prior_total_payback, "prior_total_payback")
    _require_decimal(prior_amount_repaid, "prior_amount_repaid")
    _require_decimal(renewal_amount_used_to_pay_prior, "renewal_amount_used_to_pay_prior")

    if prior_funded_amount <= 0:
        raise DoubleDippingInputError("prior_funded_amount must be > 0")
    if prior_total_payback < prior_funded_amount:
        raise DoubleDippingInputError(
            "prior_total_payback must be >= prior_funded_amount "
            "(no negative finance charge possible)"
        )
    if prior_amount_repaid < 0:
        raise DoubleDippingInputError("prior_amount_repaid must be >= 0")
    if renewal_amount_used_to_pay_prior < 0:
        raise DoubleDippingInputError("renewal_amount_used_to_pay_prior must be >= 0")

    # Principal-first amortization: any repayment counts against principal
    # before it counts against finance charge. So:
    #   unpaid principal = max(funded - repaid, 0)
    unpaid_principal = max(prior_funded_amount - prior_amount_repaid, Decimal("0"))
    if unpaid_principal == 0:
        return Decimal("0.00")

    remaining_balance = prior_total_payback - prior_amount_repaid
    if remaining_balance <= 0:
        return Decimal("0.00")

    prior_finance_charge = prior_total_payback - prior_funded_amount

    # The portion of the prior finance charge that hasn't been "earned"
    # yet (still embedded in remaining balance), scaled to how much of
    # the remaining balance the renewal payoff actually covers.
    embedded_finance_charge = (
        prior_finance_charge * unpaid_principal / prior_funded_amount
    )

    # Cap renewal payoff at remaining balance — can't double-dip more
    # than what's there.
    payoff_amount = min(renewal_amount_used_to_pay_prior, remaining_balance)
    fraction_paid_by_renewal = payoff_amount / remaining_balance

    double_dipping = embedded_finance_charge * fraction_paid_by_renewal
    return double_dipping.quantize(_TWOPLACES, rounding=ROUND_HALF_UP)


def _require_decimal(value: object, name: str) -> None:
    if not isinstance(value, Decimal):
        raise DoubleDippingInputError(
            f"{name} must be Decimal (got {type(value).__name__}); "
            "convert at the boundary, never pass float"
        )


__all__ = ["DoubleDippingInputError", "compute_double_dipping_amount"]
