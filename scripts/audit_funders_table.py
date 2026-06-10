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

import psycopg


def main() -> int:
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
            "SELECT id, name, active, created_at, "
            "left(notes_residual, 60) as notes "
            "FROM funders ORDER BY name, created_at"
        )
        rows = cur.fetchall()

        print(f"--- funders table ({len(rows)} rows) ---")
        for r in rows:
            print(
                f"  {str(r[0])[:8]} {r[1]:<32} active={r[2]}  "
                f"created={r[3].isoformat() if r[3] else 'NULL'}  "
                f"notes={r[4]!r}"
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
