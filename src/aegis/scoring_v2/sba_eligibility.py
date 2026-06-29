"""SBA referral eligibility detector (master plan § 12.1).

Pure additive informational layer surfaced on the merchant dossier next
to the verdict header. The detector reads the operator-known merchant
profile (TIB, FICO, bankruptcy, OFAC) and the bank-derived monthly
revenue and emits:

* ``eligible`` — boolean. ``False`` when any hard disqualifier fires or
  any soft blocker threshold is below the SBA-floor.
* ``program`` — when eligible, the suggested SBA program tier
  (``7(a)`` / ``504`` / ``Express`` / ``Microloan``). ``None`` when
  ineligible.
* ``blockers`` — ordered list of human-readable strings explaining why
  the merchant cannot be referred today. Empty when ``eligible=True``.
* ``strengths`` — ordered list of human-readable strings reinforcing
  why this merchant *is* a fit. Surfaced regardless of eligibility so
  the operator can see "all three core thresholds passed but one
  blocker remains" at a glance.
* ``estimated_max_amount`` — soft heuristic per the master plan's
  shipping spec: when eligible, ``revenue * 36`` (i.e. roughly 36
  months of stated monthly revenue as a notional ceiling). Always
  ``Decimal``; ``None`` when ineligible.

This is a **soft signal only.** It does NOT:

* feed Track A / Track B / Track C compute,
* change the AEGIS recommendation or hard-decline reasons,
* gate funder matching,
* persist anywhere — every call is recomputed from live merchant +
  analysis state at dossier-render time.

It exists so the operator sees a "this looks like a textbook SBA 7(a)
referral" green pill on the merchant who walks in matching the
Rendezvous Inc profile (35yr TIB, 708 FICO, $200K/month, no
bankruptcy) and is reminded to make the referral call instead of
pushing the deal to an MCA funder that will price it worse than the
SBA market would.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from aegis.merchants.models import MerchantRow
from aegis.storage import AnalysisRow

SBAProgram = Literal["7(a)", "504", "Express", "Microloan"]

# Thresholds — locked here so a future shift in SBA underwriting
# convention is a single-file edit, never a scattered constant hunt.
_TIB_MONTHS_BLOCKER: int = 24
_TIB_MONTHS_STRENGTH: int = 60
_FICO_BLOCKER: int = 650
_FICO_STRENGTH: int = 700
_REVENUE_BLOCKER: Decimal = Decimal("10000")
_REVENUE_STRENGTH: Decimal = Decimal("50000")
_REVENUE_7A_FLOOR: Decimal = Decimal("100000")
_FICO_7A_FLOOR: int = 680
_REVENUE_EXPRESS_FLOOR: Decimal = Decimal("50000")
_EST_MAX_MULTIPLE: Decimal = Decimal("36")


@dataclass(frozen=True)
class SBAEligibilityResult:
    """Result of an SBA-referral pre-screen for one merchant.

    Frozen dataclass — the dossier render path treats this as
    immutable. Construct via :func:`check_sba_eligibility`; don't
    instantiate directly outside of tests.
    """

    eligible: bool
    program: SBAProgram | None
    blockers: list[str] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    estimated_max_amount: Decimal | None = None


def check_sba_eligibility(
    merchant: MerchantRow,
    analysis: AnalysisRow,
) -> SBAEligibilityResult:
    """Score one merchant against the SBA-referral pre-screen rules.

    Hard disqualifiers (any one fires ``eligible=False``):

    * ``merchant.bankruptcy_active is True`` — active bankruptcy on
      file. SBA underwriting will reject regardless of every other
      metric.
    * ``merchant.ofac_is_clear is False`` — OFAC SDN hit. The merchant
      cannot be placed with any regulated lender; SBA referral is
      identical to MCA funder routing in this branch.

    Soft blockers (each adds one ``blockers`` entry; eligibility falls
    out of ``len(blockers) == 0``):

    * TIB < 24 months — under SBA's typical 2-year-in-business floor.
    * FICO < 650 — under SBA 7(a) typical credit floor.
    * Monthly revenue (bank-derived) < $10k — too thin for SBA
      programs that scale on revenue.

    Strengths (each adds one ``strengths`` entry; informational only):

    * TIB >= 60 months.
    * FICO >= 700.
    * Monthly revenue > $50k.

    Program tier selection (only when eligible):

    * revenue > $100k/mo AND FICO >= 680 -> ``7(a)``
    * revenue > $50k/mo                  -> ``Express``
    * otherwise                          -> ``Microloan``

    Estimated max amount (when eligible): ``revenue * 36``. Soft
    heuristic per the master plan; not a hard cap and not what the SBA
    or any funder will actually offer.
    """

    blockers: list[str] = []
    strengths: list[str] = []

    if merchant.bankruptcy_active is True:
        blockers.append("Active bankruptcy on file")

    if merchant.ofac_is_clear is False:
        blockers.append("OFAC flag — cannot place with any lender")

    tib = merchant.time_in_business_months
    if tib is not None:
        if tib < _TIB_MONTHS_BLOCKER:
            blockers.append(
                f"Time in business < {_TIB_MONTHS_BLOCKER} months (SBA typically requires 2+ years)"
            )
        elif tib >= _TIB_MONTHS_STRENGTH:
            strengths.append(f"Time in business ≥ {_TIB_MONTHS_STRENGTH} months - well-established")

    fico = merchant.credit_score
    if fico is not None:
        if fico < _FICO_BLOCKER:
            blockers.append(f"FICO < {_FICO_BLOCKER} (SBA 7(a) typical floor)")
        elif fico >= _FICO_STRENGTH:
            strengths.append(f"FICO ≥ {_FICO_STRENGTH} - strong credit")

    revenue: Decimal = analysis.monthly_revenue
    if revenue < _REVENUE_BLOCKER:
        blockers.append("Revenue too low for most SBA programs")
    elif revenue > _REVENUE_STRENGTH:
        strengths.append(f"Monthly revenue > ${_REVENUE_STRENGTH:,.0f}")

    eligible = len(blockers) == 0

    program: SBAProgram | None = None
    estimated_max_amount: Decimal | None = None
    if eligible:
        if revenue > _REVENUE_7A_FLOOR and fico is not None and fico >= _FICO_7A_FLOOR:
            program = "7(a)"
        elif revenue > _REVENUE_EXPRESS_FLOOR:
            program = "Express"
        else:
            program = "Microloan"
        estimated_max_amount = revenue * _EST_MAX_MULTIPLE

    return SBAEligibilityResult(
        eligible=eligible,
        program=program,
        blockers=blockers,
        strengths=strengths,
        estimated_max_amount=estimated_max_amount,
    )


__all__ = [
    "SBAEligibilityResult",
    "SBAProgram",
    "check_sba_eligibility",
]
