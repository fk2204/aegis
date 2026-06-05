"""One-shot: upsert Shor Capital INC as a funder.

Sourced from the operator's signed packet:
  * ``Shor Capital ISO Agreement - signed.pdf`` (Effective 2026-06-01)
  * ``Shor capital guidelines.png`` (Preferred / Restricted industries,
    minimum monthly revenue, standard stipulations)

Idempotent: upserts by name. Re-running preserves the existing id if
the funder already exists.

Multi-barrier guard (same shape as scripts/audit/seed_real_funders.py):
  1. ``--confirm`` flag required (else dry run + exit).
  2. If ``AEGIS_STORAGE_BACKEND`` is not ``memory``,
     ``AEGIS_ALLOW_PRODUCTION_SEED=true`` env var also required.

Run on the box, with /etc/aegis/aegis.env sourced::

    set -a; source /etc/aegis/aegis.env; set +a
    cd /opt/aegis
    AEGIS_ALLOW_PRODUCTION_SEED=true \\
        .venv/bin/python scripts/audit/seed_shor_capital.py --confirm
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal

from aegis.funders.models import FunderRow
from aegis.funders.repository import (
    FunderRepository,
    InMemoryFunderRepository,
    SupabaseFunderRepository,
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Seed Shor Capital funder from signed ISO packet + guidelines PNG. "
        "Refuses to write without --confirm. Refuses production writes "
        "without AEGIS_ALLOW_PRODUCTION_SEED=true."
    )
    p.add_argument(
        "--confirm",
        action="store_true",
        help="Actually perform the write. Without this flag the script "
        "runs in dry-run mode (lists what would happen, exits 0).",
    )
    return p.parse_args()


def _gate(confirm: bool) -> bool:
    """Return True if writes should proceed, False if dry-run only."""
    if not confirm:
        print("DRY RUN (no --confirm flag). Would perform the following:\n")
        return False
    backend = os.environ.get("AEGIS_STORAGE_BACKEND", "").lower()
    if backend != "memory":
        if os.environ.get("AEGIS_ALLOW_PRODUCTION_SEED", "").lower() != "true":
            print(
                "REFUSED: writing against a non-memory backend requires "
                "AEGIS_ALLOW_PRODUCTION_SEED=true in the environment.\n"
                "  This is a deliberate barrier — funder seeding must be "
                "an explicit operator decision.",
                file=sys.stderr,
            )
            sys.exit(2)
    return True


# Operator notes — anything the schema fields don't carry that the
# underwriter would want at a glance when picking Shor on a deal.
_SHOR_NOTES = (
    "ISO Agreement signed 2026-06-01 with Shor Capital INC, 747 Third Ave, "
    "2nd floor, New York, NY 10017. President: Alex Musheyev. "
    "Commission table (Exhibit A): 1.45 sell → 8 pts, 1.47 → 9 pts, "
    "1.49 → 10 pts (per-transaction, modifiable at SC sole discretion). "
    "Minimum commitment: 4 referrals per contract year. Clawback if merchant "
    "defaults within 30 days of funding. CONDITIONAL by state: "
    "California submissions must contain 4 months of bank statements "
    "for each account, and the agent must be DFPI-licensed under CFL; "
    "Virginia merchants require the agent to be registered with the "
    "Virginia Bureau of Financial Institutions as a sales-based "
    "financing Agent. Arbitration venue: New York. "
    "Contact: info@shor.capital, 877-218-8043."
)


def _build_funder() -> FunderRow:
    now = datetime.now(UTC)
    return FunderRow(
        name="Shor Capital",
        active=True,
        # From guidelines PNG: "Minimum Requirement: Average monthly
        # revenue of $20,000."
        min_monthly_revenue=Decimal("20000.00"),
        # Not published in the guidelines page or ISO agreement —
        # operator can fill in via /ui/funders/{id} once confirmed
        # with Shor. None = "no constraint on this axis" per FunderRow
        # convention.
        min_avg_daily_balance=None,
        min_credit_score=None,
        min_months_in_business=None,
        max_positions=None,
        accepts_stacking=False,
        min_advance=None,
        max_advance=None,
        max_nsf_tolerance=None,
        # ISO agreement contains no confession-of-judgment clause and
        # arbitration is the dispute mechanism (Section IV.b). Default
        # to False; operator can flip True if a future Shor MRSA template
        # carries a CoJ.
        requires_coj=False,
        # ISO Section III.e explicitly forbids agent from collecting
        # additional merchant-side fees through SC. AEGIS commission is
        # paid by Shor per Exhibit A — no merchant-side advance fees.
        charges_merchant_advance_fees=False,
        # From ISO Exhibit A — sell-rate band 1.45..1.49 with matching
        # ISO commission of 8..10 points. Treated as the typical buy-rate
        # / factor envelope for surfacing on the funder card.
        typical_factor_low=Decimal("1.45"),
        typical_factor_high=Decimal("1.49"),
        typical_holdback_low=None,
        typical_holdback_high=None,
        # From guidelines PNG "Restricted Industries (Not Funding)".
        excluded_industries=(
            "Financial Institutions",
            "Collection Agencies",
            "Used or New Auto Sales",
            "Non-Profit Organizations",
            "Bail Bonds",
            "Check Cashing",
            "Religious Entities",
            "Real Estate Investment",
            "Staffing Agencies",
            "Travel Agencies",
            "Oil Drilling",
        ),
        # No state exclusions in the guidelines — CA and VA carry extra
        # licensing requirements on AEGIS, not blocks at the funder.
        excluded_states=(),
        guidelines_extracted_at=now,
        # No PDF hash — the source was a PNG screenshot transcribed
        # manually, not an LLM-extracted PDF.
        guidelines_source_pdf_hash=None,
        # 23 NYCRR § 600.21(f) requires the funder to supply broker-
        # compensation disclosure text for NY merchants. Not in the
        # current ISO packet — operator must request from Shor before
        # routing NY-resident merchants.
        aegis_compensation_disclosure_text="",
        contact_name="Alex Musheyev",
        contact_phone="877-218-8043",
        contact_email="info@shor.capital",
        # Submission email not published in the ISO packet; default to
        # the general inbox. Operator can refine once Shor confirms a
        # dedicated submissions address.
        submission_email="info@shor.capital",
        tiers=(),
        # From guidelines PNG "Standard Stipulations for Funding".
        # These are document requirements, not auto-decline conditions.
        conditional_requirements=(
            "Driver's License (DL)",
            "Voided Check (VC)",
            "Merchant email",
            "Bank verification",
        ),
        auto_decline_conditions=(),
        notes=_SHOR_NOTES,
    )


def _pick_repository() -> FunderRepository:
    from aegis.config import get_settings

    backend = get_settings().aegis_storage_backend
    if backend == "memory":
        print("warning: using in-memory backend — no Supabase write will occur")
        return InMemoryFunderRepository()
    return SupabaseFunderRepository()


def main() -> int:
    args = _parse_args()
    will_write = _gate(args.confirm)

    repo = _pick_repository()
    existing = {f.name.lower(): f for f in repo.list_active()}
    incoming = _build_funder()

    prior = existing.get(incoming.name.lower())
    if prior is not None:
        incoming = incoming.model_copy(update={"id": prior.id})
        action = "WOULD UPDATE" if not will_write else "UPDATE"
    else:
        action = "WOULD INSERT" if not will_write else "INSERT"

    if not will_write:
        print(
            f"{action:13} {incoming.name:30} "
            f"min_rev={incoming.min_monthly_revenue} "
            f"factor={incoming.typical_factor_low}..{incoming.typical_factor_high} "
            f"excl_industries={len(incoming.excluded_industries)} "
            f"stips={len(incoming.conditional_requirements)}"
        )
        print("\nDry run complete. Re-run with --confirm to apply.")
        return 0

    saved = repo.upsert(incoming)
    print(
        f"{action:6} {saved.name:30} "
        f"id={saved.id} "
        f"min_rev={saved.min_monthly_revenue} "
        f"factor={saved.typical_factor_low}..{saved.typical_factor_high} "
        f"excl_industries={len(saved.excluded_industries)} "
        f"stips={len(saved.conditional_requirements)}"
    )
    print(f"\nTotal active funders now: {len(repo.list_active())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
