"""Seed a starter funder row so the /ui/merchants/{id}/match panel has
something to render. Operator can edit / replace via /ui/funders/import.

One conservative funder — Tier-A-like criteria — covering common MCA
gating signals. Operator should add real funder PDFs through the
funder-import flow when ready.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from typing import Any, cast

from aegis.db import get_supabase


def main() -> int:
    sb = get_supabase()
    existing = cast(
        list[dict[str, Any]],
        sb.table("funders").select("id, name").eq("name", "Sample Tier-A Funder").execute().data
        or [],
    )
    if existing:
        print(f"  reusing funder: {existing[0]['name']} ({existing[0]['id']})")
        return 0

    payload = {
        "name": "Sample Tier-A Funder",
        "accepts_stacking": False,
        "min_monthly_revenue": str(Decimal("25000.00")),
        "min_avg_daily_balance": str(Decimal("5000.00")),
        "min_credit_score": 600,
        "min_months_in_business": 6,
        "max_positions": 1,
        "min_advance": str(Decimal("10000.00")),
        "max_advance": str(Decimal("250000.00")),
        "max_nsf_tolerance": 4,
        "typical_factor_low": str(Decimal("1.18")),
        "typical_factor_high": str(Decimal("1.35")),
        "typical_holdback_low": str(Decimal("0.10")),
        "typical_holdback_high": str(Decimal("0.18")),
        "notes": "Seeded by audit run. Replace with real funder PDFs via /ui/funders/import.",
    }
    r = sb.table("funders").insert(payload).execute()
    print(f"  created funder: {payload['name']} ({r.data[0]['id']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
