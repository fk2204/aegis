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

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

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
def build_today_view(
    pipeline: Any,
    outcomes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Shape a merchant pipeline list into the Today view's KPI + queue.

    ``pipeline`` is a list of dicts (or None):
        {"merchant": MerchantRow, "analysis": AnalysisRow | None,
         "documents": list[DocumentRow]}

    ``outcomes`` is a list of ``merchant_outcomes`` rows (or None) —
    used to compute the "Funded · 7d" KPI. Each row: ``outcome``,
    ``funded_amount``, ``recorded_at``.
    """
    if not pipeline:
        return {
            "kpis": [],
            "mix": [],
            "queue": [],
            "total_requested": "$0",
            "queue_depth": 0,
            "funded_7d": "$0",
        }

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

        requested_raw = getattr(merchant, "requested_amount", None)
        try:
            requested_int = int(requested_raw) if requested_raw is not None else 0
        except (TypeError, ValueError, InvalidOperation):
            requested_int = 0

        queue.append(
            {
                "deal_id": str(getattr(merchant, "id", "") or ""),
                "name": getattr(merchant, "business_name", "") or "Unknown",
                "product": product,
                "status_text": status_text,
                "status_kind": status_kind,
                "meta": (
                    f"{_fmt_money(requested_raw)} · "
                    f"{getattr(merchant, 'state', '') or '-'} · "
                    f"{proceed_count} doc(s)"
                ),
                "why": why,
                "score_value": band or "-",
                "score_label": "band",
                "verdict_kind": verdict_kind,
                "requested": requested_int,
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

    # Funded in last 7 days (from merchant_outcomes)
    _7d_ago = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    funded_rows = [
        r
        for r in (outcomes or [])
        if isinstance(r, dict)
        and r.get("outcome") == "funded"
        and str(r.get("recorded_at") or "") > _7d_ago
    ]

    def _row_amount(r: dict[str, Any]) -> int:
        raw = r.get("funded_amount") or 0
        try:
            return int(raw)
        except (TypeError, ValueError, InvalidOperation):
            return 0

    funded_total = sum(_row_amount(r) for r in funded_rows)
    funded_str = (
        f"${funded_total / 1_000_000:.1f}M"
        if funded_total >= 1_000_000
        else f"${funded_total:,.0f}"
    )
    avg_ticket = int(funded_total / len(funded_rows)) if funded_rows else 0
    avg_str = f"avg ticket ${avg_ticket:,.0f}" if avg_ticket else "no funded deals"

    # Committee = proceed verdict AND requested > 500k
    committee = [
        q for q in queue if q.get("verdict_kind") == "p" and q.get("requested", 0) > 500_000
    ]

    # Pipeline total requested (Decimal-safe int sum)
    total_requested = sum(int(q.get("requested", 0) or 0) for q in queue)
    total_req_str = (
        f"${total_requested / 1_000_000:.1f}M"
        if total_requested >= 1_000_000
        else f"${total_requested:,.0f}"
    )

    kpis = [
        {
            "label": "Active files",
            "value": str(total),
            "detail": f"{len(set(q.get('product', '') for q in queue))} product lines",
            "is_alert": False,
        },
        {
            "label": "Blocked · can't decide",
            "value": str(block_total),
            "detail": "compliance + missing package",
            "is_alert": block_total > 0,
        },
        {
            "label": "Ready to issue",
            "value": str(proceed_total),
            "detail": "approved + matched",
            "is_alert": False,
        },
        {
            "label": "In committee",
            "value": str(len(committee)),
            "detail": "above auto-approve size",
            "is_alert": False,
        },
        {
            "label": "Funded · 7d",
            "value": funded_str,
            "detail": avg_str,
            "is_alert": False,
        },
    ]

    return {
        "kpis": kpis,
        "mix": mix,
        "queue": queue,
        "total_requested": total_req_str,
        "queue_depth": len([q for q in queue if q.get("verdict_kind") not in ("p", "k")]),
        "funded_7d": funded_str,
    }


# ============================================================ DEAL
def build_deal_view(
    merchant: Any,
    analysis: Any = None,
    documents: list[Any] | None = None,
    background_ctx: dict[str, Any] | None = None,
    funder_matches: list[Any] | None = None,
    score_result: Any = None,
    transactions: list[Any] | None = None,
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

    # paper_grade and score-tier live on ``ScoreResult`` (not
    # ``AnalysisRow``). ``run_scoring_pipeline_for_merchant`` in
    # merchants.py surfaces both when the merchant is finalized and
    # scoreable; ``None`` for unscored / unfinalized merchants.
    paper_grade = getattr(score_result, "paper_grade", None) if score_result else None
    score_tier = getattr(score_result, "tier", None) if score_result else None
    recommendation = getattr(score_result, "recommendation", None) if score_result else None

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
                # ``warn`` is the spec-2C severity token. ``_gates.html.j2``
                # normalises legacy ``amber`` to ``warn`` as well so
                # downstream callers cannot regress on this rename.
                "severity": "warn",
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

    # Deal-header inputs (spec 2B). The rebuilt dossier renders a fixed
    # 6-slot fact strip driven by these values; the legacy ``facts``
    # list stays populated for callers that still consume it.
    _entity_raw = getattr(merchant, "entity_type", None)
    entity_display = _entity_raw.upper() if isinstance(_entity_raw, str) else None
    tib_months = getattr(merchant, "time_in_business_months", None)
    fico_val = getattr(merchant, "credit_score", None)
    requested_raw = getattr(merchant, "requested_amount", None)
    mca_balance_raw = getattr(merchant, "stated_mca_balance", None)
    mca_positions_stated = getattr(merchant, "stated_mca_positions", None)

    return {
        "deal": {
            "id": str(getattr(merchant, "id", "") or ""),
            "product": product,
            "name": getattr(merchant, "business_name", None) or "",
            "code": str(getattr(merchant, "id", "") or "")[:8].upper(),
            "subline": _deal_subline(merchant, product_raw),
            "facts": facts,
            # Spec-2B extras. None values pass through so the template
            # can render an em-dash placeholder instead of an empty cell.
            "owner_name": getattr(merchant, "owner_name", None),
            "state": getattr(merchant, "state", None),
            "entity": entity_display,
            "tib": tib_months,
            "fico": fico_val,
            "requested": _fmt_money(requested_raw) if requested_raw is not None else None,
            "mca_balance": _fmt_money(mca_balance_raw) if mca_balance_raw is not None else None,
            "mca_positions": mca_positions_stated,
        },
        "documents": documents or [],
        "verdict": {
            "kind": verdict_kind,
            "rec": rec,
            "tag": band or "",
            "grade": paper_grade,
            "score_tier": score_tier,
            "recommendation": recommendation,
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
        **build_classification_view(analysis, transactions=transactions),
    }


def _empty_deal_view() -> dict[str, Any]:
    return {
        "deal": {
            "id": "",
            "product": "rbf",
            "name": "",
            "code": "",
            "subline": "",
            "facts": [],
            # Spec-2B extras — None so the template's ``or '—'`` fallback
            # renders the em-dash placeholder without raising.
            "owner_name": None,
            "state": None,
            "entity": None,
            "tib": None,
            "fico": None,
            "requested": None,
            "mca_balance": None,
            "mca_positions": None,
        },
        "documents": [],
        "verdict": {
            "kind": "review",
            "rec": "-",
            "tag": "",
            "grade": None,
            "score_tier": None,
            "recommendation": None,
            "lead": "",
            "why": "",
            "actions": [],
        },
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
_MONTH_NAMES: tuple[str, ...] = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _build_rbf_months(analysis: Any) -> list[dict[str, Any]]:
    """Shape ``AnalysisRow.monthly_breakdown`` for the cashflow module.

    Source row shape (see ``parser/aggregate.py::_monthly_breakdown``):
        ``{"month": "YYYY-MM", "deposits": "1234.56",
           "withdrawals": "234.56", "avg_balance": "5678.90",
           "nsf_count": "0"}``  — Decimals stringified for JSONB.

    Template consumes ``{label, gross, adb, bars, nsf, mom}`` where
    ``bars`` is a list of 0-100 bar heights driving the sparkline and
    ``mom`` is the month-over-month deposits delta as a signed integer
    percentage. Money math via ``Decimal`` per CLAUDE.md.
    """
    if analysis is None:
        return []
    raw = list(getattr(analysis, "monthly_breakdown", None) or [])
    if not raw:
        return []

    parsed: list[dict[str, Any]] = []
    for row in raw:
        try:
            deposits_dec = Decimal(str(row.get("deposits", "0") or "0"))
            adb_dec = Decimal(str(row.get("avg_balance", "0") or "0"))
        except (InvalidOperation, TypeError, ValueError):
            deposits_dec = Decimal("0")
            adb_dec = Decimal("0")
        try:
            nsf_int = int(row.get("nsf_count", "0") or "0")
        except (TypeError, ValueError):
            nsf_int = 0
        parsed.append(
            {
                "raw_month": row.get("month", "") or "",
                "deposits": deposits_dec,
                "adb": adb_dec,
                "nsf": nsf_int,
            }
        )

    max_deposits = max((p["deposits"] for p in parsed), default=Decimal("0"))
    out: list[dict[str, Any]] = []
    for idx, p in enumerate(parsed):
        # Sparkline: 5 bars scaled off peak-month deposits so the
        # operator sees the relative-height trend at a glance. All bars
        # in one month share the same height today (deposits are a
        # single scalar per month), but the shape stays open for a
        # future per-week breakdown.
        if max_deposits > 0:
            pct = int((p["deposits"] / max_deposits) * 100)
        else:
            pct = 0
        bars = [pct] * 5

        mom: int | None = None
        if idx > 0:
            prev = parsed[idx - 1]["deposits"]
            if prev > 0:
                delta = ((p["deposits"] - prev) / prev) * Decimal("100")
                mom = int(delta.quantize(Decimal("1")))

        raw_month = p["raw_month"]
        try:
            year_str, month_str = raw_month.split("-", 1)
            year = int(year_str)
            month = int(month_str)
            label = f"{_MONTH_NAMES[month - 1]} {year % 100:02d}"
        except (ValueError, IndexError):
            label = raw_month

        out.append(
            {
                "label": label,
                "gross": _fmt_money(p["deposits"]),
                "adb": _fmt_money(p["adb"]),
                "bars": bars,
                "nsf": p["nsf"],
                "mom": mom,
            }
        )
    return out


def build_risk_module(merchant: Any, analysis: Any, product: str) -> dict[str, Any]:
    """One dict per product; template dispatch selects the right sub-key."""
    revenue_month = getattr(analysis, "monthly_revenue", None) if analysis else None
    num_nsf = getattr(analysis, "num_nsf", None) if analysis else None
    mca_positions = getattr(analysis, "mca_positions", None) if analysis else None
    avg_balance = getattr(analysis, "avg_daily_balance", None) if analysis else None
    days_negative = getattr(analysis, "days_negative", None) if analysis else None
    statement_days = getattr(analysis, "statement_days", None) if analysis else None
    months_analyzed = (int(statement_days) // 30) if statement_days else None

    rbf_months = _build_rbf_months(analysis)

    modules: dict[str, dict[str, Any]] = {
        "rbf": {
            "true_revenue": _fmt_money(revenue_month),
            "stated_revenue": _fmt_money(getattr(merchant, "monthly_revenue", None)),
            "nsf_count": num_nsf,
            "mca_confirmed": mca_positions,
            "avg_daily_balance": _fmt_money(avg_balance),
            "days_negative": days_negative,
            "months_analyzed": months_analyzed,
            # Cashflow-module inputs (spec 2E). Empty list when no
            # analysis is threaded through — the template renders the
            # "upload statements" empty state instead.
            "months": rbf_months,
            # Placeholder for the reclassify-CTA amount suffix. Empty
            # today; wired in a follow-on that surfaces per-category
            # `other`-bucket dollar totals.
            "unclassified_total": "",
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
# Per-category color + operator-facing label. Same taxonomy as
# ``TransactionCategory`` (parser.models). ``other`` is the unclassified
# bucket the coverage-percentage subtracts from 100%.
_BUCKET_COLORS: dict[str, str] = {
    "deposit": "var(--pass)",
    "payroll": "var(--warn)",
    "ach_credit": "#2a7a5c",
    "mca_debit": "var(--violet)",
    "nsf_fee": "var(--kill)",
    "wire_in": "#2a7a5c",
    "wire_out": "#a55a06",
    "transfer": "var(--info)",
    "fee": "#c9c6ba",
    "chargeback": "var(--kill)",
    "refund": "#8a6d3b",
    "other": "#e0ddd0",
}
_BUCKET_HELP: dict[str, str] = {
    "deposit": "Revenue in",
    "payroll": "Payroll out",
    "ach_credit": "ACH credit",
    "mca_debit": "MCA repayment",
    "nsf_fee": "NSF / OD",
    "wire_in": "Wire received",
    "wire_out": "Wire sent",
    "transfer": "Owner / internal",
    "fee": "Bank fees",
    "chargeback": "Chargeback",
    "refund": "Refund",
    "other": "Unclassified",
}


def build_classification_view(
    analysis: Any,
    transactions: list[Any] | None = None,
) -> dict[str, Any]:
    """Classification-coverage panel driven by real transactions.

    Coverage % = share of transactions whose ``category`` != ``other``.
    Buckets = ``sum(|amount|)`` per non-``other`` category, top-N.
    Empty state when no transactions are threaded through.

    ``analysis`` is kept as a parameter to preserve the signature
    ``build_deal_view`` calls with; only its presence is inspected (to
    decide whether the panel should say "no analysis yet" vs "analysis
    present but no transactions loaded").
    """
    if not transactions:
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
                "coverage_say": (
                    "Transactions not loaded in this render - open the dossier "
                    "to see the classification detail."
                ),
                "buckets": [],
                "counterparties": [],
                "sample_txns": [],
                "callout_kind": "i",
                "callout": "",
            }
        }

    total = len(transactions)
    if total == 0:
        return {
            "classification": {
                "coverage_pct": 0,
                "coverage_say": "No transactions on this statement.",
                "buckets": [],
                "counterparties": [],
                "sample_txns": [],
                "callout_kind": "i",
                "callout": "",
            }
        }

    bucket_totals: dict[str, float] = {}
    classified = 0
    for t in transactions:
        cat = str(getattr(t, "category", "other") or "other")
        try:
            amt = abs(float(getattr(t, "amount", 0) or 0))
        except (TypeError, ValueError):
            amt = 0.0
        bucket_totals[cat] = bucket_totals.get(cat, 0.0) + amt
        if cat != "other":
            classified += 1

    coverage_pct = round(classified / total * 100)
    max_amount = max(bucket_totals.values()) if bucket_totals else 1.0
    buckets = [
        {
            "name": cat.replace("_", " ").title(),
            "pct": round(total_amt / max_amount * 100) if max_amount else 0,
            "amount": _fmt_money(total_amt),
            "color": _BUCKET_COLORS.get(cat, "#e0ddd0"),
            "help": _BUCKET_HELP.get(cat, cat),
        }
        for cat, total_amt in sorted(bucket_totals.items(), key=lambda x: -x[1])
        if cat != "other"
    ][:8]

    if coverage_pct < 60:
        callout_kind = "w"
        callout = (
            "<b>Low classification coverage.</b> Many transactions couldn't "
            "be categorized — revenue figures may be understated. Try "
            "reclassifying."
        )
    elif coverage_pct >= 90:
        callout_kind = "p"
        callout = "<b>Strong classification.</b> Revenue figures are reliable."
    else:
        callout_kind = "i"
        callout = ""

    return {
        "classification": {
            "coverage_pct": coverage_pct,
            "coverage_say": f"{coverage_pct}% of transactions classified ({classified}/{total})",
            "buckets": buckets,
            "counterparties": [],
            "sample_txns": [],
            "callout_kind": callout_kind,
            "callout": callout,
        }
    }


# ============================================================ FUNDERS
# Product-line labels for the funder card's ``product_line`` display
# string. Same tokens as ``FunderRow.deal_types_accepted`` mapped to a
# short human-facing label. Unknown tokens display verbatim.
_DEAL_TYPE_LABELS: dict[str, str] = {
    "mca": "MCA",
    "revenue_based": "MCA",
    "term_loan": "Term",
    "term": "Term",
    "business_loan": "Term",
    "loc": "LOC",
    "line_of_credit": "LOC",
    "equipment": "Equipment",
    "equipment_financing": "Equipment",
    "factoring": "Factoring",
    "receivables": "Factoring",
    "asset_based": "ABL",
    "abl": "ABL",
    "sba": "SBA",
}


def _fmt_money_short(val: Any) -> str:
    """Render ``$25K`` / ``$1.5M`` style for compact card fields.

    Money math stays on ``Decimal``; the final label is a string. ``None``
    or blank returns em-dash.
    """
    if val is None or val == "":
        return "—"
    try:
        d = Decimal(str(val))
    except (InvalidOperation, TypeError, ValueError):
        return "—"
    if d <= 0:
        return "$0"
    if d >= Decimal("1000000"):
        rendered = f"${d / Decimal('1000000'):.1f}M"
        return rendered.replace(".0M", "M")
    if d >= Decimal("1000"):
        return f"${d / Decimal('1000'):.0f}K"
    return f"${d:,.0f}"


def _build_excludes_summary(
    excluded_industries: tuple[str, ...],
    excluded_states: tuple[str, ...],
) -> str:
    """Compact excludes label for the card. em-dash when both empty."""
    industries = list(excluded_industries)[:3]
    states = list(excluded_states)[:5]
    if not industries and not states:
        return "—"
    pieces: list[str] = []
    if industries:
        pieces.append(", ".join(i.replace("_", " ") for i in industries))
    if states:
        pieces.append(", ".join(states))
    return " · ".join(pieces)


def _resolve_channel(funder: Any) -> str:
    """Return ``"direct"`` or ``"marketplace"``.

    Schema gap: ``FunderRow`` has no ``channel`` field yet. We surface
    any ``channel`` attribute if downstream code has added one; otherwise
    default to ``"direct"`` (Commera integrates with direct lenders as
    the standard case; marketplace is an exception). Reported as a
    schema gap so the column can be added later.
    """
    raw = getattr(funder, "channel", None)
    if raw in ("direct", "marketplace"):
        return str(raw)
    return "direct"


def _resolve_product_line(deal_types: tuple[str, ...]) -> str:
    """Compact product-line label from ``deal_types_accepted``.

    Empty tuple -> ``"MCA"`` (matcher default). Multiple types -> joined
    with `` / ``.
    """
    if not deal_types:
        return "MCA"
    labels: list[str] = []
    for t in deal_types[:3]:
        labels.append(_DEAL_TYPE_LABELS.get(t, t.replace("_", " ").title()))
    return " / ".join(labels)


def _last_submission_summary(
    funder_id: str,
    funder_note_subs: Any,
) -> tuple[str | None, str | None]:
    """Look up the most recent Close-Note submission for this funder.

    Returns ``(result_label, date_str)`` -- both may be ``None`` when the
    repository has no rows or the lookup fails. ``funder_note_subs`` is
    optional; the HTMX partial re-render path passes ``None`` and both
    values come back ``None``.
    """
    if funder_note_subs is None or not funder_id:
        return (None, None)
    try:
        rows = funder_note_subs.list_for_funder(UUID(funder_id), limit=1) or []
    except Exception:
        return (None, None)
    if not rows:
        return (None, None)
    latest = rows[0]
    status = getattr(latest, "status", None)
    submitted = getattr(latest, "submitted_at", None)
    result_label: str | None = None
    if status:
        result_label = {
            "approved": "Approved",
            "declined": "Declined",
            "pending": "Pending",
        }.get(str(status), str(status).title())
    date_str: str | None = None
    if isinstance(submitted, datetime):
        date_str = submitted.strftime("%Y-%m-%d")
    return (result_label, date_str)


def build_funders_view(
    data: Any,
    q: str = "",
    filt: str = "all",
    *,
    funder_note_subs: Any = None,
) -> dict[str, Any]:
    """List page shaper. ``data`` is a list of ``FunderRow`` (or ``None``).

    Filter tokens:
      * ``all``          - no filter
      * ``direct``       - ``channel == "direct"``
      * ``marketplace``  - ``channel == "marketplace"``
      * ``active`` / ``paused`` / ``first_position_only`` / ``selective``
                         - operator_status tokens kept for legacy callers

    Search is a case-insensitive substring against ``name``.

    Card fields per funder:
        ``id, name, channel, status, product_line, remittance_type,
        min_revenue, min_fico, min_tib_months, max_positions,
        stacking_ok, excludes_summary,
        last_submission_result, last_submission_date``

    ``funder_note_subs`` is the optional repository used to look up
    ``last_submission_*``. The router passes it on the full-page render;
    HTMX partial re-renders may omit it.
    """
    if not data:
        return {"funders": [], "q": q, "filter": filt}

    q_lower = (q or "").strip().lower()
    funders_out: list[dict[str, Any]] = []
    for f in data:
        name = getattr(f, "name", "") or ""
        status = getattr(f, "operator_status", "active") or "active"
        deal_types = tuple(getattr(f, "deal_types_accepted", ()) or ())
        tiers = getattr(f, "tiers", ()) or ()

        if q_lower and q_lower not in name.lower():
            continue

        channel = _resolve_channel(f)
        # Channel filter is orthogonal to operator_status filter.
        if filt == "direct" and channel != "direct":
            continue
        if filt == "marketplace" and channel != "marketplace":
            continue
        if filt in ("active", "paused", "first_position_only", "selective") and status != filt:
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
        min_tib = (
            getattr(best_tier, "min_months_in_business", None)
            if best_tier
            else getattr(f, "min_months_in_business", None)
        )

        fid = str(getattr(f, "id", "") or "")
        last_result, last_date = _last_submission_summary(fid, funder_note_subs)

        funders_out.append(
            {
                "id": fid,
                "name": name,
                # Schema gap: no ``channel`` column on FunderRow yet.
                "channel": channel,
                "status": status,
                "product_line": _resolve_product_line(deal_types),
                # Schema gap: no ``remittance_type`` column yet.
                "remittance_type": getattr(f, "remittance_type", None) or "straight",
                "min_revenue": _fmt_money_short(min_rev),
                "min_fico": min_fico,
                "min_tib_months": min_tib,
                "max_positions": max_pos,
                "stacking_ok": bool(getattr(f, "accepts_stacking", False)),
                "excludes_summary": _build_excludes_summary(
                    tuple(getattr(f, "excluded_industries", ()) or ()),
                    tuple(getattr(f, "excluded_states", ()) or ()),
                ),
                # Legacy field kept for callers that still read it -
                # not consumed by the v2 template.
                "deal_types": list(deal_types)[:4],
                "last_submission_result": last_result,
                "last_submission_date": last_date,
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
# ``ok`` / ``warn`` / ``miss`` map to the ``.st-*`` CSS classes in
# aegis-design.css (``st-clear``, ``st-review``, ``st-match``). We use
# the shorter tokens directly on the template's ``st-{status}`` string
# after mapping in ``_kyb_status`` below.
def _kyb_status(passed: bool | None) -> tuple[str, str]:
    """Bool tri-state -> (status_class, label).

    ``True``  -> ``ok``   / ``"OK"``
    ``False`` -> ``miss`` / ``"Missing"``
    ``None``  -> ``warn`` / ``"Pending"`` (never-checked path)
    """
    if passed is True:
        return ("ok", "OK")
    if passed is False:
        return ("miss", "Missing")
    return ("warn", "Pending")


def _relative_time(when: datetime | None, now: datetime | None = None) -> str:
    """Compact ``"3h ago"`` / ``"2d ago"`` for the activity log.

    ``None`` returns em-dash. Now-relative rendering uses UTC.
    """
    if when is None:
        return "—"
    now = now or datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    delta = now - when
    if delta < timedelta(minutes=1):
        return "just now"
    if delta < timedelta(hours=1):
        mins = int(delta.total_seconds() // 60)
        return f"{mins}m ago"
    if delta < timedelta(days=1):
        hrs = int(delta.total_seconds() // 3600)
        return f"{hrs}h ago"
    if delta < timedelta(days=30):
        return f"{delta.days}d ago"
    return when.strftime("%Y-%m-%d")


def _activity_kind_for_action(action: str) -> tuple[str, str]:
    """Map an audit action -> (kind, label) for the activity-log badge.

    Kind is one of ``ok`` / ``warn`` / ``kill`` / ``info``; label is the
    short badge string. Kept centralized because prefix alone is
    ambiguous (``kyb.clear`` is ok, ``kyb.miss`` is warn).
    """
    a = (action or "").lower()
    if "ofac" in a and any(tok in a for tok in ("hit", "match", "block")):
        return ("kill", "OFAC hit")
    if "hard_decline" in a or ".block" in a:
        return ("kill", "Block")
    if any(tok in a for tok in ("deadline_approaching", "pending", "reminder", "escalat")):
        return ("warn", "Attention")
    if any(tok in a for tok in ("clear", "confirm", "verified", "resolved", "approved")):
        return ("ok", "Cleared")
    return ("info", "Event")


def _activity_description(row: dict[str, Any]) -> str:
    """Render one audit row -> short human-facing description string.

    Prefers ``details.summary`` if present; falls back to the action
    with subject substitutions.
    """
    details = row.get("details") or {}
    if isinstance(details, dict):
        summary = details.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary.strip()
    action = row.get("action") or "event"
    subject_type = row.get("subject_type") or ""
    subject_id = row.get("subject_id") or ""
    if subject_type and subject_id:
        return f"{action} · {subject_type} {str(subject_id)[:8]}"
    return str(action)


def _load_state_matrix_rows() -> list[dict[str, Any]]:
    """Read ``docs/compliance/states.yaml`` -> list of state cards.

    Sorted by risk (kill first, then warn, then ok) then name. Each card
    gets a short ``description`` derived from tier + overlays. Never
    fabricates statute text -- only surfaces facts structurally present
    in the yaml. Empty list on any load / validation failure.
    """
    try:
        from aegis.compliance.state_matrix import Tier1Regulation, load_matrix
    except Exception:
        return []
    try:
        matrix = load_matrix()
    except Exception:
        return []

    warn_risk = {"medium"}
    kill_risk = {"high"}

    rows: list[dict[str, Any]] = []
    for code, entry in matrix.states.items():
        tier = getattr(entry, "tier", 3)
        if tier == 1 and isinstance(entry, Tier1Regulation):
            risk_source = getattr(entry.overlays, "ag_enforcement_risk", "low")
            statute = ", ".join(entry.cfdl.statute[:1]) if entry.cfdl.statute else "CFDL"
            coj = getattr(entry.overlays, "coj", "permitted")
            broker_fee = getattr(entry.overlays, "broker_advance_fee", "permitted")
            desc_parts = [f"CFDL: {statute}"]
            if coj != "permitted":
                desc_parts.append(f"CoJ {coj}")
            if broker_fee == "prohibited":
                desc_parts.append("broker advance-fee prohibited")
            description = " · ".join(desc_parts)
        elif tier == 2:
            risk_source = getattr(entry, "likelihood", "low")
            bills = list(getattr(entry, "pending_bills", []))[:2]
            description = f"Watch: {', '.join(bills)}" if bills else "Watch list"
        else:  # tier 3
            risk_source = getattr(entry, "ag_enforcement_risk", "low")
            description = "No MCA-specific law · defensive posture"

        if risk_source in kill_risk:
            risk = "kill"
        elif risk_source in warn_risk:
            risk = "warn"
        else:
            risk = "ok"

        rows.append(
            {
                "name": getattr(entry, "name", code),
                "code": code,
                "risk": risk,
                "description": description,
                "tier": tier,
            }
        )

    risk_order = {"kill": 0, "warn": 1, "ok": 2}
    rows.sort(key=lambda r: (risk_order.get(r["risk"], 3), r["name"]))
    return rows


def _build_kyb_rows(merchants: list[Any] | None) -> list[dict[str, Any]]:
    """One row per merchant with a real KYB signal on the record.

    Filters to merchants where any KYB-adjacent field has been touched -
    no reason to render an all-``pending`` row for a merchant that has
    not been screened at all.

    Columns: entity (SOS), beneficial ownership, ID verification,
    TIN / EIN match, bank verification. Per-column status derives from
    real ``MerchantRow`` fields; where no source field exists
    (beneficial ownership, TIN cross-check), status is ``warn``
    (schema gap - those columns are on the KYB backlog).
    """
    if not merchants:
        return []

    rows: list[dict[str, Any]] = []
    for m in merchants:
        touched = any(
            getattr(m, attr, None) is not None
            for attr in ("sos_checked_at", "ucc_checked_at", "ofac_checked_at")
        )
        if not touched:
            continue

        sos_status, sos_label = _kyb_status(getattr(m, "sos_is_active", None))

        # Schema gap: no ``beneficial_ownership_confirmed`` column yet.
        bo_status, bo_label = ("warn", "Pending")

        dl_ok = bool(getattr(m, "drivers_license_on_file", False))
        id_status, id_label = _kyb_status(True) if dl_ok else _kyb_status(False)

        # Schema gap: no IRS cross-check field; ``ein`` presence is the
        # closest signal (on-file vs missing).
        ein = getattr(m, "ein", None)
        if ein:
            tin_status, tin_label = ("ok", "On file")
        else:
            tin_status, tin_label = ("miss", "Missing")

        bank_ok = bool(getattr(m, "voided_check_on_file", False))
        bank_status, bank_label = _kyb_status(True) if bank_ok else _kyb_status(False)

        rows.append(
            {
                "name": getattr(m, "business_name", None) or "(unknown)",
                "sos_status": sos_status,
                "sos_label": sos_label,
                "bo_status": bo_status,
                "bo_label": bo_label,
                "id_status": id_status,
                "id_label": id_label,
                "tin_status": tin_status,
                "tin_label": tin_label,
                "bank_status": bank_status,
                "bank_label": bank_label,
            }
        )

    return rows[:50]


def _build_compliance_kpis(
    ofac_items: list[dict[str, Any]],
    kyb_rows: list[dict[str, Any]],
    activity_rows: list[dict[str, Any]],
    state_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Five KPIs matching the mockup, computed from real signals.

    Never invents; each KPI degrades to em-dash on missing source data.
    """
    ofac_open = len(ofac_items)

    kyb_incomplete = sum(
        1
        for r in kyb_rows
        if any(
            r.get(c) in ("miss", "warn")
            for c in ("sos_status", "bo_status", "id_status", "tin_status", "bank_status")
        )
    )

    state_flags = sum(1 for r in state_rows if r.get("risk") == "kill")

    cleared_7d = sum(1 for a in activity_rows if a.get("kind") == "ok")

    # Avg clear time - cannot compute without the paired
    # (opened_at, cleared_at) rows the audit log does not currently
    # pair. Degrade to em-dash so the panel is honest.
    avg_clear_time = "—"

    return [
        {
            "label": "OFAC open",
            "value": str(ofac_open),
            "detail": "pending review",
            "is_alert": ofac_open > 0,
        },
        {
            "label": "KYB incomplete",
            "value": str(kyb_incomplete) if kyb_rows else "—",
            "detail": "missing signals",
            "is_alert": kyb_incomplete > 0,
        },
        {
            "label": "State-license flags",
            "value": str(state_flags) if state_rows else "—",
            "detail": "high AG risk",
            "is_alert": state_flags > 0,
        },
        {
            "label": "Cleared · 7d",
            "value": str(cleared_7d) if activity_rows else "—",
            "detail": "recent activity",
            "is_alert": False,
        },
        {
            "label": "Avg clear time",
            "value": avg_clear_time,
            "detail": "median lookback",
            "is_alert": False,
        },
    ]


def build_compliance_view(
    ofac_rows: Any,
    *,
    all_merchants: list[Any] | None = None,
    audit_log: Any = None,
) -> dict[str, Any]:
    """Compliance workbench view model.

    ``ofac_rows``      - list of ``MerchantRow`` (or dicts) with the OFAC
                          match flag set. Same shape the router passes.
    ``all_merchants``  - full merchant list used to build the KYB table.
                          Omit -> KYB section renders empty.
    ``audit_log``      - ``AuditLog`` protocol implementation used to
                          read recent compliance-family activity.
                          Omit -> activity section renders empty.

    Returns ``{"kpis", "ofac", "kyb", "states", "activity"}`` - the
    router forwards this to the template context verbatim.
    """
    # -- OFAC queue (existing shape preserved) --
    ofac_items: list[dict[str, Any]] = []
    if ofac_rows:
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

    kyb_rows = _build_kyb_rows(all_merchants)
    state_rows = _load_state_matrix_rows()

    activity_rows: list[dict[str, Any]] = []
    if audit_log is not None:
        try:
            raw = audit_log.list_recent(limit=200) or []
        except Exception:
            raw = []
        for r in raw:
            action = (r.get("action") or "").lower()
            if not any(action.startswith(p) for p in ("compliance.", "ofac.", "kyb.")):
                continue
            created_at = r.get("created_at")
            when: datetime | None = None
            if isinstance(created_at, datetime):
                when = created_at
            elif isinstance(created_at, str):
                try:
                    when = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                except ValueError:
                    when = None
            kind, label = _activity_kind_for_action(action)
            activity_rows.append(
                {
                    "date": _relative_time(when),
                    "description": _activity_description(r),
                    "kind": kind,
                    "label": label,
                }
            )
            if len(activity_rows) >= 20:
                break

    kpis = _build_compliance_kpis(ofac_items, kyb_rows, activity_rows, state_rows)

    return {
        "kpis": kpis,
        "ofac": ofac_items,
        "kyb": kyb_rows,
        "states": state_rows,
        "activity": activity_rows,
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
