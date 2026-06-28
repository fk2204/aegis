"""Bulk-seed the 18 major US MCA / loan funders missing from the AEGIS catalog.

The production catalog as of 2026-06-28 has 9 funders — operator
reported this is critically thin. This script adds 18 well-known
funders pulled from the operator's manual reference list with
underwriting criteria the matcher can fire on.

**Idempotent**: looks up each funder by canonical name first; skips
when a row already exists. New funders go in via the standard
``FunderRepository.upsert`` path, so a single ``audit_log`` row is
written per insert (mirrors the ``/ui/funders/import/save`` route's
persistence semantics).

Usage::

    uv run python scripts/seed_missing_funders.py            # dry-run
    uv run python scripts/seed_missing_funders.py --apply    # write

The default is dry-run for safety — the script prints what it WOULD
insert and exits zero. ``--apply`` flips the switch and performs the
actual upsert.

Field mapping note: the operator's spec used short field names
(``min_revenue``, ``min_fico``, ``min_tib_months``, ``allows_stacking``)
that don't match ``FunderRow``. This script translates to the
canonical names (``min_monthly_revenue``, ``min_credit_score``,
``min_months_in_business``, ``accepts_stacking``).
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from typing import Any, Final

from aegis.api.deps import get_funder_repository
from aegis.funders.models import FunderRow

# Source-of-truth list. Field names match FunderRow exactly (operator's
# spec aliases translated here, NOT downstream).
_MISSING_FUNDERS: Final[tuple[dict[str, Any], ...]] = (
    {
        "name": "Rapid Finance",
        "min_monthly_revenue": Decimal("10000"),
        "min_credit_score": 550,
        "min_months_in_business": 12,
        "max_positions": 3,
        "accepts_stacking": True,
        "deal_types_accepted": ("mca", "term_loan", "loc"),
    },
    {
        "name": "Kapitus",
        "min_monthly_revenue": Decimal("25000"),
        "min_credit_score": 625,
        "min_months_in_business": 24,
        "max_positions": 2,
        "accepts_stacking": True,
        "deal_types_accepted": ("mca", "term_loan", "equipment_financing"),
    },
    {
        "name": "Fora Financial",
        "min_monthly_revenue": Decimal("12500"),
        "min_credit_score": 500,
        "min_months_in_business": 6,
        "max_positions": 4,
        "accepts_stacking": True,
        "deal_types_accepted": ("mca",),
    },
    {
        "name": "Credibly",
        "min_monthly_revenue": Decimal("15000"),
        "min_credit_score": 500,
        "min_months_in_business": 6,
        "max_positions": 3,
        "accepts_stacking": True,
        "deal_types_accepted": ("mca", "term_loan", "loc"),
    },
    {
        "name": "Expansion Capital Group",
        "min_monthly_revenue": Decimal("10000"),
        "min_credit_score": 500,
        "min_months_in_business": 6,
        "max_positions": 5,
        "accepts_stacking": True,
        "deal_types_accepted": ("mca",),
    },
    {
        "name": "Libertas Funding",
        "min_monthly_revenue": Decimal("20000"),
        "min_credit_score": 550,
        "min_months_in_business": 12,
        "max_positions": 3,
        "accepts_stacking": True,
        "deal_types_accepted": ("mca",),
    },
    {
        "name": "Greenbox Capital",
        "min_monthly_revenue": Decimal("8000"),
        "min_credit_score": 500,
        "min_months_in_business": 3,
        "max_positions": 5,
        "accepts_stacking": True,
        "deal_types_accepted": ("mca",),
    },
    {
        "name": "Forward Financing",
        "min_monthly_revenue": Decimal("10000"),
        "min_credit_score": 500,
        "min_months_in_business": 6,
        "max_positions": 3,
        "accepts_stacking": True,
        "deal_types_accepted": ("mca",),
    },
    {
        "name": "Idea Financial",
        "min_monthly_revenue": Decimal("15000"),
        "min_credit_score": 600,
        "min_months_in_business": 12,
        "max_positions": 2,
        "accepts_stacking": False,
        "deal_types_accepted": ("term_loan", "loc"),
    },
    {
        "name": "Mulligan Funding",
        "min_monthly_revenue": Decimal("17500"),
        "min_credit_score": 600,
        "min_months_in_business": 12,
        "max_positions": 2,
        "accepts_stacking": False,
        "deal_types_accepted": ("mca", "term_loan"),
    },
    {
        "name": "Yellowstone Capital",
        "min_monthly_revenue": Decimal("10000"),
        "min_credit_score": 500,
        "min_months_in_business": 6,
        "max_positions": 5,
        "accepts_stacking": True,
        "deal_types_accepted": ("mca",),
    },
    {
        "name": "World Business Lenders",
        "min_monthly_revenue": Decimal("25000"),
        "min_credit_score": 620,
        "min_months_in_business": 24,
        "max_positions": 1,
        "accepts_stacking": False,
        "deal_types_accepted": ("term_loan",),
    },
    {
        "name": "National Business Capital",
        "min_monthly_revenue": Decimal("10000"),
        "min_credit_score": 550,
        "min_months_in_business": 12,
        "max_positions": 4,
        "accepts_stacking": True,
        "deal_types_accepted": ("mca", "term_loan", "equipment_financing"),
    },
    {
        "name": "Rewards Network",
        "min_monthly_revenue": Decimal("10000"),
        "min_credit_score": 500,
        "min_months_in_business": 6,
        "max_positions": 2,
        "accepts_stacking": True,
        "deal_types_accepted": ("mca",),
        # Operator note: Rewards Network only funds restaurants. Listed
        # here as a notes_residual; the matcher doesn't enforce
        # industry inclusion (only exclusion via excluded_industries),
        # so the operator handles the restaurant gate at submission time.
        "notes_residual": (
            "Restaurant-only funder. Operator screens for NAICS 722xxx "
            "at submission; matcher does not enforce industry inclusion."
        ),
    },
    {
        "name": "SOS Capital",
        "min_monthly_revenue": Decimal("8000"),
        "min_credit_score": 500,
        "min_months_in_business": 3,
        "max_positions": 6,
        "accepts_stacking": True,
        "deal_types_accepted": ("mca",),
    },
    {
        "name": "Business Backer",
        "min_monthly_revenue": Decimal("12500"),
        "min_credit_score": 525,
        "min_months_in_business": 12,
        "max_positions": 3,
        "accepts_stacking": True,
        "deal_types_accepted": ("mca", "term_loan"),
    },
    {
        "name": "Maxim Commercial Capital",
        "min_monthly_revenue": Decimal("0"),
        "min_credit_score": 550,
        "min_months_in_business": 0,
        "max_positions": 1,
        "accepts_stacking": False,
        "deal_types_accepted": ("equipment_financing",),
    },
    {
        "name": "OnDeck",
        "min_monthly_revenue": Decimal("10000"),
        "min_credit_score": 600,
        "min_months_in_business": 12,
        "max_positions": 1,
        "accepts_stacking": False,
        "deal_types_accepted": ("term_loan", "loc"),
    },
)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the upsert. Without this flag the script prints what "
        "it would insert and exits zero (dry-run).",
    )
    args = parser.parse_args(argv)

    repo = get_funder_repository()
    existing = {f.name.strip().lower(): f for f in repo.list_active()}

    new_count = 0
    skipped_count = 0

    for spec in _MISSING_FUNDERS:
        canonical = spec["name"].strip().lower()
        if canonical in existing:
            print(f"[SKIP]   already in catalog — {spec['name']}")
            skipped_count += 1
            continue

        funder = FunderRow.model_validate(spec)
        print(
            f"[NEW]    {spec['name']:35s}  "
            f"min_rev=${spec['min_monthly_revenue']:>7,}  "
            f"FICO={spec['min_credit_score']}  "
            f"TIB={spec['min_months_in_business']:>2}mo  "
            f"max_pos={spec['max_positions']}  "
            f"stacking={'Y' if spec['accepts_stacking'] else 'N'}  "
            f"types={','.join(spec['deal_types_accepted'])}"
        )
        if args.apply:
            repo.upsert(funder)
            new_count += 1

    print()
    if args.apply:
        print(f"Inserted {new_count} new funders; skipped {skipped_count} already present.")
    else:
        print(
            f"DRY RUN: would insert {len(_MISSING_FUNDERS) - skipped_count} new funders; "
            f"skipped {skipped_count} already present. Re-run with --apply to write."
        )
    return 0


if __name__ == "__main__":
    sys.exit(_main())
