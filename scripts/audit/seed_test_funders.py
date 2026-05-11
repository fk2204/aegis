"""Audit-time helper: seed a starter funder row so the /match panel
has something to render.

ONLY for dev/test environments. The "Sample Tier-A Funder" name is
deliberately a placeholder — never a real funder. Operator should
add real funder PDFs through /ui/funders/import.

Same multi-barrier guard as scripts/audit/seed_test_merchants.py:

  1. --confirm flag required (else dry run + exit).
  2. --env flag required, one of {dev, production}.
  3. If --env=production, AEGIS_ALLOW_PRODUCTION_SEED=true env var
     also required.

Usage:

    # Dry run:
    uv run python scripts/audit/seed_test_funders.py --env=dev

    # Apply in dev:
    uv run python scripts/audit/seed_test_funders.py --env=dev --confirm

    # Apply in production (extra barrier):
    AEGIS_ALLOW_PRODUCTION_SEED=true uv run python \\
        scripts/audit/seed_test_funders.py --env=production --confirm
"""

from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal
from typing import Any, cast

from aegis.db import get_supabase


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Seed a starter test funder. Refuses to write without "
        "--confirm. Refuses production writes without "
        "AEGIS_ALLOW_PRODUCTION_SEED=true."
    )
    p.add_argument(
        "--env",
        required=True,
        choices=("dev", "production"),
        help="Target environment. 'production' requires "
        "AEGIS_ALLOW_PRODUCTION_SEED=true env var.",
    )
    p.add_argument(
        "--confirm",
        action="store_true",
        help="Actually perform the write. Without this flag the script "
        "runs in dry-run mode (lists what would happen, exits 0).",
    )
    return p.parse_args()


def _gate(env: str, confirm: bool) -> bool:
    """Return True if writes should proceed, False if dry-run only."""
    if not confirm:
        print("DRY RUN (no --confirm flag). Would perform the following write:\n")
        return False
    if env == "production":
        if os.environ.get("AEGIS_ALLOW_PRODUCTION_SEED", "").lower() != "true":
            print(
                "REFUSED: --env=production requires "
                "AEGIS_ALLOW_PRODUCTION_SEED=true in the environment.\n"
                "  This is a deliberate barrier — placeholder funder rows "
                "must never reach production again.",
                file=sys.stderr,
            )
            sys.exit(2)
    return True


def main() -> int:
    args = _parse_args()
    will_write = _gate(args.env, args.confirm)
    sb = get_supabase()

    existing = cast(
        list[dict[str, Any]],
        sb.table("funders")
        .select("id, name")
        .eq("name", "Sample Tier-A Funder")
        .execute()
        .data
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

    if not will_write:
        print(f"  WOULD CREATE funder: {payload['name']}")
        print(
            "\nDry run complete. Re-run with --confirm to apply.\n"
            "  For --env=production also set AEGIS_ALLOW_PRODUCTION_SEED=true."
        )
        return 0

    r = sb.table("funders").insert(cast(dict[str, Any], payload)).execute()
    rows = cast(list[dict[str, Any]], r.data or [])
    print(f"  created funder: {payload['name']} ({rows[0]['id']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
