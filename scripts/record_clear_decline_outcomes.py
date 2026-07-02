#!/usr/bin/env python3
"""Record ``declined`` outcomes for clear-decline merchants (2026-07-01 B1).

Some merchants have been operator-declined outside AEGIS (never
submitted, no funder reply, no scored decision) — the operator makes
the call from their notes / gut and moves on. Those declines still
need to land in ground truth so the calibration engine sees the
signal.

Migration 106 added ``merchant_outcomes`` — merchant-scope,
anchor-free outcome capture. This script inserts one
``source='backfill'`` row per named merchant in the list below.

Idempotent: a merchant that already has a ``declined`` row is skipped
(counts + name are reported so the operator sees which of the four
were already recorded).

Usage
-----

    # Dry-run — no writes:
    uv run python scripts/record_clear_decline_outcomes.py --dry-run

    # Apply:
    uv run python scripts/record_clear_decline_outcomes.py --apply

Requires the standard AEGIS env (``SUPABASE_URL`` / ``SUPABASE_KEY`` /
``AEGIS_DATA_RESIDENCY_CONFIRMED=true``).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, cast

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

_env_file = _REPO_ROOT / ".env"
if _env_file.exists():
    _raw = _env_file.read_bytes()
    if _raw.startswith(b"\xff\xfe"):
        _text = _raw.decode("utf-16-le")
    elif _raw.startswith(b"\xfe\xff"):
        _text = _raw.decode("utf-16-be")
    elif _raw.startswith(b"\xef\xbb\xbf"):
        _text = _raw[3:].decode("utf-8")
    else:
        _text = _raw.decode("utf-8", errors="replace")
    for _line in _text.splitlines():
        _line = _line.strip().lstrip("﻿")
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from aegis.db import get_supabase  # noqa: E402

# Substring name matches (case-insensitive). Each corresponds to a
# merchant the operator declined outside AEGIS on 2026-07-01. The
# ``matches`` list below is queried with ``ilike`` so partial names
# resolve without demanding exact registered names.
CLEAR_DECLINES: list[dict[str, str]] = [
    {
        "name": "noble horse",
        "notes": "Operator decline (2026-07-01) — reason logged outside AEGIS.",
    },
    {
        "name": "showcase",
        "notes": "Operator decline (2026-07-01) — reason logged outside AEGIS.",
    },
    {
        "name": "turnbull",
        "notes": "Operator decline (2026-07-01) — reason logged outside AEGIS.",
    },
    {
        "name": "banda towing",
        "notes": "Operator decline (2026-07-01) — reason logged outside AEGIS.",
    },
]


def _rows(result: object) -> list[dict[str, Any]]:
    data = cast(Any, result).data
    return cast(list[dict[str, Any]], data or [])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Persist the rows.")
    mode.add_argument("--dry-run", action="store_true", help="Default — report only.")
    args = parser.parse_args()
    apply = bool(args.apply)

    sb = get_supabase()
    print(f"# mode={'APPLY' if apply else 'DRY-RUN'}")

    inserted = 0
    already = 0
    unresolved: list[str] = []

    for entry in CLEAR_DECLINES:
        name = entry["name"]
        notes = entry["notes"]
        matches = _rows(
            sb.table("merchants")
            .select("id,business_name")
            .ilike("business_name", f"%{name}%")
            .execute()
        )
        if not matches:
            unresolved.append(name)
            continue
        if len(matches) > 1:
            display = ", ".join(str(m.get("business_name") or "")[:40] for m in matches)
            print(
                f"  ! '{name}' matched {len(matches)} merchants: {display} (skipping — ambiguous)"
            )
            unresolved.append(name)
            continue

        merchant_row = matches[0]
        merchant_id = merchant_row["id"]
        business_name = merchant_row.get("business_name") or "(unknown)"

        # Idempotency: skip if this merchant already has a declined row.
        existing = _rows(
            sb.table("merchant_outcomes")
            .select("id")
            .eq("merchant_id", merchant_id)
            .eq("outcome", "declined")
            .limit(1)
            .execute()
        )
        if existing:
            print(f"  = '{business_name}' already has declined outcome — skip")
            already += 1
            continue

        payload: dict[str, Any] = {
            "merchant_id": merchant_id,
            "outcome": "declined",
            "source": "backfill",
            "notes": notes,
            "recorded_by": "system:record_clear_decline_outcomes",
        }
        if apply:
            try:
                sb.table("merchant_outcomes").insert(cast(Any, payload)).execute()
            except Exception as exc:
                print(f"  ! insert failed for '{business_name}': {exc}")
                continue
        print(f"  + '{business_name}' declined ({'written' if apply else 'planned'})")
        inserted += 1

    print()
    print(
        f"# RESULT mode={'APPLY' if apply else 'DRY-RUN'}\n"
        f"  inserted:  {inserted}\n"
        f"  already:   {already}\n"
        f"  unresolved: {len(unresolved)}"
    )
    if unresolved:
        print("  unresolved names:")
        for n in unresolved:
            print(f"    - {n}")
    return 0 if not unresolved else 3


if __name__ == "__main__":
    sys.exit(main())
