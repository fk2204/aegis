"""Re-sync ``product_type`` on all existing merchants via auto-detect.

Runs ``detect_product_type`` (aegis.close.field_map) against every
finalized merchant whose ``product_type`` is still the migration-080
default (``revenue_based``). Only up-transitions happen — a merchant
already tagged as ``equipment`` / ``business_loan`` / etc. is
untouched. Government-lender detection uses the same
``_is_government_lender`` keywords the field-map parser applies.

Safe to re-run: the detector returns ``None`` when the signals are
ambiguous, and we only UPDATE when a specific non-default type comes
back. Merchants that don't have ``use_of_funds`` / lenders / requested
amount stay put.

Usage on the box::

    systemd-run --uid=aegis --pipe --wait \\
      --property=EnvironmentFile=/etc/aegis/aegis.env \\
      /opt/aegis/.venv/bin/python /opt/aegis/scripts/resync_product_types.py

Or directly::

    uv run python scripts/resync_product_types.py            # apply
    uv run python scripts/resync_product_types.py --dry-run  # report only
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from aegis.close.field_map import detect_product_type
from aegis.db import get_supabase


def _rows(result: object) -> list[dict[str, Any]]:
    data = cast(Any, result).data
    return cast(list[dict[str, Any]], data or [])


def _to_decimal(val: object) -> Decimal | None:
    if val is None or val == "":
        return None
    try:
        return Decimal(str(val))
    except (TypeError, ValueError, InvalidOperation):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Persist product_type updates.")
    mode.add_argument("--dry-run", action="store_true", help="Report only. Default.")
    args = parser.parse_args()
    apply = bool(args.apply)

    sb = get_supabase()
    merchants = _rows(
        sb.table("merchants")
        .select(
            "id,business_name,product_type,use_of_funds,stated_current_lenders,requested_amount"
        )
        .is_("deleted_at", "null")
        .execute()
    )

    total = len(merchants)
    print(f"# mode={'APPLY' if apply else 'DRY-RUN'}  merchants={total}")

    updates: list[tuple[str, str, str]] = []
    skipped_already_typed = 0
    skipped_no_signal = 0

    for m in merchants:
        current = m.get("product_type") or "revenue_based"
        if current != "revenue_based":
            skipped_already_typed += 1
            continue

        lenders_raw = m.get("stated_current_lenders")
        if isinstance(lenders_raw, str):
            lenders_list = [ln.strip() for ln in lenders_raw.split(",") if ln.strip()]
        elif isinstance(lenders_raw, list):
            lenders_list = [str(ln).strip() for ln in lenders_raw if str(ln).strip()]
        else:
            lenders_list = []

        requested = _to_decimal(m.get("requested_amount"))

        detected = detect_product_type(
            use_of_funds=(m.get("use_of_funds") or None),
            current_lenders=lenders_list or None,
            requested_amount=requested,
        )
        if detected is None or detected == current:
            skipped_no_signal += 1
            continue

        mid = str(m.get("id") or "")
        name = str(m.get("business_name") or "")
        print(f"  {name[:40]:40s}  {current!r} -> {detected!r}")
        updates.append((mid, current, detected))

        if apply and mid:
            try:
                sb.table("merchants").update({"product_type": detected}).eq("id", mid).execute()
            except Exception as exc:
                print(f"    ! update failed: {exc}")

    print()
    print(
        f"# RESULT mode={'APPLY' if apply else 'DRY-RUN'}\n"
        f"  total:             {total}\n"
        f"  already_typed:     {skipped_already_typed}\n"
        f"  no_detected_signal:{skipped_no_signal}\n"
        f"  updated:           {len(updates)}"
    )
    if updates:
        new_dist: Counter[str] = Counter(dst for _, _, dst in updates)
        print("  new_type_distribution:")
        for t, n in new_dist.most_common():
            print(f"    {t}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
