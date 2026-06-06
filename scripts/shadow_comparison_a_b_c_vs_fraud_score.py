"""Step 2a — SHADOW COMPARISON of the new A/B/C tracks vs the live
``fraud_score`` decision path.

READ-ONLY. Zero writes. Zero database mutations. Does not touch the live
decline path, does not modify ``fraud_score``, does not change scoring.
For each merchant in the corpus, compares:

1. LIVE: what does the existing ``score_deal`` pipeline decide today?
        (tier ∈ {A,B,C,D,F}, recommendation ∈ {approve,refer,decline},
        hard-decline reasons, soft concerns)
2. NEW: what would the new A/B/C tracks indicate alongside?
        (Track A integrity verdict, Track B risk band+action,
        Track C concentration framing — informational only)
3. CATEGORIZES each disagreement honestly:
       * **agreement**                    — both flag, or both clean
       * **new-is-better**                — live flagged fraud where the
                                            new view correctly reads
                                            business-model / concentration
                                            (the VU case)
       * **old-caught-something-new-misses**
                                          — live declined / flagged
                                            something the new tracks
                                            would let through.  POTENTIAL
                                            REGRESSION — flagged loudly
       * **genuinely-ambiguous**          — differing but neither
                                            obviously better.  Needs an
                                            operator judgment call.

This script lives at ``scripts/`` (flat) — read-only prod diagnostics
sit here alongside ``check_vu7722_status.py`` /
``vu_cross_account_analysis.py``. ``scripts/audit/`` is reserved for
prod-WRITE / side-effect / external-API-cost scripts (seeds, reparses,
captures, Bedrock probes).

Usage on the box, with ``/etc/aegis/aegis.env`` sourced::

    set -a; source /etc/aegis/aegis.env; set +a
    cd /opt/aegis
    .venv/bin/python scripts/shadow_comparison_a_b_c_vs_fraud_score.py
"""

from __future__ import annotations

import sys
import traceback
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import SupabaseMerchantRepository
from aegis.parser.patterns import analyze_patterns, pattern_analysis_from_dto
from aegis.scoring.models import ScoreResult
from aegis.scoring.multi_month import score_input_multi_month
from aegis.scoring.score import score_deal
from aegis.scoring_v2.dossier_panel import UnifiedTracksView, build_unified_tracks_view
from aegis.storage import AnalysisRow, DocumentRow, SupabaseDocumentRepository

# ─────────────────────────────────────────────────────────────────────
# Disagreement categories — explicit, reviewable
# ─────────────────────────────────────────────────────────────────────

CAT_AGREEMENT = "agreement"
CAT_NEW_BETTER = "new-is-better"
CAT_OLD_BETTER = "old-caught-something-new-misses"  # REGRESSION
CAT_AMBIGUOUS = "genuinely-ambiguous"
CAT_INSUFFICIENT = "insufficient-new-data"  # B/C cannot compute; A alone


@dataclass
class MerchantComparison:
    merchant_id: UUID
    business_name: str
    is_finalized: bool
    doc_count: int
    analyzed_doc_count: int
    docs_with_transactions: int
    # LIVE
    live_score: int | None = None
    live_tier: str | None = None
    live_recommendation: str | None = None
    live_hard_reasons: list[str] = field(default_factory=list)
    live_soft_concerns: list[str] = field(default_factory=list)
    live_error: str | None = None
    # NEW (Track A + B + C)
    new_integrity: str | None = None  # 'fail' | 'review' | 'clean' | None
    new_integrity_branches: list[str] = field(default_factory=list)
    new_band: str | None = None  # 'high' | 'elevated' | 'moderate' | 'low' | None
    new_band_action: str | None = None
    new_band_factors: list[str] = field(default_factory=list)
    new_intl_share_pct: float | None = None
    new_revenue_basis: Decimal | None = None
    new_track_b_top_severity: str | None = None
    new_unified_insufficient_reason: str | None = None
    new_error: str | None = None
    # Categorization
    category: str = CAT_AGREEMENT
    rationale: str = ""


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _live_decision(
    merchant: MerchantRow,
    items: list[tuple[DocumentRow, AnalysisRow]],
    latest_doc: DocumentRow | None,
    latest_analysis: AnalysisRow | None,
    latest_transactions: list[Any],
) -> tuple[ScoreResult | None, str | None]:
    """Replicate the dossier handler's live-scoring path.

    Returns ``(score_result, error_str)``. If the merchant is not
    finalized OR has no analyzed items, returns ``(None, reason)``.
    """
    if not getattr(merchant, "is_finalized", False):
        return None, "merchant not finalized"
    if not items:
        return None, "no analyzed documents"

    # Pattern analysis — prefer the AnalysisRow cache; fall back to live
    # recompute the same way the dossier handler does.
    pattern_analysis = None
    if latest_analysis is not None:
        cached = getattr(latest_analysis, "pattern_analysis", None)
        if cached is not None:
            try:
                pattern_analysis = pattern_analysis_from_dto(cached)
            except Exception:
                pattern_analysis = None
        if pattern_analysis is None and latest_transactions:
            try:
                pattern_analysis = analyze_patterns(
                    latest_transactions,
                    latest_analysis.statement_period_start,
                    latest_analysis.statement_period_end,
                )
            except Exception:
                pattern_analysis = None

    try:
        score_input = score_input_multi_month(
            merchant, items, pattern_analysis=pattern_analysis
        )
    except Exception as exc:
        return None, f"score_input_multi_month failed: {exc!r}"
    try:
        # Run WITHOUT OFAC — shadow comparison is about the
        # fraud_score/track-B/track-C overlap, not OFAC sanctions
        # screening. OFAC=None also avoids writing an ofac_screening
        # audit row as a side effect of this read-only sweep.
        result = score_deal(score_input, ofac=None)
    except Exception as exc:
        return None, f"score_deal failed: {exc!r}"
    return result, None


def _build_new_view(
    docs: SupabaseDocumentRepository,
    documents: list[DocumentRow],
    analyses_by_doc: dict[UUID, AnalysisRow],
) -> tuple[UnifiedTracksView | None, str | None]:
    """Build the unified A+B+C view from the same documents."""
    try:
        view = build_unified_tracks_view(
            documents=documents,
            list_transactions=docs.list_transactions,
            analyses_by_doc=analyses_by_doc,
        )
        return view, None
    except Exception as exc:
        return None, f"build_unified_tracks_view failed: {exc!r}"


def _categorise(row: MerchantComparison) -> None:
    """Categorise the disagreement between live and new for ONE merchant.

    Honest assignment — never silently masks regressions. The four
    headline buckets are documented at the top of the file. Edge cases:

      * If LIVE could not run (merchant not finalized, no items), the
        category is ``insufficient-new-data`` UNLESS new shows fail/elevated
        — then the new view is at least informational (still not actionable).
      * If NEW had insufficient data (no transactions persisted), category is
        ``insufficient-new-data`` unless Track A produced a FAIL verdict.
    """
    live = row.live_recommendation
    a = row.new_integrity
    b = row.new_band
    hard = set(row.live_hard_reasons)

    # ── Both surfaces unavailable
    if live is None and a is None and b is None:
        row.category = CAT_INSUFFICIENT
        row.rationale = "no live scoring + no new signals"
        return

    # ── Live unavailable, but new produced something
    if live is None:
        # Branch 2a split (operator decision 2026-06-05): credit
        # ``new-is-better`` only when live's silence is due to the
        # manual_review path producing no analysis row (the A&R KM
        # shape — live can't score because the parser flagged the
        # statement for human review). When live's silence is due to
        # NON-finalization (state missing, merchant not finalized), an
        # A=fail finding is genuinely-ambiguous, not a credited win —
        # live didn't run because the merchant isn't ready, not because
        # it missed something.
        live_err = (row.live_error or "").lower()
        unscored_due_to_no_analysis = "no analyzed documents" in live_err
        unscored_due_to_not_finalized = "not finalized" in live_err
        if a == "fail":
            if unscored_due_to_no_analysis:
                row.category = CAT_NEW_BETTER
                row.rationale = (
                    "live did not score — manual_review path produced no "
                    "analysis row; new Track A reports FAIL integrity, "
                    "surfacing what live missed (A&R KM shape)"
                )
            elif unscored_due_to_not_finalized:
                row.category = CAT_AMBIGUOUS
                row.rationale = (
                    "live did not score — merchant not finalized; new Track A "
                    "reports FAIL but live's silence is finalization gating, "
                    "not a missed catch"
                )
            else:
                row.category = CAT_AMBIGUOUS
                row.rationale = (
                    f"live did not score ({row.live_error}); new Track A "
                    "reports FAIL — operator review needed to attribute"
                )
            return
        if a == "review":
            row.category = CAT_AMBIGUOUS
            row.rationale = (
                "live did not score; new Track A reports REVIEW — soft signal "
                "live's score path wouldn't have surfaced"
            )
            return
        row.category = CAT_INSUFFICIENT
        row.rationale = (
            f"live did not score ({row.live_error or 'unscored'}); "
            "new tracks show nothing actionable either"
        )
        return

    # ── Live scored — compare
    new_flags = (a in ("fail", "review")) or (b in ("high", "elevated"))
    fraud_decline = any(
        r.startswith("fraud_score_critical") or r.startswith("incremental_pdf_saves")
        or r == "bank_statement_tampering_confirmed"
        or r == "validation_failed_manual_review_required"
        for r in hard
    )
    intl_concentration_factor = (
        "international_concentration" in row.new_band_factors
        or (row.new_intl_share_pct is not None and row.new_intl_share_pct >= 50.0)
    )

    if live == "decline":
        if a == "fail":
            row.category = CAT_AGREEMENT
            row.rationale = "live decline AND new Track A FAIL — both catch it"
            return
        if (
            fraud_decline
            and a == "clean"
            and b in ("low", "moderate")
            and intl_concentration_factor
        ):
            row.category = CAT_NEW_BETTER
            row.rationale = (
                "live declined on fraud_score (driven by intl wires) but new "
                "Track A is CLEAN and Track C reframes intl as a durability "
                "question — VU-shape: business-model misread, not fraud"
            )
            return
        if fraud_decline and a == "clean" and not intl_concentration_factor:
            row.category = CAT_AMBIGUOUS
            row.rationale = (
                "live declined on fraud_score but new Track A is CLEAN — "
                "no obvious VU-style reframe; operator review needed"
            )
            return
        if a == "clean" and b in ("low", "moderate") and not new_flags:
            row.category = CAT_OLD_BETTER
            row.rationale = (
                "live declined but new tracks are all clean/low — POTENTIAL "
                "REGRESSION; verify what hard_decline_reason fired"
            )
            return
        if a == "review" or b in ("elevated", "high"):
            row.category = CAT_AGREEMENT
            row.rationale = (
                f"live decline + new partial signal (A={a}, B={b}) — both flag, "
                "different severity"
            )
            return

    if live == "refer":
        if a in ("fail", "review") or b in ("high", "elevated"):
            row.category = CAT_AGREEMENT
            row.rationale = (
                f"live refer + new flagging (A={a}, B={b}) — both want second look"
            )
            return
        if a == "clean" and b in ("low", "moderate"):
            row.category = CAT_OLD_BETTER
            row.rationale = (
                "live refer but new tracks all clean — soft signal live caught "
                "that new tracks don't surface"
            )
            return

    if live == "approve":
        if a == "fail":
            row.category = CAT_NEW_BETTER
            row.rationale = "live approve BUT new Track A FAIL — integrity catch live missed"
            return
        if b == "high":
            row.category = CAT_NEW_BETTER
            row.rationale = "live approve BUT new Track B HIGH risk — risk-band catch live missed"
            return
        if a == "review" or b == "elevated":
            row.category = CAT_AMBIGUOUS
            row.rationale = (
                f"live approve + new A={a}/B={b} — softer-than-live framing, "
                "operator judgment whether to escalate"
            )
            return
        row.category = CAT_AGREEMENT
        row.rationale = "live approve + new tracks clean/low — both green-light"
        return

    row.category = CAT_AMBIGUOUS
    row.rationale = (
        f"live={live} new A={a} B={b} — no rule covers this combination; "
        "manual review"
    )


def _process_one(
    merchant: MerchantRow,
    docs: SupabaseDocumentRepository,
) -> MerchantComparison:
    row = MerchantComparison(
        merchant_id=merchant.id,
        business_name=merchant.business_name,
        is_finalized=getattr(merchant, "is_finalized", False),
        doc_count=0,
        analyzed_doc_count=0,
        docs_with_transactions=0,
    )

    # ── Load documents + analyses
    documents = docs.list_documents(merchant_id=merchant.id, limit=400)
    row.doc_count = len(documents)
    analyses_by_doc: dict[UUID, AnalysisRow] = {}
    items: list[tuple[DocumentRow, AnalysisRow]] = []
    docs_with_txns = 0
    for d in documents:
        a = docs.get_analysis(d.id)
        if a is None:
            continue
        analyses_by_doc[d.id] = a
        items.append((d, a))
        try:
            txns = docs.list_transactions(d.id)
        except Exception:
            txns = []
        if txns:
            docs_with_txns += 1
    items.sort(key=lambda pair: pair[1].statement_period_end, reverse=True)
    row.analyzed_doc_count = len(items)
    row.docs_with_transactions = docs_with_txns

    latest_doc = items[0][0] if items else None
    latest_analysis = items[0][1] if items else None
    latest_transactions: list[Any] = []
    if latest_doc is not None:
        try:
            latest_transactions = docs.list_transactions(latest_doc.id)
        except Exception:
            latest_transactions = []

    # ── LIVE
    result, err = _live_decision(
        merchant, items, latest_doc, latest_analysis, latest_transactions
    )
    if result is not None:
        row.live_score = result.score
        row.live_tier = result.tier
        row.live_recommendation = result.recommendation
        row.live_hard_reasons = list(result.hard_decline_reasons)
        row.live_soft_concerns = list(result.soft_concerns)
    else:
        row.live_error = err

    # ── NEW (always; even if live couldn't run, new view is still useful)
    view, view_err = _build_new_view(docs, documents, analyses_by_doc)
    if view is not None:
        row.new_integrity = view.integrity_worst_verdict
        row.new_integrity_branches = [v.branch for v in view.integrity_verdicts]
        row.new_unified_insufficient_reason = view.insufficient_data_reason or None
        if view.risk_band is not None:
            row.new_band = view.risk_band.band
            row.new_band_action = view.risk_band.action
            row.new_band_factors = [r.factor for r in view.risk_band.reasons]
            severities = [r.severity for r in view.risk_band.reasons]
            for sev in ("critical", "elevated", "concern", "neutral", "ok"):
                if sev in severities:
                    row.new_track_b_top_severity = sev
                    break
            row.new_intl_share_pct = (
                float(view.risk_band.cashflow.international_client_share_pct)
                if view.risk_band.cashflow.international_client_share_pct is not None
                else None
            )
        if view.context_panel is not None:
            row.new_revenue_basis = view.context_panel.revenue_basis
    else:
        row.new_error = view_err

    _categorise(row)
    return row


# ─────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────


def _fmt_list(items: Iterable[str], max_items: int = 3) -> str:
    items = list(items)
    if not items:
        return "—"
    if len(items) <= max_items:
        return ", ".join(items)
    return ", ".join(items[:max_items]) + f" (+{len(items) - max_items} more)"


def _print_summary(rows: list[MerchantComparison]) -> None:
    print()
    print("=" * 88)
    print("SHADOW COMPARISON · LIVE fraud_score path  vs  new A/B/C tracks")
    print("=" * 88)
    print()
    print(f"  total merchants scanned:              {len(rows)}")
    rows_finalized = [r for r in rows if r.is_finalized]
    rows_with_txns = [r for r in rows if r.docs_with_transactions > 0]
    rows_with_docs = [r for r in rows if r.doc_count > 0]
    print(f"  merchants with ≥1 document:           {len(rows_with_docs)}")
    print(f"  merchants finalized (scoring-eligible): {len(rows_finalized)}")
    print(f"  merchants with classified transactions: {len(rows_with_txns)}")
    print()
    by_cat: dict[str, int] = {}
    for r in rows:
        by_cat[r.category] = by_cat.get(r.category, 0) + 1
    print("  By disagreement category:")
    for cat in (CAT_AGREEMENT, CAT_NEW_BETTER, CAT_OLD_BETTER, CAT_AMBIGUOUS, CAT_INSUFFICIENT):
        n = by_cat.get(cat, 0)
        marker = " ← REGRESSION RISK" if cat == CAT_OLD_BETTER and n > 0 else ""
        print(f"    {cat:42s}  {n}{marker}")
    print()
    print("  CONFIDENCE: the corpus is small (history was wiped/quarantined).")
    print("  A comparison on this N is INDICATIVE, not conclusive. Treat")
    print("  every disagreement below as a case study, not a population statistic.")


def _print_table(rows: list[MerchantComparison]) -> None:
    print()
    print("─" * 88)
    print("PER-MERCHANT COMPARISON TABLE")
    print("─" * 88)
    header = (
        "  "
        + "Merchant".ljust(30)
        + " | "
        + "Live".ljust(18)
        + " | "
        + "New A/B/C".ljust(22)
        + " | "
        + "Category"
    )
    print(header)
    print("  " + "─" * (len(header) - 2))
    for r in rows:
        live = (
            f"{r.live_recommendation or '—'}/{r.live_tier or '—'}"
            if r.live_recommendation
            else (r.live_error or "—")[:18]
        )
        new = (
            f"A={r.new_integrity or '—'}"
            f" B={r.new_band or '—'}"
            + (f" intl={r.new_intl_share_pct:.0f}%" if r.new_intl_share_pct else "")
        )
        cat_marker = " ←!" if r.category == CAT_OLD_BETTER else ""
        print(
            "  "
            + (r.business_name[:30]).ljust(30)
            + " | "
            + live[:18].ljust(18)
            + " | "
            + new[:22].ljust(22)
            + " | "
            + r.category
            + cat_marker
        )


def _print_per_merchant_detail(rows: list[MerchantComparison]) -> None:
    print()
    print("─" * 88)
    print("PER-MERCHANT DETAIL · disagreement breakdown")
    print("─" * 88)
    for r in rows:
        print()
        print(f"### {r.business_name}  ({r.merchant_id})")
        print(
            f"  finalized: {r.is_finalized}   docs: {r.doc_count}   "
            f"analyzed: {r.analyzed_doc_count}   "
            f"with-txns: {r.docs_with_transactions}"
        )
        if r.live_recommendation:
            print(
                f"  LIVE     : tier={r.live_tier} recommendation={r.live_recommendation} "
                f"score={r.live_score}"
            )
            if r.live_hard_reasons:
                print(f"             hard_decline   : {_fmt_list(r.live_hard_reasons, 5)}")
            if r.live_soft_concerns:
                print(f"             soft_concerns  : {_fmt_list(r.live_soft_concerns, 5)}")
        else:
            print(f"  LIVE     : UNAVAILABLE — {r.live_error}")
        if r.new_integrity is not None or r.new_band is not None:
            print(
                f"  NEW   A  : verdict={r.new_integrity or '—'}  "
                f"branches=[{_fmt_list(r.new_integrity_branches, 3)}]"
            )
            print(
                f"  NEW   B  : band={r.new_band or '—'}  action={r.new_band_action or '—'}  "
                f"top_severity={r.new_track_b_top_severity or '—'}"
            )
            print(
                f"             factors        : {_fmt_list(r.new_band_factors, 4)}"
            )
            if r.new_intl_share_pct is not None:
                print(f"             intl_share     : {r.new_intl_share_pct:.1f}%")
            if r.new_revenue_basis is not None:
                print(f"  NEW   C  : revenue_basis = ${r.new_revenue_basis:,.0f}")
        else:
            reason = r.new_error or r.new_unified_insufficient_reason or "no data"
            print(f"  NEW      : UNAVAILABLE — {reason}")
        print(f"  CATEGORY : {r.category}")
        print(f"             → {r.rationale}")


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────


def main() -> int:
    merchant_repo = SupabaseMerchantRepository()
    docs = SupabaseDocumentRepository()
    try:
        all_merchants = merchant_repo.list_all()
    except Exception as exc:
        print(f"FATAL: could not list merchants: {exc!r}", file=sys.stderr)
        traceback.print_exc()
        return 2

    rows: list[MerchantComparison] = []
    for m in all_merchants:
        try:
            row = _process_one(m, docs)
        except Exception as exc:
            row = MerchantComparison(
                merchant_id=m.id,
                business_name=m.business_name,
                is_finalized=getattr(m, "is_finalized", False),
                doc_count=-1,
                analyzed_doc_count=-1,
                docs_with_transactions=-1,
                live_error=f"processing failed: {exc!r}",
                new_error=f"processing failed: {exc!r}",
            )
            row.category = CAT_AMBIGUOUS
            row.rationale = "processing failed; needs operator review"
        rows.append(row)

    # Sort: regression risks first, then new-better, then ambiguous, then
    # agreement, then insufficient. Inside each bucket alphabetise.
    order_key = {
        CAT_OLD_BETTER: 0,
        CAT_NEW_BETTER: 1,
        CAT_AMBIGUOUS: 2,
        CAT_AGREEMENT: 3,
        CAT_INSUFFICIENT: 4,
    }
    rows.sort(key=lambda r: (order_key.get(r.category, 99), r.business_name.lower()))

    _print_summary(rows)
    _print_table(rows)
    _print_per_merchant_detail(rows)

    # Exit non-zero if regression-risk rows exist — makes the script
    # cheap to bolt into a CI/scheduled check later.
    if any(r.category == CAT_OLD_BETTER for r in rows):
        print()
        print("EXIT: regression-risk rows present → exit 3")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
