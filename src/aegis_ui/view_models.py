"""aegis_ui/view_models.py — wired to real AEGIS data (Step 3 of migration).

All TODO(data) resolved against verified field names:
  * MerchantRow:  aegis.merchants.models.MerchantRow  (Pydantic-strict)
  * AnalysisRow:  aegis.storage.AnalysisRow           (Pydantic-strict)
  * FunderRow:    aegis.funders.models.FunderRow
  * FunderMatch:  aegis.scoring.models.FunderMatch    (+ EstimatedTerms)

Templates stay dumb; every shaping decision lives here.

Deferred TODO(data) still present:
  * ``paper_grade`` / ``track_a_verdict`` — live on ``ScoreResult``, not
    on AnalysisRow. Surfacing them requires threading the scoring pass
    through the v2 deal route (out of scope for this PR — a future step
    extracts the scoring orchestrator from ``web/routers/merchants.py``
    ``merchant_detail`` into a service the v2 route can call cheaply).
    Today the verdict is derived from persisted OFAC + UCC + Track B
    band, which covers the block / review / proceed axis without
    re-running the scorer.
  * ``classification_coverage_pct`` / ``classification_buckets`` —
    same story; those are computed in
    ``scoring_v2/dossier_panel.build_unified_tracks_view``. Left empty
    with a friendly caption until the same extraction happens.
"""

from __future__ import annotations

from typing import Any

PRODUCTS = frozenset({"rbf", "term", "loc", "equipment", "abl", "factoring"})

# ProductType (aegis.product_types) -> 6-slot risk-module code the risk
# dispatcher switches on. The dispatcher's ``_dispatch.html.j2`` covers
# ``rbf``, ``term``, ``loc``, ``equipment``, ``factoring``, ``abl``.
_PRODUCT_MAP: dict[str, str] = {
    "revenue_based": "rbf",
    "business_loan": "term",
    "line_of_credit": "loc",
    "equipment": "equipment",
    "receivables": "factoring",
    "asset_based": "abl",
}


def _fmt_money(val: Any) -> str:
    if val is None or val == "":
        return "-"
    try:
        return f"${float(val):,.0f}"
    except (TypeError, ValueError):
        return str(val)


def _deal_subline(merchant: Any, product_raw: str) -> str:
    state = getattr(merchant, "state", "") or "-"
    industry = getattr(merchant, "industry_choice", None) or product_raw
    return f"{state} · {industry.replace('_', ' ').title()}"


def _fmt_pct(val: Any) -> str:
    if val is None:
        return "-"
    try:
        return f"{float(val) * 100:.1f}%"
    except (TypeError, ValueError):
        return str(val)


# ============================================================ TODAY
def build_today_view(pipeline: Any) -> dict[str, Any]:
    """Shape a merchant pipeline list into the Today view's KPI + queue.

    ``pipeline`` is a list of dicts (or None):
        {"merchant": MerchantRow, "analysis": AnalysisRow | None,
         "documents": list[DocumentRow]}
    """
    if not pipeline:
        return {"kpis": [], "mix": [], "queue": []}

    queue: list[dict[str, Any]] = []
    product_counts: dict[str, int] = {}

    for item in pipeline:
        merchant = item.get("merchant") if isinstance(item, dict) else item
        analysis = item.get("analysis") if isinstance(item, dict) else None
        docs = (item.get("documents") if isinstance(item, dict) else None) or []

        product_raw = getattr(merchant, "product_type", None) or "revenue_based"
        product = _PRODUCT_MAP.get(product_raw, "rbf")
        product_counts[product] = product_counts.get(product, 0) + 1

        band = getattr(analysis, "business_risk_band", None) if analysis else None
        ofac_clear = getattr(merchant, "ofac_is_clear", None)
        ucc_defaults = getattr(merchant, "ucc_default_indicators", None) or []

        if ofac_clear is False or band == "high":
            status_kind = "k"
            status_text = "Block"
            verdict_kind = "k"
        elif ucc_defaults or band == "elevated":
            status_kind = "w"
            status_text = "Review"
            verdict_kind = "w"
        elif band in ("moderate", "low"):
            status_kind = "p"
            status_text = "Proceed"
            verdict_kind = "p"
        else:
            status_kind = "mut"
            status_text = "Pending"
            verdict_kind = "mut"

        why = ""
        if ofac_clear is False:
            why = "OFAC match - funder matching blocked"
        elif ucc_defaults:
            why = f"{len(ucc_defaults)} previous default indicator(s)"
        elif band == "high":
            why = "Track B: high risk band"
        elif band:
            why = f"Track B: {band} risk band"

        proceed_count = sum(1 for d in docs if getattr(d, "parse_status", None) == "proceed")

        queue.append(
            {
                "deal_id": str(getattr(merchant, "id", "") or ""),
                "name": getattr(merchant, "business_name", "") or "Unknown",
                "product": product,
                "status_text": status_text,
                "status_kind": status_kind,
                "meta": (
                    f"{_fmt_money(getattr(merchant, 'requested_amount', None))} · "
                    f"{getattr(merchant, 'state', '') or '-'} · "
                    f"{proceed_count} doc(s)"
                ),
                "why": why,
                "score_value": band or "-",
                "score_label": "band",
                "verdict_kind": verdict_kind,
            }
        )

    total = len(queue)
    product_colors = {
        "rbf": "#0f3d2e",
        "term": "#2a5c86",
        "loc": "#6b4e8f",
        "equipment": "#a55a06",
        "factoring": "#b42318",
        "abl": "#657069",
    }
    mix = [
        {
            "product": k,
            "count": v,
            "pct": round(v / total * 100) if total else 0,
            "color": product_colors.get(k, "#657069"),
        }
        for k, v in sorted(product_counts.items(), key=lambda x: -x[1])
    ]

    proceed_total = sum(1 for q in queue if q["status_kind"] == "p")
    block_total = sum(1 for q in queue if q["status_kind"] == "k")

    kpis = [
        {"label": "Active deals", "value": str(total), "detail": "in pipeline", "is_alert": False},
        {
            "label": "Ready to submit",
            "value": str(proceed_total),
            "detail": "proceed verdict",
            "is_alert": False,
        },
        {
            "label": "Blocked",
            "value": str(block_total),
            "detail": "need review",
            "is_alert": block_total > 0,
        },
    ]

    return {"kpis": kpis, "mix": mix, "queue": queue}


# ============================================================ DEAL
def build_deal_view(
    merchant: Any,
    analysis: Any = None,
    documents: list[Any] | None = None,
    background_ctx: dict[str, Any] | None = None,
    funder_matches: list[Any] | None = None,
) -> dict[str, Any]:
    """Shape a single deal for the v2 dossier."""
    if merchant is None:
        return _empty_deal_view()

    product_raw = getattr(merchant, "product_type", None) or "revenue_based"
    product = _PRODUCT_MAP.get(product_raw, "rbf")

    # -- Verdict --
    band = getattr(analysis, "business_risk_band", None) if analysis else None
    ofac_clear = getattr(merchant, "ofac_is_clear", None)
    ucc_defaults = getattr(merchant, "ucc_default_indicators", None) or []
    ucc_filings = getattr(merchant, "ucc_filings", None) or []

    if ofac_clear is False:
        verdict_kind = "block"
        rec = "Block"
        lead = "OFAC Sanctions Match"
        why = (
            "This merchant matched the OFAC Specially Designated Nationals "
            "list. Funder matching is suppressed pending compliance review."
        )
    elif ucc_defaults:
        verdict_kind = "block"
        rec = "Block"
        lead = "Previous Default Found"
        why = (
            f"Background check found {len(ucc_defaults)} previous default "
            f"indicator(s). Most funders will decline."
        )
    elif band == "high":
        verdict_kind = "block"
        rec = "Block"
        lead = "Track B: High Risk"
        why = "Business-risk band lands at HIGH; do not submit without operator override."
    elif band == "elevated":
        verdict_kind = "review"
        rec = "Review"
        lead = "Track B: Elevated"
        why = "Elevated risk factors present. Manual review before submitting."
    elif band in ("moderate", "low"):
        verdict_kind = "go"
        rec = "Proceed"
        lead = f"Track B: {band.title()}"
        why = "Scoring cleared. Match to funders and submit."
    else:
        verdict_kind = "review"
        rec = "Pending"
        lead = "Awaiting analysis"
        why = "Upload bank statements to generate a scoring verdict."

    # -- Gates --
    gates: list[dict[str, Any]] = []
    if ofac_clear is False:
        gates.append(
            {
                "severity": "kill",
                "title": "OFAC Sanctions Match",
                "body": (
                    "This merchant appears on the OFAC Specially Designated "
                    "Nationals list. No funder submission is permitted until "
                    "compliance clears the match."
                ),
                "action_label": "Review in Compliance",
                "action_url": "/v2/compliance",
            }
        )
    if ucc_defaults:
        gates.append(
            {
                "severity": "kill",
                "title": f"Previous Default - {len(ucc_defaults)} indicator(s)",
                "body": (
                    "UCC background check found prior default indicators. "
                    "Disclose to any funder before submitting."
                ),
                "action_label": None,
                "action_url": None,
            }
        )
    if len(ucc_filings) >= 3:
        gates.append(
            {
                "severity": "amber",
                "title": f"{len(ucc_filings)} UCC Liens Filed",
                "body": (
                    "Multiple secured creditors have claims on business "
                    "assets. Funder may require lien subordination or payoff."
                ),
                "action_label": None,
                "action_url": None,
            }
        )

    # -- Signals --
    kill_signals: list[dict[str, Any]] = []
    weaken_signals: list[dict[str, Any]] = []
    favor_signals: list[dict[str, Any]] = []

    if analysis is not None:
        pa = getattr(analysis, "pattern_analysis", None)
        if pa is not None:
            for p in list(getattr(pa, "patterns", ()) or ()):
                code = getattr(p, "code", "") or ""
                detail = getattr(p, "detail", "") or ""
                sev = getattr(p, "severity", 0) or 0
                title = code.replace("_", " ").title() if code else "Pattern"
                bucket = kill_signals if sev >= 70 else weaken_signals
                bucket.append(
                    {
                        "title": title,
                        "detail": detail,
                        "severity": "k" if sev >= 70 else "w",
                    }
                )

    if ucc_defaults:
        kill_signals.append(
            {
                "title": "Previous Default Found",
                "detail": ", ".join(str(d) for d in ucc_defaults[:3]),
                "severity": "k",
            }
        )
    if len(ucc_filings) >= 3:
        weaken_signals.append(
            {
                "title": f"{len(ucc_filings)} UCC Liens",
                "detail": "Secured creditors have priority on business assets.",
                "severity": "w",
            }
        )

    if analysis is not None:
        num_nsf = getattr(analysis, "num_nsf", 0) or 0
        if num_nsf == 0:
            favor_signals.append(
                {
                    "title": "Zero NSFs",
                    "detail": "No non-sufficient-funds events in this window.",
                    "severity": "p",
                }
            )
        if getattr(analysis, "payroll_detected", False):
            favor_signals.append(
                {
                    "title": "Payroll Detected",
                    "detail": "Regular payroll ACH - indicates operating business.",
                    "severity": "p",
                }
            )

    # -- Background checks --
    _bg = background_ctx or {}
    _ = _bg  # reserved for future SOS/UCC operator-verified context
    ofac_matches = list(getattr(merchant, "ofac_match_detail", None) or [])
    sos_active = getattr(merchant, "sos_is_active", None)
    background = [
        {
            "name": "OFAC",
            "what": "Sanctions screening - SDN + Consolidated lists",
            "status": "st-clear"
            if ofac_clear is True
            else ("st-match" if ofac_clear is False else "st-notrun"),
            "finding": "Clear"
            if ofac_clear is True
            else (f"Match - {len(ofac_matches)} hit(s)" if ofac_clear is False else "Not checked"),
            "detail": ofac_matches[:4] if ofac_clear is False else None,
        },
        {
            "name": "UCC",
            "what": "Liens and prior defaults",
            "status": (
                "st-match"
                if ucc_defaults
                else (
                    "st-review"
                    if ucc_filings
                    else ("st-clear" if getattr(merchant, "ucc_checked_at", None) else "st-notrun")
                )
            ),
            "finding": (
                f"{len(ucc_defaults)} default(s) · {len(ucc_filings)} lien(s)"
                if getattr(merchant, "ucc_checked_at", None)
                else "Not checked"
            ),
        },
        {
            "name": "SOS",
            "what": "Secretary of State entity status",
            "status": (
                "st-clear"
                if sos_active is True
                else ("st-review" if sos_active is False else "st-notrun")
            ),
            "finding": (
                (getattr(merchant, "sos_status", None) or "Active")
                if sos_active is True
                else (getattr(merchant, "sos_status", None) or "Not checked")
            ),
        },
        {
            "name": "Web presence",
            "what": "Reputation & legitimacy signals",
            "status": "st-clear"
            if getattr(merchant, "web_presence_scanned_at", None)
            else "st-notrun",
            "finding": (
                (getattr(merchant, "web_presence_summary", None) or "")[:80] or "Not checked"
            ),
        },
    ]

    # -- Facts bar --
    tib = getattr(merchant, "time_in_business_months", None)
    bank_display = (
        (getattr(analysis, "bank_name", None) if analysis else None)
        or getattr(merchant, "stated_bank", None)
        or "-"
    )
    facts = [
        {"k": "Requested", "v": _fmt_money(getattr(merchant, "requested_amount", None))},
        {"k": "Stated rev", "v": _fmt_money(getattr(merchant, "monthly_revenue", None))},
        {
            "k": "True rev",
            "v": _fmt_money(getattr(analysis, "monthly_revenue", None) if analysis else None),
        },
        {"k": "FICO", "v": str(getattr(merchant, "credit_score", None) or "-")},
        {
            "k": "Positions",
            "v": str(
                getattr(merchant, "stated_mca_positions", None)
                if getattr(merchant, "stated_mca_positions", None) is not None
                else "-"
            ),
        },
        {"k": "TIB", "v": f"{tib}mo" if tib else "-"},
        {"k": "Bank", "v": bank_display},
        {"k": "State", "v": getattr(merchant, "state", None) or "-"},
    ]

    # -- Scores --
    integrity_pct = 96
    integrity_label = "Clean"
    if ofac_clear is False or ucc_defaults:
        integrity_pct = 15
        integrity_label = "Blocked"
    elif kill_signals:
        integrity_pct = 40
        integrity_label = "Concerns"

    dscr_val = "-"
    dscr_pct = 0
    revenue_month = getattr(analysis, "monthly_revenue", None) if analysis else None
    requested = getattr(merchant, "requested_amount", None)
    if revenue_month and requested:
        try:
            monthly_payment = float(requested) / 36.0
            noi = float(revenue_month) * 0.25
            if monthly_payment > 0:
                dscr = noi / monthly_payment
                dscr_val = f"{dscr:.2f}x"
                dscr_pct = min(int(dscr / 2.5 * 100), 100)
        except (TypeError, ValueError, ArithmeticError):
            pass

    scores = {
        "integrity": {"value": integrity_label, "pct": integrity_pct},
        "second": {
            "label": "Est. DSCR",
            "value": dscr_val,
            "color": "var(--pass)" if dscr_pct > 50 else "var(--warn)",
            "bar_pct": dscr_pct,
            "mark_pct": 50,
            "say": "1.25x minimum for term loans and LOC",
        },
    }

    # -- Application KV rows --
    lender_list = getattr(merchant, "stated_current_lenders", None) or []
    application_rows = [
        {"k": "Legal name", "v": getattr(merchant, "business_name", None) or "-"},
        {"k": "DBA", "v": getattr(merchant, "dba", None) or "-"},
        {"k": "EIN", "v": getattr(merchant, "ein", None) or "-"},
        {"k": "Entity", "v": (getattr(merchant, "entity_type", None) or "-").upper()},
        {"k": "State", "v": getattr(merchant, "state", None) or "-"},
        {"k": "Use of funds", "v": getattr(merchant, "use_of_funds", None) or "-"},
        {
            "k": "Current lenders",
            "v": ", ".join(lender_list) if lender_list else "None stated",
        },
        {"k": "MCA balance", "v": _fmt_money(getattr(merchant, "stated_mca_balance", None))},
        {"k": "Daily payment", "v": _fmt_money(getattr(merchant, "stated_daily_payment", None))},
        {"k": "Bank", "v": getattr(merchant, "stated_bank", None) or "-"},
        {"k": "Product", "v": product_raw.replace("_", " ").title()},
    ]

    # -- Document package checklist --
    required_docs = {
        "revenue_based": ["Bank statements", "Voided check", "Signed ISO", "Driver's license"],
        "business_loan": [
            "Tax returns",
            "P&L statement",
            "Balance sheet",
            "Bank statements",
            "Debt schedule",
        ],
        "equipment": ["Equipment invoice", "Bank statements", "Equipment specs", "Voided check"],
        "line_of_credit": ["A/R aging", "Bank statements", "Tax returns", "Voided check"],
        "receivables": ["A/R aging", "Sample invoices", "Customer list", "Bank statements"],
        "asset_based": ["A/R aging", "Inventory list", "Balance sheet", "Bank statements"],
    }
    doc_names = required_docs.get(product_raw, required_docs["revenue_based"])
    docs_uploaded: set[str] = set()
    for d in documents or []:
        fname = str(getattr(d, "original_filename", "") or "").lower()
        for name in doc_names:
            for keyword in name.lower().split()[:2]:
                if keyword and keyword in fname:
                    docs_uploaded.add(name)
                    break
    if getattr(merchant, "voided_check_on_file", False):
        docs_uploaded.add("Voided check")
    if getattr(merchant, "drivers_license_on_file", False):
        docs_uploaded.add("Driver's license")
    if (getattr(merchant, "bank_statements_months", 0) or 0) >= 3:
        docs_uploaded.add("Bank statements")
    doc_items = [{"name": n, "done": n in docs_uploaded} for n in doc_names]
    doc_pct = int(len(docs_uploaded) / len(doc_names) * 100) if doc_names else 0

    # -- Pricing rows from best funder match --
    pricing_rows: list[dict[str, Any]] = []
    pricing_note = ""
    if funder_matches:
        best = funder_matches[0]
        if isinstance(best, dict):
            est = best.get("estimated_terms")
            best_name = best.get("funder_name") or "best match"
        else:
            est = getattr(best, "estimated_terms", None)
            best_name = getattr(best, "funder_name", None) or "best match"
        if est is not None:
            pricing_rows = [
                {"k": "Advance", "v": _fmt_money(getattr(est, "estimated_advance", None))},
                {"k": "Factor", "v": str(getattr(est, "estimated_factor", "-"))},
                {"k": "Holdback", "v": _fmt_pct(getattr(est, "estimated_holdback_pct", None))},
                {
                    "k": "Daily payment",
                    "v": _fmt_money(getattr(est, "estimated_daily_payment", None)),
                },
                {"k": "APR", "v": _fmt_pct(getattr(est, "estimated_apr", None))},
            ]
            pricing_note = f"Based on {best_name}: {getattr(est, 'interpolation_evidence', '')}"

    return {
        "deal": {
            "id": str(getattr(merchant, "id", "") or ""),
            "product": product,
            "name": getattr(merchant, "business_name", None) or "",
            "code": str(getattr(merchant, "id", "") or "")[:8].upper(),
            "subline": _deal_subline(merchant, product_raw),
            "facts": facts,
        },
        "verdict": {
            "kind": verdict_kind,
            "rec": rec,
            "tag": band or "",
            "lead": lead,
            "why": why,
            "actions": [],
        },
        "gates": gates,
        "scores": scores,
        "signals": {"kill": kill_signals, "weaken": weaken_signals, "favor": favor_signals},
        "pricing": {"rows": pricing_rows, "note": pricing_note},
        "application": {"rows": application_rows},
        "package": {"pct": doc_pct, "items": doc_items},
        "background": background,
        "risk": build_risk_module(merchant, analysis, product),
        "funder_match": build_funder_match_view(funder_matches),
        **build_classification_view(analysis),
    }


def _empty_deal_view() -> dict[str, Any]:
    return {
        "deal": {"id": "", "product": "rbf", "name": "", "code": "", "subline": "", "facts": []},
        "verdict": {"kind": "review", "rec": "-", "tag": "", "lead": "", "why": "", "actions": []},
        "gates": [],
        "scores": {
            "integrity": {"value": "-", "pct": 0},
            "second": {
                "label": "",
                "value": "",
                "color": "var(--info)",
                "bar_pct": 0,
                "mark_pct": 0,
                "say": "",
            },
        },
        "signals": {"kill": [], "weaken": [], "favor": []},
        "pricing": {"rows": [], "note": ""},
        "application": {"rows": []},
        "package": {"pct": 0, "items": []},
        "background": [],
        "risk": {},
        "funder_match": {"type": "rbf", "items": [], "note": ""},
        "classification": {
            "coverage_pct": 0,
            "coverage_say": "",
            "buckets": [],
            "counterparties": [],
            "sample_txns": [],
            "callout_kind": "i",
            "callout": "",
        },
    }


# ============================================================ RISK MODULES
def build_risk_module(merchant: Any, analysis: Any, product: str) -> dict[str, Any]:
    """One dict per product; template dispatch selects the right sub-key."""
    revenue_month = getattr(analysis, "monthly_revenue", None) if analysis else None
    num_nsf = getattr(analysis, "num_nsf", None) if analysis else None
    mca_positions = getattr(analysis, "mca_positions", None) if analysis else None
    avg_balance = getattr(analysis, "avg_daily_balance", None) if analysis else None
    days_negative = getattr(analysis, "days_negative", None) if analysis else None
    statement_days = getattr(analysis, "statement_days", None) if analysis else None
    months_analyzed = (int(statement_days) // 30) if statement_days else None

    modules: dict[str, dict[str, Any]] = {
        "rbf": {
            "true_revenue": _fmt_money(revenue_month),
            "stated_revenue": _fmt_money(getattr(merchant, "monthly_revenue", None)),
            "nsf_count": num_nsf,
            "mca_confirmed": mca_positions,
            "avg_daily_balance": _fmt_money(avg_balance),
            "days_negative": days_negative,
            "months_analyzed": months_analyzed,
        },
        "term": {
            "true_revenue": _fmt_money(revenue_month),
            "requested": _fmt_money(getattr(merchant, "requested_amount", None)),
            "tib_months": getattr(merchant, "time_in_business_months", None),
            "fico": getattr(merchant, "credit_score", None),
        },
        "loc": {
            "true_revenue": _fmt_money(revenue_month),
            "avg_daily_balance": _fmt_money(avg_balance),
            "nsf_count": num_nsf,
        },
        "equipment": {
            "fico": getattr(merchant, "credit_score", None),
            "tib_months": getattr(merchant, "time_in_business_months", None),
            "use_of_funds": getattr(merchant, "use_of_funds", None),
        },
        "factoring": {
            "true_revenue": _fmt_money(revenue_month),
        },
        "abl": {
            "true_revenue": _fmt_money(revenue_month),
            "avg_daily_balance": _fmt_money(avg_balance),
        },
    }
    return modules.get(product, {})


# ============================================================ CLASSIFICATION
def build_classification_view(analysis: Any) -> dict[str, Any]:
    """Classification-coverage panel. Currently a placeholder - the real
    coverage figures live in ``scoring_v2.dossier_panel``. This surface
    reads a friendly caption until the same extraction happens.
    """
    if analysis is None:
        return {
            "classification": {
                "coverage_pct": 0,
                "coverage_say": "No analysis yet - upload bank statements.",
                "buckets": [],
                "counterparties": [],
                "sample_txns": [],
                "callout_kind": "i",
                "callout": "",
            }
        }

    return {
        "classification": {
            "coverage_pct": 0,
            "coverage_say": "Coverage detail pending extraction from scoring_v2.dossier_panel.",
            "buckets": [],
            "counterparties": [],
            "sample_txns": [],
            "callout_kind": "i",
            "callout": "",
        }
    }


# ============================================================ FUNDERS
def build_funders_view(data: Any, q: str = "", filt: str = "all") -> dict[str, Any]:
    """List page shaper. ``data`` is a list of ``FunderRow`` (or ``None``).

    Filter tokens: ``all`` / ``active`` / ``paused``.
    Search is a case-insensitive substring against ``name``.
    """
    if not data:
        return {"funders": [], "q": q, "filter": filt}

    q_lower = (q or "").strip().lower()
    funders_out: list[dict[str, Any]] = []
    for f in data:
        name = getattr(f, "name", "") or ""
        status = getattr(f, "operator_status", "active") or "active"
        deal_types = getattr(f, "deal_types_accepted", ()) or ()
        tiers = getattr(f, "tiers", ()) or ()

        if q_lower and q_lower not in name.lower():
            continue
        if filt != "all" and status != filt:
            continue

        best_tier = tiers[0] if tiers else None
        min_fico = (
            getattr(best_tier, "min_credit_score", None)
            if best_tier
            else getattr(f, "min_credit_score", None)
        )
        min_rev = (
            getattr(best_tier, "min_monthly_revenue", None)
            if best_tier
            else getattr(f, "min_monthly_revenue", None)
        )
        max_pos = (
            getattr(best_tier, "max_positions", None)
            if best_tier
            else getattr(f, "max_positions", None)
        )

        funders_out.append(
            {
                "id": str(getattr(f, "id", "") or ""),
                "name": name,
                "status": status,
                "deal_types": list(deal_types)[:4],
                "min_fico": min_fico,
                "min_revenue": _fmt_money(min_rev),
                "max_positions": max_pos,
            }
        )

    return {"funders": funders_out, "q": q, "filter": filt}


def build_funder_match_view(matches: list[Any] | None) -> dict[str, Any]:
    """Shape a list of matches for the deal-scoped funder table.

    Accepts either:
      * dicts from ``_match_card`` (dossier + v2 pipeline) — real prod path
      * Pydantic ``FunderMatch`` objects (tests, direct-call callers)

    Card shape from ``_match_card``:
        ``funder_name``, ``match_score``, ``color`` (red|yellow|green),
        ``hard_reasons``, ``soft_concerns``, ``estimated_terms`` (Pydantic).
    """
    if not matches:
        return {
            "type": "rbf",
            "items": [],
            "note": (
                "No funder matches - ensure bank statements are parsed, OFAC "
                "is clear, and product type is set."
            ),
        }

    def _read(m: Any, key: str, default: Any = None) -> Any:
        if isinstance(m, dict):
            return m.get(key, default)
        return getattr(m, key, default)

    items: list[dict[str, Any]] = []
    qualifying = 0
    for m in matches:
        color = _read(m, "color", "green")
        qualifies = color != "red"
        if qualifies:
            qualifying += 1
        est = _read(m, "estimated_terms")
        factor_val = None
        holdback_val = None
        daily_val = None
        apr_val = None
        if est is not None:
            factor_val = getattr(est, "estimated_factor", None)
            holdback_val = getattr(est, "estimated_holdback_pct", None)
            daily_val = getattr(est, "estimated_daily_payment", None)
            apr_val = getattr(est, "estimated_apr", None)

        hard_reasons = list(_read(m, "hard_reasons", []) or [])[:3]
        soft_concerns = list(_read(m, "soft_concerns", []) or [])[:3]

        items.append(
            {
                "name": _read(m, "funder_name", "") or "",
                "qualifies": qualifies,
                "match_score": _read(m, "match_score", 0),
                "hard_fails": hard_reasons,
                "soft_concerns": soft_concerns,
                "factor_rate": str(factor_val) if factor_val is not None else "-",
                "holdback_pct": _fmt_pct(holdback_val),
                "daily_payment": _fmt_money(daily_val),
                "apr": _fmt_pct(apr_val),
            }
        )

    return {
        "type": "rbf",
        "items": items,
        "note": f"{qualifying} of {len(items)} funder(s) qualify for this deal.",
    }


# ============================================================ COMPLIANCE
def build_compliance_view(ofac_rows: Any) -> dict[str, Any]:
    """Compliance workbench — list of merchants flagged by OFAC.

    ``ofac_rows`` may be a list of dicts (raw Supabase rows), a list of
    ``MerchantRow`` instances, or ``None``. Handles either shape via
    getattr/get. Real dashboard-grade metrics live in
    ``compliance.obligations``; this view surfaces the OFAC block queue
    as the first cut.
    """
    if not ofac_rows:
        return {"kpis": [], "ofac": [], "kyb": [], "states": [], "activity": []}

    ofac_items: list[dict[str, Any]] = []
    for row in ofac_rows:
        if isinstance(row, dict):
            name = row.get("business_name") or ""
            state = row.get("state") or ""
            mid = str(row.get("id") or "")
            matches = list(row.get("ofac_match_detail") or [])
        else:
            name = getattr(row, "business_name", "") or ""
            state = getattr(row, "state", "") or ""
            mid = str(getattr(row, "id", "") or "")
            matches = list(getattr(row, "ofac_match_detail", None) or [])
        ofac_items.append(
            {
                "id": mid,
                "name": name or "(unknown)",
                "state": state or "-",
                "match_count": len(matches),
                "matches": matches[:4],
                "status": "match",
            }
        )

    return {
        "kpis": [
            {
                "label": "OFAC flagged",
                "value": str(len(ofac_items)),
                "detail": "pending review",
                "is_alert": len(ofac_items) > 0,
            },
        ],
        "ofac": ofac_items,
        "kyb": [],
        "states": [],
        "activity": [],
    }


__all__ = [
    "build_classification_view",
    "build_compliance_view",
    "build_deal_view",
    "build_funder_match_view",
    "build_funders_view",
    "build_risk_module",
    "build_today_view",
]
