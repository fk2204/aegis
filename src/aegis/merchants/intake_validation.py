"""Intake-time data quality validator for merchant application data.

Runs at Close-webhook merchant sync time (BEFORE any bank statement
is parsed) to catch obvious data-entry errors and impossible ratios
in the application. The 2026-07-01 audit found three real cases in
the pipeline:

* **Fullerworks LLC** — Close application says ``monthly_revenue=$55``
  (missing three zeros — should be $55,000) while ``requested_amount=$150,000``.
  The Close sync picked the first (broken) submission and stored $55.
* **Noble Horse Carriages** — Close application says
  ``monthly_revenue=$25,000`` and ``requested_amount=$1,350,000``, a
  54x revenue-to-request ratio. The existing
  ``detect_impossible_payment_load`` pattern requires
  ``stated_daily_payment`` (only fires at scoring time on
  parse-time data), so this application-level mismatch is silent.
* **The Turnbull Company LLC** — 37x requested-to-revenue ratio,
  same silent-at-intake problem.

The three warnings this module emits are informational — they do NOT
gate the deal, do NOT change scoring, and do NOT auto-decline
anything. They ONLY surface on the dossier Application Data
section so the operator reviews before spending time underwriting a
broken row.

Pure function. No I/O. Callers pass the intake fields as a small
dict; the module returns a list of ``IntakeWarning`` dataclass
instances (empty when nothing fires). The caller decides where to
write them (audit_log, merchant.notes, an inline banner).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

# Rev < $1K on a request > $10K is a near-certain "missing zeros" typo
# — no legitimate merchant application clears the requested advance
# floor with a sub-$1K revenue field. Tuned conservatively so a real
# but tiny business doesn't fire (the request cap keeps it narrow).
_SUSPICIOUS_REVENUE_FLOOR: Final[Decimal] = Decimal("1000")
_SUSPICIOUS_REVENUE_REQUEST_FLOOR: Final[Decimal] = Decimal("10000")

# > 10x requested-to-revenue is a hard "the merchant is asking for a
# year's worth of revenue" ratio. MCA industry norm caps advances at
# roughly 1x to 1.5x monthly revenue; 10x is off the map.
_IMPOSSIBLE_REQUEST_RATIO: Final[Decimal] = Decimal("10")

# Existing MCA balance > 3x monthly revenue is the "the merchant is
# already underwater" signal. At 3x balance the daily-payment load
# alone (assuming 6-month term) exceeds monthly cashflow.
_IMPOSSIBLE_BALANCE_RATIO: Final[Decimal] = Decimal("3")

# Warning codes — the caller uses these for the audit_log action
# name and any dossier filtering. Kept short + stable so downstream
# consumers can grep for them.
_CODE_SUSPICIOUS_REVENUE: Final[str] = "intake_revenue_suspiciously_low"
_CODE_IMPOSSIBLE_REQUEST: Final[str] = "intake_request_over_10x_revenue"
_CODE_IMPOSSIBLE_BALANCE: Final[str] = "intake_balance_over_3x_revenue"


@dataclass(frozen=True)
class IntakeWarning:
    """One data-quality warning about the intake application data.

    ``code`` is a short stable string suitable for audit_log action
    names and dossier filtering. ``message`` is the operator-facing
    plain-English explanation.
    """

    code: str
    message: str


def validate_intake_financial_data(
    *,
    monthly_revenue: Decimal | float | int | None,
    requested_amount: Decimal | float | int | None,
    stated_mca_balance: Decimal | float | int | None = None,
) -> list[IntakeWarning]:
    """Return operator-facing warnings for the application data.

    Empty list = no issues detected. None of the checks fail-close:
    missing inputs collapse to "not checked" rather than "warn."

    * ``monthly_revenue`` — MerchantRow.monthly_revenue at intake.
    * ``requested_amount`` — MerchantRow.requested_amount at intake.
    * ``stated_mca_balance`` — MerchantRow.stated_mca_balance at intake
      (optional; used only for the balance-ratio check).
    """
    warnings: list[IntakeWarning] = []
    rev = _to_decimal(monthly_revenue)
    req = _to_decimal(requested_amount)
    bal = _to_decimal(stated_mca_balance)

    # Rule 1 — revenue field suspiciously low relative to the request.
    if (
        rev is not None
        and rev > Decimal("0")
        and rev < _SUSPICIOUS_REVENUE_FLOOR
        and req is not None
        and req > _SUSPICIOUS_REVENUE_REQUEST_FLOOR
    ):
        warnings.append(
            IntakeWarning(
                code=_CODE_SUSPICIOUS_REVENUE,
                message=(
                    f"monthly_revenue=${rev:,.0f} may be a data entry "
                    f"error — request is ${req:,.0f}. Check the "
                    "application for missing zeros before underwriting."
                ),
            )
        )

    # Rule 2 — requested amount > 10x monthly revenue.
    if rev is not None and rev > Decimal("0") and req is not None and req > Decimal("0"):
        ratio = req / rev
        if ratio > _IMPOSSIBLE_REQUEST_RATIO:
            warnings.append(
                IntakeWarning(
                    code=_CODE_IMPOSSIBLE_REQUEST,
                    message=(
                        f"Requested amount (${req:,.0f}) is "
                        f"{ratio:.0f}x monthly revenue (${rev:,.0f}). "
                        "Verify with the merchant before spending time "
                        "on underwriting — this ratio is off market."
                    ),
                )
            )

    # Rule 3 — existing MCA balance > 3x monthly revenue.
    if rev is not None and rev > Decimal("0") and bal is not None and bal > Decimal("0"):
        ratio = bal / rev
        if ratio > _IMPOSSIBLE_BALANCE_RATIO:
            warnings.append(
                IntakeWarning(
                    code=_CODE_IMPOSSIBLE_BALANCE,
                    message=(
                        f"Existing MCA balance (${bal:,.0f}) is "
                        f"{ratio:.1f}x monthly revenue (${rev:,.0f}). "
                        "Payment load likely impossible — verify "
                        "current obligations before underwriting."
                    ),
                )
            )

    return warnings


def _to_decimal(
    value: Decimal | float | int | None,
) -> Decimal | None:
    """Convert numeric-ish inputs to Decimal. Preserves None + bad
    strings as None so the validator degrades gracefully."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool is a subclass of int
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    return None


__all__ = [
    "IntakeWarning",
    "validate_intake_financial_data",
]
