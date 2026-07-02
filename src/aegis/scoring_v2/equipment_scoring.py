"""Equipment financing scoring (2026-07-01 A1b).

Equipment financing lenders care about: credit score (600+ typical),
TIB (12+ months), LTV (≤90%), equipment eligibility. Bank statements
are secondary — equipment value + invoice + credit are primary.

The dossier renders a product-analysis section when
``merchant.product_type == 'equipment'`` — eligibility, LTV, required
down payment, term range, and any soft concerns (equipment age, missing
invoice).

Not persisted; pure derivation from live merchant + operator inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from aegis.merchants.models import MerchantRow

EQUIP_MIN_FICO: int = 600
EQUIP_MIN_TIB_MONTHS: int = 12
EQUIP_MAX_LTV: Decimal = Decimal("0.90")
EQUIP_TYPICAL_DOWN_PCT: Decimal = Decimal("0.10")
EQUIP_TERM_MONTHS_MIN: int = 24
EQUIP_TERM_MONTHS_MAX: int = 84
EQUIP_MAX_AGE_YEARS: int = 10

# Substrings that mark an "equipment" description as ineligible
# collateral — lenders won't finance these as equipment.
INELIGIBLE_EQUIPMENT_KEYWORDS: frozenset[str] = frozenset(
    {
        "software",
        "intellectual property",
        "franchise fees",
        "working capital",
        "real estate",
        "inventory",
        "goodwill",
    }
)


@dataclass
class EquipmentScoringResult:
    """Product-analysis output for an equipment-shape deal."""

    eligible: bool
    blockers: list[str] = field(default_factory=list)
    soft_concerns: list[str] = field(default_factory=list)
    max_advance: Decimal | None = None
    ltv: Decimal | None = None
    required_down_payment: Decimal | None = None
    term_months_range: tuple[int, int] | None = None


def score_equipment_deal(
    merchant: MerchantRow,
    equipment_cost: Decimal | None = None,
    equipment_type: str | None = None,
    equipment_age_years: int | None = None,
) -> EquipmentScoringResult:
    """Score one merchant against equipment-financing criteria."""
    blockers: list[str] = []
    concerns: list[str] = []

    fico = merchant.credit_score or 0
    tib = merchant.time_in_business_months or 0

    if fico < EQUIP_MIN_FICO:
        blockers.append(f"FICO {fico} below equipment minimum {EQUIP_MIN_FICO}")
    if tib < EQUIP_MIN_TIB_MONTHS:
        blockers.append(f"TIB {tib} months below equipment minimum {EQUIP_MIN_TIB_MONTHS}")
    if equipment_type:
        eq_lower = equipment_type.lower()
        for ineligible in INELIGIBLE_EQUIPMENT_KEYWORDS:
            if ineligible in eq_lower:
                blockers.append(
                    f"'{equipment_type}' is not eligible collateral for equipment financing"
                )
                break
    if equipment_age_years is not None and equipment_age_years > EQUIP_MAX_AGE_YEARS:
        concerns.append(
            f"Equipment age {equipment_age_years} years — lenders typically cap "
            f"at {EQUIP_MAX_AGE_YEARS} years"
        )
    if equipment_cost is None:
        concerns.append("No equipment cost/invoice provided — LTV cannot be calculated")

    if blockers:
        return EquipmentScoringResult(eligible=False, blockers=blockers)

    max_advance = (equipment_cost * EQUIP_MAX_LTV) if equipment_cost else None
    down = (equipment_cost * EQUIP_TYPICAL_DOWN_PCT) if equipment_cost else None

    return EquipmentScoringResult(
        eligible=True,
        soft_concerns=concerns,
        max_advance=max_advance,
        ltv=EQUIP_MAX_LTV,
        required_down_payment=down,
        term_months_range=(EQUIP_TERM_MONTHS_MIN, EQUIP_TERM_MONTHS_MAX),
    )


__all__ = [
    "EQUIP_MAX_AGE_YEARS",
    "EQUIP_MAX_LTV",
    "EQUIP_MIN_FICO",
    "EQUIP_MIN_TIB_MONTHS",
    "EQUIP_TERM_MONTHS_MAX",
    "EQUIP_TERM_MONTHS_MIN",
    "EQUIP_TYPICAL_DOWN_PCT",
    "INELIGIBLE_EQUIPMENT_KEYWORDS",
    "EquipmentScoringResult",
    "score_equipment_deal",
]
