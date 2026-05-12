"""One-shot: upsert the three real funders Commera has signed ISO agreements with.

Sourced from the operator's signed packets in ``OneDrive/COmmera ugovori/``:
  * Logic Advance Group  (LAG Holdings, LLC — Logic Advance Updated guidelines PNG)
  * Velocity Capital Group (VCG ISO Guidelines PDF)
  * Swiftsource Funding   (Swiftsource ISO Welcome & Funding Guidelines page)

Idempotent: re-running upserts by name (existing IDs preserved if the funder
already exists).

Run on the box, with /etc/aegis/aegis.env sourced:

    set -a; source /etc/aegis/aegis.env; set +a
    cd /opt/aegis
    .venv/bin/python scripts/_seed_real_funders.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal

from aegis.funders.models import FunderRow
from aegis.funders.repository import (
    FunderRepository,
    InMemoryFunderRepository,
    SupabaseFunderRepository,
)

_LOGIC_NOTES = (
    "4-tier ISO structure: Elite (1.25 buy, 5+ yrs TIB, 700+ FICO, $100K+ MRR, "
    "max 1 position, max $1.5M, ≤15% holdback), Premium (1.28, 4+ yrs, 650+ FICO, "
    "$50K+ MRR, max 2 positions, max $1.2M, ≤25% holdback), Standard (1.30, "
    "12+ mo TIB, 550+ FICO, $25K+ MRR, max 3 positions, max $1M, ≤45% holdback), "
    "High-Risk (1.37, 6+ mo TIB, 500+ FICO, $25K+ MRR, max 5 positions, ≤50% "
    "holdback, satisfied defaults/settlements OK, 10% origination). "
    "Auto declines: bouncing MCA payments, active defaults, lowered/modified "
    "payments, recent bankruptcy, multiple fundings in recent months, 6+ "
    "total positions. CONDITIONAL: Trucking and Construction excluded only "
    "when TIB <24 months OR revenue <$100K — operator must manually verify "
    "for those industries. Contact: Erik (516) 261-2730. "
    "Submissions: Submissions@logicadvancegroup.com."
)

_VCG_NOTES = (
    "Funding for all 50 states. 1st-4th positions. Up to $2M advance. "
    "Up to 12 months TIB. Bankruptcies/judgments/tax liens OK with "
    "documentation. Home-based businesses OK. Min 3 deposits/month, "
    "max 2-3 negative days/month, max 30% withhold. Min 51% ownership "
    "required. Commissions: same-day, up to 15% capped. Renewals at "
    "50% paid off. CONDITIONAL: '1st Position Construction' is excluded — "
    "2nd+ position construction is fundable. Contact: (833) VCG-FUND, "
    "info@velocitycg.com. Founder: Jay Avigdor."
)

_SWIFTSOURCE_NOTES = (
    "2nd position and up only — NOT a first-position funder. Terms up to "
    "100 days / 20 weeks. Max $750K. Min factor 1.37+. Min $40K monthly "
    "revenue. CONDITIONAL revenue floors by industry: Transportation "
    "requires $100K+, Construction requires $50K+. Required submission: "
    "App + 3 bank statements + MTD (after 15th). Commissions paid weekly "
    "(Thursdays), 10-15 points. PSF fee on top of underwriting fee "
    "strictly prohibited. Submissions: Subs@SwiftFunding.net, "
    "CC: Jason@SwiftFunding.net."
)


def _build_funders() -> list[FunderRow]:
    now = datetime.now(UTC)
    return [
        FunderRow(
            name="Logic Advance Group",
            active=True,
            # Loosest tier floor (High-Risk). A merchant who clears these is
            # fundable at *some* tier; AEGIS surfaces the funder, operator
            # prices per tier from the notes.
            min_monthly_revenue=Decimal("25000.00"),
            min_credit_score=500,
            min_months_in_business=6,
            max_positions=5,
            accepts_stacking=True,
            min_advance=None,
            max_advance=Decimal("1500000.00"),
            max_nsf_tolerance=None,
            requires_coj=False,
            charges_merchant_advance_fees=False,
            typical_factor_low=Decimal("1.25"),
            typical_factor_high=Decimal("1.37"),
            typical_holdback_low=Decimal("0.15"),
            typical_holdback_high=Decimal("0.50"),
            excluded_industries=(
                "Automotive Dealership",
                "Real Estate",
                "Financial Services",
            ),
            excluded_states=(),
            guidelines_extracted_at=now,
            notes=_LOGIC_NOTES,
        ),
        FunderRow(
            name="Velocity Capital Group",
            active=True,
            min_monthly_revenue=Decimal("20000.00"),
            min_credit_score=500,
            min_months_in_business=12,
            max_positions=4,
            accepts_stacking=True,
            min_advance=None,
            max_advance=Decimal("2000000.00"),
            max_nsf_tolerance=3,
            requires_coj=False,
            charges_merchant_advance_fees=False,
            typical_factor_low=None,
            typical_factor_high=None,
            typical_holdback_low=None,
            typical_holdback_high=Decimal("0.30"),
            excluded_industries=(
                "Bail Bonds",
                "Gas Stations",
                "Investment Firms",
                "Law Firms",
                "Travel Agencies",
                "Religious Organizations",
                "Logistics",
                "Moving",
                "Trucking",
                "Nail/Salons",
                "Lending Platforms",
                "Vehicle/Auto Dealer",
                "Real Estate",
                "Staffing",
                "Vape Shops",
                "Pawn Shops",
            ),
            excluded_states=(),
            guidelines_extracted_at=now,
            notes=_VCG_NOTES,
        ),
        FunderRow(
            name="Swiftsource Funding",
            active=True,
            min_monthly_revenue=Decimal("40000.00"),
            min_credit_score=None,
            min_months_in_business=12,
            max_positions=None,  # 2nd+ only; no published upper cap
            accepts_stacking=True,
            min_advance=None,
            max_advance=Decimal("750000.00"),
            max_nsf_tolerance=None,
            requires_coj=False,
            charges_merchant_advance_fees=False,
            typical_factor_low=Decimal("1.37"),
            typical_factor_high=None,
            typical_holdback_low=None,
            typical_holdback_high=None,
            excluded_industries=(
                "Casinos",
                "Financial Services",
            ),
            excluded_states=(),
            guidelines_extracted_at=now,
            notes=_SWIFTSOURCE_NOTES,
        ),
    ]


def _pick_repository() -> FunderRepository:
    from aegis.config import get_settings

    backend = get_settings().aegis_storage_backend
    if backend == "memory":
        print("warning: using in-memory backend — no Supabase write will occur")
        return InMemoryFunderRepository()
    return SupabaseFunderRepository()


def main() -> int:
    repo = _pick_repository()
    existing = {f.name.lower(): f for f in repo.list_active()}
    funders = _build_funders()

    for incoming in funders:
        prior = existing.get(incoming.name.lower())
        if prior is not None:
            # Preserve the existing id so re-runs don't churn UUIDs.
            incoming = incoming.model_copy(update={"id": prior.id})
            action = "UPDATE"
        else:
            action = "INSERT"
        saved = repo.upsert(incoming)
        print(
            f"{action:6} {saved.name:30} "
            f"id={saved.id} "
            f"min_rev={saved.min_monthly_revenue} "
            f"max_adv={saved.max_advance} "
            f"excl={len(saved.excluded_industries)}"
        )

    print(f"\nTotal active funders now: {len(repo.list_active())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
