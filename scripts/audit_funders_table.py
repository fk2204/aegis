# ruff: noqa: E501
"""Diagnostic: list every funders row + flag duplicates.

Reads from the production funders table via the runner's DSN env var.
Read-only — no writes. Use:

    ssh aegis@aegis-ssh.commerafunding.com 'cd /opt/aegis && bash -c "set -a; source /etc/aegis/aegis.env; set +a; .venv/bin/python scripts/audit_funders_table.py"'

Output:
  1. Every row's name + id + key fields + created_at
  2. Duplicate-name groups (same name, multiple ids)
  3. Total row count
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg


def _load_dotenv() -> None:
    """Load /opt/aegis/.env (where MIGRATIONS_DB_URL_PROD lives on the
    box) without overwriting existing env. Mirrors apply_migrations.py."""
    for path in (Path(".env"), Path(".env.local")):
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def main() -> int:
    _load_dotenv()
    dsn = (
        os.environ.get("MIGRATIONS_DB_URL_PROD")
        or os.environ.get("DATABASE_URL")
        or ""
    )
    if not dsn:
        print("ERROR: MIGRATIONS_DB_URL_PROD or DATABASE_URL must be set", file=sys.stderr)
        return 2

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, active, jsonb_array_length(tiers) as tier_count, "
            "left(notes_residual, 60) as notes "
            "FROM funders ORDER BY name"
        )
        rows = cur.fetchall()

        print(f"--- funders table ({len(rows)} rows) ---")
        for r in rows:
            tier_str = f"tiers={r[3]:>1}" if r[3] else "tiers=0"
            print(
                f"  {str(r[0])[:8]} {r[1]:<32} active={r[2]}  {tier_str}  "
                f"notes={(r[4] or '')[:50]!r}"
            )

        cur.execute(
            "SELECT name, COUNT(*) FROM funders "
            "GROUP BY name HAVING COUNT(*) > 1 ORDER BY name"
        )
        dupes = cur.fetchall()
        if dupes:
            print(f"\n--- DUPLICATE NAMES ({len(dupes)} names with >1 row) ---")
            for name, count in dupes:
                print(f"  {name}: {count} rows")
        else:
            print("\n--- no duplicate names ---")

        cur.execute(
            "SELECT name, tiers FROM funders "
            "WHERE jsonb_array_length(tiers) > 0 ORDER BY name"
        )
        tiered = cur.fetchall()
        if tiered:
            print(f"\n--- TIER BREAKDOWN ({len(tiered)} tiered funders) ---")
            for name, tiers in tiered:
                print(f"  {name}:")
                for t in tiers:
                    fields = []
                    if t.get("buy_rate_low"):
                        fields.append(f"buy={t['buy_rate_low']}")
                    if t.get("min_credit_score"):
                        fields.append(f"fico={t['min_credit_score']}")
                    if t.get("min_months_in_business"):
                        fields.append(f"tib={t['min_months_in_business']}mo")
                    if t.get("min_monthly_revenue"):
                        fields.append(f"rev=${t['min_monthly_revenue']}")
                    if t.get("max_advance"):
                        fields.append(f"max=${t['max_advance']}")
                    if t.get("max_positions"):
                        fields.append(f"pos={t['max_positions']}")
                    print(f"    - {t['name']:<26} {' '.join(fields)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
