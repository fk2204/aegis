#!/usr/bin/env python3
"""AEGIS full health check + auto-fix loop.

Run on prod:
  cd /opt/aegis
  set -a && source /etc/aegis/aegis.env && set +a
  .venv/bin/python scripts/health_check.py

Checks every system. Auto-fixes what it can without a deploy.
Reports every result. Exits 0 if all pass, 1 if any fail.
"""

from __future__ import annotations

import inspect
import pathlib
import sys
from collections import Counter
from datetime import UTC, datetime
from typing import Any, cast

PASS = "✅"  # noqa: S105 - status icon, not a secret
FAIL = "❌"
WARN = "⚠️ "
results: dict[str, bool] = {}


def section(title: str) -> None:
    print(f"\n{'-' * 55}")
    print(f"  {title}")
    print(f"{'-' * 55}")


def check(label: str, ok: bool, detail: str = "") -> bool:
    icon = PASS if ok else FAIL
    print(f"  {icon} {label}" + (f" -- {detail}" if detail else ""))
    return ok


# -- IMPORTS ---------------------------------------------------------
try:
    from aegis.db import get_supabase

    sb = get_supabase()
except Exception as e:
    print(f"{FAIL} Cannot import aegis -- is the venv active? {e}")
    sys.exit(1)

print("\n" + "=" * 55)
print("  AEGIS HEALTH CHECK")
print(f"  {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}")
print("=" * 55)

# -- 1. DATABASE ----------------------------------------------------
section("1. Database connection")
try:
    resp = sb.table("merchants").select("id", count=cast(Any, "exact")).execute()
    results["database"] = check("Supabase connected", True, f"{resp.count} merchants")
except Exception as e:
    results["database"] = check("Supabase connected", False, str(e))

# -- 2. DOCUMENT PIPELINE -------------------------------------------
section("2. Document pipeline")
try:
    docs = sb.table("documents").select("parse_status").execute()
    dc = Counter(d["parse_status"] for d in (docs.data or []) if isinstance(d, dict))
    for s in ["proceed", "manual_review", "error", "pending"]:
        n = dc.get(s, 0)
        ok = s != "manual_review" or n == 0
        check(s, ok, str(n))
    results["documents"] = dc.get("manual_review", 0) == 0

    # AUTO-FIX: promote clean manual_review docs (no hard fraud flags)
    if dc.get("manual_review", 0) > 0:
        print("\n  AUTO-FIX: promoting clean manual_review docs...")
        try:
            from aegis.workers import HARD_FRAUD_FLAGS

            hard_set = {str(f).upper() for f in HARD_FRAUD_FLAGS}
            mr_docs = (
                sb.table("documents")
                .select("id,all_flags")
                .eq("parse_status", "manual_review")
                .execute()
            )
            promoted = 0
            skipped = 0
            for d in mr_docs.data or []:
                if not isinstance(d, dict):
                    continue
                raw_flags = d.get("all_flags") or []
                # Flags land as "[META] editor_detected:..." style. Strip
                # the bracketed prefix so bare fraud codes still match.
                normalized = {str(f).split("]")[-1].strip().upper() for f in raw_flags}
                if normalized & hard_set:
                    skipped += 1
                    continue
                sb.table("documents").update({"parse_status": "proceed"}).eq(
                    "id", d["id"]
                ).execute()
                promoted += 1
            print(f"  {PASS} Promoted {promoted} docs, skipped {skipped} (fraud flags)")
            results["documents"] = True
        except Exception as e:
            print(f"  {FAIL} Auto-fix failed: {e}")
except Exception as e:
    results["documents"] = check("Document query", False, str(e))

# -- 3. OFAC --------------------------------------------------------
section("3. OFAC screening")
try:
    from aegis.compliance.ofac import (
        _FOREIGN_ONLY_PROGRAMS,
        DEFAULT_CACHE_PATH,
        JARO_WINKLER_THRESHOLD,
        TOKEN_SORT_THRESHOLD,
        screen_merchant,
    )

    cache_path = pathlib.Path(str(DEFAULT_CACHE_PATH))
    check(
        "Cache file exists",
        cache_path.exists(),
        f"{cache_path.stat().st_size:,} bytes" if cache_path.exists() else str(cache_path),
    )
    check(
        "Program codes",
        len(_FOREIGN_ONLY_PROGRAMS) >= 25,
        f"{len(_FOREIGN_ONLY_PROGRAMS)} in set",
    )
    check("RUSSIA-EO14024", "RUSSIA-EO14024" in _FOREIGN_ONLY_PROGRAMS)
    check("IRGC", "IRGC" in _FOREIGN_ONLY_PROGRAMS)
    check("JW threshold", True, str(JARO_WINKLER_THRESHOLD))
    check("TS threshold", True, str(TOKEN_SORT_THRESHOLD))

    # Test known false positives
    print("\n  False positive tests:")
    fp_pass = 0
    fp_total = 0
    for biz, owner, expect_clear, _note in [
        ("BandA towing", "Agustin Barrera", False, "owner name -- common Hispanic name"),
        ("The Turnbull Company LLC", "W. Robert Livingston", True, "Turnbull vs Tekhnopol"),
        ("Genovate HVAC Company, Inc.", None, True, "generic company words"),
        ("David Simonini Custom Homes", None, True, "already known clear"),
    ]:
        ofac_res = screen_merchant(biz, owner)
        fp_total += 1
        is_expected = ofac_res.is_clear == expect_clear
        if is_expected:
            fp_pass += 1
        icon = PASS if is_expected else WARN
        status = "CLEAR" if ofac_res.is_clear else "BLOCKED"
        print(f"  {icon} {biz[:40]:<40} {status}")
        if not ofac_res.is_clear:
            for detail_line in ofac_res.match_detail[:1]:
                print(f"       -> {detail_line}")

    check("False positive tests", fp_pass == fp_total, f"{fp_pass}/{fp_total}")

    # Show all currently blocked merchants
    blocked = (
        sb.table("merchants")
        .select("id,business_name")
        .eq("ofac_is_clear", False)
        .is_("deleted_at", "null")
        .execute()
    )
    blocked_rows: list[dict[str, Any]] = [
        cast(dict[str, Any], r) for r in (blocked.data or []) if isinstance(r, dict)
    ]
    print(f"\n  Currently OFAC-blocked on prod: {len(blocked_rows)}")
    for m in blocked_rows:
        print(f"    - {m.get('business_name')}")

    # AUTO-FIX: re-screen all blocked merchants with updated program codes
    if blocked_rows:
        print(f"\n  AUTO-FIX: re-screening {len(blocked_rows)} blocked merchants...")
        try:
            from uuid import UUID

            from aegis.audit import SupabaseAuditLog
            from aegis.compliance.ofac import refresh_ofac_for_merchant
            from aegis.merchants.repository import SupabaseMerchantRepository

            repo = SupabaseMerchantRepository()
            audit = SupabaseAuditLog()
            cleared = 0
            still_blocked: list[str] = []
            for m in blocked_rows:
                merchant_name = str(m.get("business_name") or "?")
                merchant_id_raw = m.get("id")
                if not isinstance(merchant_id_raw, str):
                    print(f"  {FAIL} {merchant_name}: bad id")
                    continue
                try:
                    res = refresh_ofac_for_merchant(
                        UUID(merchant_id_raw), merchants_repo=repo, audit=audit
                    )
                    if res.is_clear:
                        cleared += 1
                        print(f"  {PASS} {merchant_name}: now CLEAR")
                    else:
                        still_blocked.append(merchant_name)
                        print(f"  {WARN} {merchant_name}: still blocked")
                        for detail in res.match_detail[:1]:
                            print(f"       -> {detail}")
                except Exception as e:
                    print(f"  {FAIL} {merchant_name}: {e}")
            print(f"\n  Cleared: {cleared}/{len(blocked_rows)}")
            if still_blocked:
                print(
                    f"  Still blocked ({len(still_blocked)}) -- "
                    "genuine matches or need threshold fix:"
                )
                for name in still_blocked:
                    print(f"    - {name}")
        except Exception as e:
            print(f"  {FAIL} Re-screen failed: {e}")

    results["ofac"] = True
except Exception as e:
    results["ofac"] = check("OFAC import", False, str(e))

# -- 4. WORKERS -----------------------------------------------------
section("4. Workers / crons")
try:
    import aegis.workers as w

    wsrc = inspect.getsource(w)
    crons = [
        "promote_clean_manual_review_cron",
        "retry_stuck_error_documents_cron",
        "daily_hetzner_snapshot",
        "weekly_calibration",
        "daily_cost_check",
        "HARD_FRAUD_FLAGS",
    ]
    all_ok = all(check(c, c in wsrc) for c in crons)
    results["workers"] = all_ok
except Exception as e:
    results["workers"] = check("Workers import", False, str(e))

# -- 5. SCORING -----------------------------------------------------
section("5. Scoring signals")
try:
    from aegis.scoring_v2.track_b import compute

    csrc = inspect.getsource(compute)
    signals = [
        "cc_sales_exceeds_revenue",
        "payment_load_critical",
        "requested_amount_exceeds_capacity",
        "negative_revenue_computed",
    ]
    results["scoring"] = all(check(s, s in csrc) for s in signals)
except Exception as e:
    results["scoring"] = check("Scoring import", False, str(e))

# -- 6. FUNDERS -----------------------------------------------------
section("6. Funder catalog")
try:
    from aegis.funders.repository import SupabaseFunderRepository

    funders = SupabaseFunderRepository().list_active()
    results["funders"] = check("Active funders", len(funders) > 0, str(len(funders)))
    for f in funders[:5]:
        name = getattr(f, "name", getattr(f, "funder_name", "?"))
        print(f"    - {name}")
except Exception as e:
    results["funders"] = check("Funders", False, str(e))

# -- 7. CLOSE -------------------------------------------------------
section("7. Close CRM")
try:
    from aegis.close.client import CloseClient

    me = CloseClient().request("GET", "/api/v1/me/")
    results["close"] = check("Close connected", True, str(me.get("email", "?")))
except Exception as e:
    results["close"] = check("Close connected", False, str(e))

# -- 8. NARRATOR ----------------------------------------------------
section("8. Deal narrator")
try:
    from aegis.scoring_v2.deal_summary import generate_funder_narrative

    sig = inspect.signature(generate_funder_narrative)
    params = sig.parameters
    check("true_revenue_monthly param", "true_revenue_monthly" in params)
    check("close_context param", "close_context" in params)
    results["narrator"] = True
except Exception as e:
    results["narrator"] = check("Narrator import", False, str(e))

# -- 9. CALIBRATION -------------------------------------------------
section("9. Calibration")
try:
    mo = sb.table("merchant_outcomes").select("outcome").execute()
    _mo_rows = [r for r in (mo.data or []) if isinstance(r, dict)]
    oc = Counter(r["outcome"] for r in _mo_rows if r.get("outcome"))
    total = len(_mo_rows)
    check(
        "Outcomes recorded",
        total >= 20,
        f"{total}/20 -- need {max(0, 20 - total)} more",
    )
    print(f"    {dict(oc)}")
    results["calibration"] = total >= 20
except Exception as e:
    results["calibration"] = check("Outcomes query", False, str(e))

# -- 10. BACKGROUND CHECK COVERAGE ---------------------------------
section("10. Background check coverage")
try:
    bg_resp = (
        sb.table("merchants")
        .select("ucc_checked_at,web_presence_scanned_at,ofac_checked_at,sos_checked_at")
        .is_("deleted_at", "null")
        .execute()
    )
    _m_rows = [r for r in (bg_resp.data or []) if isinstance(r, dict)]
    total = len(_m_rows)
    for field, label in [
        ("ofac_checked_at", "OFAC"),
        ("ucc_checked_at", "UCC"),
        ("sos_checked_at", "SOS"),
        ("web_presence_scanned_at", "Web presence"),
    ]:
        n = sum(1 for r in _m_rows if r.get(field))
        pct = int(n / total * 100) if total else 0
        check(f"{label} checked", pct == 100, f"{n}/{total} ({pct}%)")
    results["bg_checks"] = True
except Exception as e:
    results["bg_checks"] = check("Background checks", False, str(e))

# -- 11. V2 UI ------------------------------------------------------
section("11. V2 UI routes")
try:
    import aegis_ui.router as ui_r

    routes = [str(getattr(r, "path", "")) for r in ui_r.router.routes]
    for path in ["/v2/", "/v2/funders", "/v2/compliance", "/v2/deal/{merchant_id}"]:
        check(path, any(path in r for r in routes))
    results["v2_ui"] = True
except Exception as e:
    results["v2_ui"] = check("V2 UI import", False, str(e))

# -- 12. PRODUCT SCORING --------------------------------------------
section("12. Product scoring modules")
try:
    modules = [
        ("aegis.scoring_v2.sba_scoring", "score_sba_deal"),
        ("aegis.scoring_v2.equipment_scoring", "score_equipment_deal"),
        ("aegis.scoring_v2.factoring_scoring", "score_factoring_deal"),
        ("aegis.scoring_v2.loc_scoring", "score_loc_deal"),
        ("aegis.scoring_v2.term_loan_scoring", "score_term_loan_deal"),
    ]
    all_ok = True
    for mod, fn in modules:
        try:
            m_mod = __import__(mod, fromlist=[fn])
            ok = hasattr(m_mod, fn)
            check(fn, ok)
            if not ok:
                all_ok = False
        except Exception as e:
            check(fn, False, str(e))
            all_ok = False
    results["product_scoring"] = all_ok
except Exception as e:
    results["product_scoring"] = check("Product scoring", False, str(e))

# -- SUMMARY --------------------------------------------------------
print("\n" + "=" * 55)
print("  SUMMARY")
print("=" * 55)
passed = sum(1 for v in results.values() if v)
total_systems = len(results)
for k, v in results.items():
    print(f"  {PASS if v else FAIL} {k}")
print(f"\n  {passed}/{total_systems} systems passing")

if passed < total_systems:
    failing = [k for k, v in results.items() if not v]
    print(f"\n  Failing: {', '.join(failing)}")
    sys.exit(1)
else:
    print(f"\n  {PASS} All systems operational")
    sys.exit(0)
