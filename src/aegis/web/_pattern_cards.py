"""Build operator-facing pattern cards from a PatternAnalysis.

The parser emits ``Pattern`` dataclasses with a ``code`` (machine name),
``severity`` (0-100 contribution to fraud_score), ``detail`` (compact
one-liner) and ``source_ids`` (UUIDs into the ``transactions`` table).

The dashboard renders a card per pattern with:

* a plain-English title and description (so an underwriter reading
  cold understands what triggered),
* the severity color band,
* the parser's ``detail`` string (the actual values: counts, dollar
  amounts, dates),
* an expandable table of the contributing transactions — so the broker
  can answer "which rows raised this flag" without leaving the page.

``mca_stacking`` is deliberately excluded — it has its own richer card
(``_stacking_card.html.j2``) that breaks down per-position daily
equivalents. Including it twice would double-count visually.

Additionally, when a hard-decline reason is attached for the same fact a
pattern card would describe (the Phase 9 rules
``acceleration_clause_triggered`` and ``unauthorized_withdrawal_dispute``
fire BOTH as a pattern severity contributor AND as their own
hard-decline line), the pattern card is suppressed via the
``hard_declined_codes`` filter on ``build_pattern_cards``. The
hard-decline list at the top of the verdict carries the now-humanized
copy (via ``HARD_DECLINE_COPY``) and the worker reads the same fact
once, not twice. The signal still fires for scoring; only the duplicate
DISPLAY card is suppressed.

This module also carries operator-facing copy for two adjacent signal
surfaces:

* ``HARD_DECLINE_COPY`` — humanizes the raw ``ScoreResult.hard_decline_reasons``
  strings that ``score.py:_check_hard_declines`` emits, so the verdict's
  hard-decline list reads as worker-language sentences rather than raw
  identifier blobs like ``acceleration_clause_triggered: …``.
* ``SOFT_CONCERN_COPY`` — same treatment for ``ScoreResult.soft_concerns``,
  including the soft-score-only codes ``ai_generated_statement_*``,
  ``customer_concentration_severe``, ``payroll_absent_high_revenue``,
  etc. that never reach the pattern-card surface (they're score-only,
  not ``Pattern(...)`` emissions).

The two adjacent maps are intentionally co-located with ``PATTERN_COPY``
because all three feed the same dossier verdict / findings layout and
share the same "worker-readable language" discipline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from aegis.parser.models import ClassifiedTransaction
from aegis.parser.patterns import PatternAnalysis


@dataclass(frozen=True)
class PatternCardCopy:
    """Static copy keyed by pattern code. Operator-readable language."""

    title: str
    description: str


# Pattern code -> operator-facing copy. Every code emitted by
# aegis.parser.patterns.analyze_patterns must have an entry here (except
# the two intentionally rendered elsewhere). When a new detector lands
# in patterns.py, add its copy block here in the same commit.
PATTERN_COPY: Final[dict[str, PatternCardCopy]] = {
    "duplicate_deposits_detected": PatternCardCopy(
        title="Duplicate Deposits",
        description=(
            "Same date and exact amount appears in more than one deposit row. "
            "Legitimate when a merchant has truly identical sales across "
            "channels; suspicious when it suggests fabricated rows used to "
            "inflate revenue."
        ),
    ),
    "synthetic_low_variance": PatternCardCopy(
        title="Deposits Look Synthetic",
        description=(
            "Deposit amounts cluster tightly around the mean (coefficient of "
            "variation < 15% across 10+ deposits). Real merchant cash flow "
            "varies; uniformly-sized deposits often indicate fabricated or "
            "ACH-padded statements."
        ),
    ),
    "round_number_deposits": PatternCardCopy(
        title="Round-Number Deposits",
        description=(
            "More than 75% of deposits are exact multiples of $100. Real "
            "merchant revenue rarely lands on clean multiples — this pattern "
            "often signals fabricated activity or rounded-up cash counts."
        ),
    ),
    "preloan_spike": PatternCardCopy(
        title="Pre-Loan Deposit Spike",
        description=(
            "Deposits in the last 7 or 14 days of the statement exceed 2.5x "
            "the prior-period weekly average. Common indicator that a "
            "merchant is padding the account before applying for funding. "
            "Verify against historical statements if available."
        ),
    ),
    "nsf_clustering_short": PatternCardCopy(
        title="NSF Concentration (Short Statement)",
        description=(
            "More than 3 non-sufficient-funds fees in a statement shorter "
            "than 20 days. Short-window NSF clustering is structural cash-"
            "flow stress, not a one-off accident."
        ),
    ),
    "nsf_late_concentration": PatternCardCopy(
        title="NSF Concentration (Late in Statement)",
        description=(
            "At least 3 NSF fees fell in the final 30 days of a longer "
            "statement. Indicates the merchant's cash position is "
            "deteriorating, not improving — a late-period decline is more "
            "worrying than early-period bumps that resolved."
        ),
    ),
    "wash_deposit_suspected": PatternCardCopy(
        title="Wash Deposits Suspected",
        description=(
            "Deposit and withdrawal pairs of near-equal size (within 2%) "
            "appear within 5 days of each other. Pattern of moving money in "
            "and out to inflate apparent deposit volume without real revenue."
        ),
    ),
    "paydown_mca_suspected": PatternCardCopy(
        title="MCA Paydown Pattern",
        description=(
            "Same-payee debits with monotonically descending amounts (≥5 "
            "events, ≤5% noise on the way down, ending ≤85% of the start). "
            "Suggests an existing MCA position is being paid down — often a "
            "renewal-stage merchant. Confirm with the broker."
        ),
    ),
    "deposit_velocity_spike": PatternCardCopy(
        title="Deposit Velocity Spike",
        description=(
            "A 7-day rolling window contains more than 3x the period-average "
            "daily deposit count. Different signal from a dollar-amount "
            "spike — catches merchants stuffing deposit rows to look busier "
            "than they are."
        ),
    ),
    "withdrawal_acceleration": PatternCardCopy(
        title="MCA Debit Acceleration",
        description=(
            "MCA debit count in the last 7 days is more than 1.5x the prior "
            "weekly average. Indicates new positions being stacked late in "
            "the statement — verify whether the merchant disclosed all "
            "existing MCA obligations."
        ),
    ),
    "acceleration_clause_triggered": PatternCardCopy(
        title="MCA Acceleration",
        description=(
            "A recurring MCA position broke and a single debit 5-10x larger "
            "than the prior median posted to the same payee - the funder "
            "called the loan after default. Decline class flag: the merchant "
            "defaulted on a prior funder, outside our risk appetite."
        ),
    ),
    "recent_account_opening": PatternCardCopy(
        title="Recent Account Opening",
        description=(
            "Bank account opened less than 60 days before today. Doesn't "
            "satisfy our 6-month intake baseline on its face. If the "
            "BUSINESS itself is also under 6 months, decline; if it's an "
            "established business that recently switched banks, request "
            "prior bank statements before declining."
        ),
    ),
    "payroll_absent": PatternCardCopy(
        title="Payroll Absent",
        description=(
            "No payroll-processor activity (ADP / Gusto / Paychex / etc.) "
            "across a ≥21 day period with ≥$50k revenue. Real operating "
            "businesses at this scale almost always have payroll — ask how "
            "the merchant pays employees and contractors. 1099-only is "
            "plausible; W-2 claims without a payroll trace is a red flag."
        ),
    ),
    "unauthorized_withdrawal_dispute": PatternCardCopy(
        title="Unauthorized Withdrawal Dispute",
        description=(
            "A credit row with reversal/dispute keywords ('reversal', "
            "'unauthorized', 'ach return credit') pairs with a prior MCA "
            "debit within 14 days at near-equal amount. The merchant "
            "fought (and won) a funder withdrawal — most funders won't "
            "touch a merchant who disputed a prior funder. Near-decline "
            "single-event signal. Ask directly which funder, why the "
            "dispute, and how it resolved."
        ),
    ),
    "unreconciled_internal_transfer": PatternCardCopy(
        title="Unreconciled Internal Transfer",
        description=(
            "Transfer-OUT > $500 with no matching transfer-IN (within "
            "$1, within ±3 days) in the bundle. Money leaving the visible "
            "accounts to an undisclosed account — often hosting an "
            "undisclosed MCA. Severity scales with unmatched count (15 "
            "base, +5 per additional, capped at 40). Request all bank "
            "account statements; merchants hiding accounts often hide MCAs."
        ),
    ),
    "mca_payoff_signature": PatternCardCopy(
        title="MCA Payoff Signature",
        description=(
            "Any single debit > $5,000 whose description contains a known "
            "funder token. Recently-paid-off MCA still counts in renewal-"
            "likelihood scoring even if not currently active. Look-closer "
            "signal, not a decline. Ask the merchant when this funder was "
            "paid off and confirm no balance remains."
        ),
    ),
    "customer_concentration": PatternCardCopy(
        title="Customer Concentration",
        description=(
            "Top single counterparty's share of revenue exceeds 30% "
            "(severity 10), 40% (20), or 60% (30). Single-customer "
            "dependency — lose that customer and the merchant can't "
            "service the advance. Material above 50%; over 70% pauses "
            "the deal regardless of other signals. Ask who the customer "
            "is, contract length, and whether it's renewable."
        ),
    ),
    "chargeback_velocity": PatternCardCopy(
        title="Chargeback / Refund Velocity",
        description=(
            "Debits containing chargeback or refund keywords "
            "('chargeback', 'refund', 'return ach', 'dispute', 'merchant "
            "return', 'credit reversal'). Three paths: short statements "
            "with ≥5 rows; longer statements where last-14d count > 1.5x "
            "the prior fortnight; longer statements with ≥6 total rows. "
            "Leading indicator of B2C distress and dispute risk on the "
            "funder's holdback. Cross-check against revenue scale — 1% "
            "is normal, > 3% is alarming."
        ),
    ),
    "processor_holdback_detected": PatternCardCopy(
        title="Processor Holdback Detected",
        description=(
            "≥10 deposits from a known card processor (Stripe / Square / "
            "Toast / etc.) over a ≥14 day period, with the daily-summed "
            "coefficient of variation ≥ 0.50. Variable processor payouts "
            "strongly imply an in-place MCA holdback — a funder taking a "
            "cut before payout reaches the bank. Ask directly whether "
            "any card processor payout is split or held by a funder, "
            "and cross-check against the MCA stacking count."
        ),
    ),
}


def _severity_band(severity: int) -> str:
    """Map a Pattern.severity to a CSS color band.

    Severities in aegis.parser.patterns currently range 15-50; bands
    chosen so the lowest-severity patterns (round_number_deposits=15)
    stay in 'warn' while the heaviest (wash_deposit_suspected=35,
    duplicate_deposits=30) reach 'neg'.
    """
    if severity >= 30:
        return "neg"
    if severity >= 15:
        return "warn"
    return "pos"


# Patterns rendered elsewhere — skip them in the card list.
# ``mca_stacking`` has its own richer card driven by ``StackingCard``.
# ``recent_account_opening`` used to be excluded because there was nothing
# to drill into; with the explanation panel landing in chunk 2 of the
# evidence drill-down work, it now renders as a pattern card so the
# operator playbook sits next to the flag instead of hiding inside the
# score-breakdown panel only.
# ``unreconciled_internal_transfer_v2`` is a shadow detector (lives on
# ``shadow_patterns``, not ``patterns``); ``build_pattern_cards`` only
# walks the live ``patterns`` list, so the code is unreachable here by
# design. Surfaced via the [SHADOW] flag and the dossier shadow-signals
# panel instead.
# ``ai_generated_statement`` is the same — emitted by
# ``forensic.ai_statement.detect_ai_generated_statement`` and appended
# to ``shadow_patterns`` by ``parser.pipeline``. Listed defensively
# even though the source-file AST walker in
# ``_emitted_pattern_codes_from_source`` only scans ``parser/patterns.py``
# today; a future cleanup that consolidates the emit-call into
# ``parser/patterns.py`` would otherwise fail the contract test.
_RENDERED_ELSEWHERE: Final[frozenset[str]] = frozenset(
    {
        "mca_stacking",
        "unreconciled_internal_transfer_v2",
        "ai_generated_statement",
        # 2026-06-28 — Agent 5 application-vs-measured detectors. These
        # don't render as generic pattern cards; they surface on the
        # Track B unified panel (``_unified_tracks_panel.html.j2``) as
        # CRITICAL / ELEVATED ``FactorReason`` rows because their
        # interpretation is risk-band-anchored, not pattern-detail-
        # anchored. The pattern_code copy contract treats them as
        # rendered-elsewhere so the test below doesn't false-positive.
        "impossible_payment_load",
        "stated_vs_measured_revenue_divergence",
    }
)


# Pattern codes that ALSO surface as a hard-decline reason on the same
# dossier (Phase 9 dual-tier signals, v2 catalog Bucket B.7). When the
# scorer attaches the equivalent hard-decline reason, the pattern card
# is suppressed at the DISPLAY layer so the worker sees one
# authoritative line — the hard-decline reason — instead of the same
# fact in a "soft severity" pattern card AND a "decline" verdict line.
# The pattern itself still fires (severity still flows into
# ``patterns.fraud_score`` upstream). This map answers "if I see this
# hard-decline reason on the deal, which pattern card should I suppress
# below?".
PATTERN_CODE_BY_HARD_DECLINE_REASON: Final[dict[str, str]] = {
    "acceleration_clause_triggered": "acceleration_clause_triggered",
    # Score-side reason carries an ``_active`` suffix
    # (``score.py:197``) where the pattern code does not — same fact,
    # different name. Mapped explicitly here so the suppression bridges
    # the naming gap (v2 catalog Bucket B.7).
    "unauthorized_withdrawal_dispute_active": "unauthorized_withdrawal_dispute",
}


@dataclass(frozen=True)
class PatternCard:
    code: str
    title: str
    description: str
    detail: str
    severity: int
    severity_band: str
    source_transactions: list[ClassifiedTransaction]


def _hard_declined_pattern_codes(
    hard_decline_reasons: list[str] | None,
) -> frozenset[str]:
    """Translate the verdict's hard-decline reason list into the set of
    pattern codes whose display card should be suppressed.

    The hard-decline list may carry detail after the code
    (``acceleration_clause_triggered: OnDeck …``); the lookup key is the
    bare code prefix. The ``PATTERN_CODE_BY_HARD_DECLINE_REASON`` map
    also bridges the ``_active`` suffix between
    ``unauthorized_withdrawal_dispute_active`` (scorer) and
    ``unauthorized_withdrawal_dispute`` (pattern).

    Returns an empty frozenset when no reasons are passed — i.e. cards
    render unchanged when there is no decline to anchor them to. This is
    the right behavior for the ``refer`` and ``approve`` recommendations
    where the pattern card is the only place the fact surfaces.
    """
    if not hard_decline_reasons:
        return frozenset()
    suppressed: set[str] = set()
    for reason in hard_decline_reasons:
        bare = reason.split(":", 1)[0].strip()
        mapped = PATTERN_CODE_BY_HARD_DECLINE_REASON.get(bare)
        if mapped is not None:
            suppressed.add(mapped)
    return frozenset(suppressed)


def build_pattern_cards(
    pattern_analysis: PatternAnalysis | None,
    transactions: list[ClassifiedTransaction],
    hard_decline_reasons: list[str] | None = None,
) -> list[PatternCard]:
    """Return one card per surfaced pattern, ordered by severity descending.

    Patterns lacking copy in PATTERN_COPY are skipped silently (so a
    parser-side new detector doesn't crash the dashboard) — but `make
    check` should catch the omission via the regression test that
    asserts every code in PATTERN_COPY matches an emitted Pattern.

    When ``hard_decline_reasons`` is passed, patterns whose code is
    already represented as a hard-decline line on the verdict are
    suppressed here (the worker sees the same fact once, under the
    decline banner, not duplicated as a soft-severity card below). This
    closes v2 catalog Bucket B.7 — the dual-tier acceleration / dispute
    presentation. The underlying scoring is unchanged: the pattern still
    contributes to ``patterns.fraud_score`` and ``score.py`` still
    attaches the hard-decline reason. Only the duplicate card is hidden.
    """
    if pattern_analysis is None:
        return []
    suppressed = _hard_declined_pattern_codes(hard_decline_reasons)
    by_id: dict[UUID, ClassifiedTransaction] = {t.id: t for t in transactions}
    cards: list[PatternCard] = []
    for p in pattern_analysis.patterns:
        if p.code in _RENDERED_ELSEWHERE:
            continue
        if p.code in suppressed:
            continue
        copy = PATTERN_COPY.get(p.code)
        if copy is None:
            continue
        src = [by_id[i] for i in p.source_ids if i in by_id]
        cards.append(
            PatternCard(
                code=p.code,
                title=copy.title,
                description=copy.description,
                detail=p.detail,
                severity=p.severity,
                severity_band=_severity_band(p.severity),
                source_transactions=src,
            )
        )
    cards.sort(key=lambda c: c.severity, reverse=True)
    return cards


# ---------------------------------------------------------------------------
# Hard-decline and soft-concern humanizers
#
# ``score.py`` emits hard-decline reasons and soft concerns as raw
# identifier strings (``acceleration_clause_triggered``,
# ``ai_generated_statement_strong``, …). The dossier used to render
# these verbatim — workers staring at ``customer_concentration_severe``
# had no operator-language hook telling them what the engine was
# flagging or why. The two maps + ``humanize_hard_decline`` /
# ``humanize_soft_concern`` helpers below give the template a one-call
# path to the same plain-English copy discipline ``PATTERN_COPY``
# already provides.
#
# Every reason / concern code score.py emits MUST have an entry here —
# the regression tests below assert it. Adding a new score-side code
# without registering its copy raises the test, the same way the
# pattern-card regression test catches new ``Pattern(code=...)``
# additions.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HumanReason:
    """Renderable shape for a single hard-decline reason or soft concern.

    ``code`` is the bare identifier (no detail tail), ``title`` is the
    short worker-facing phrase that opens the line, and ``description``
    is the one-sentence rationale that follows. ``detail`` carries the
    optional value tail the scorer attached (``: 2 EOF markers``,
    ``: score=42``, …) verbatim, so the worker sees the concrete number
    that fired alongside the humanized copy.

    ``confidence`` mirrors the v2 catalog tags — ``confident`` for copy
    that maps cleanly to the engine logic, ``best_guess`` for copy
    drafted from the catalog but not operator-reviewed yet. Surfaces
    only in the catalog doc and tests; not rendered on the dossier.
    """

    code: str
    title: str
    description: str
    detail: str = ""
    confidence: str = "confident"


# Hard-decline reason -> humanized copy. Codes are the BARE identifier
# (no ``: …`` tail). The scorer emits a handful with an interpolated
# value tail; the humanizer parses that off and surfaces it as
# ``HumanReason.detail`` so the worker sees both the rationale and the
# concrete number that triggered.
HARD_DECLINE_COPY: Final[dict[str, HumanReason]] = {
    "ofac_sanctions_match": HumanReason(
        code="ofac_sanctions_match",
        title="OFAC sanctions match",
        description=(
            "Merchant or owner name matched the Treasury SDN list. Federal "
            "prohibition on funding; matched name and SDN UID below. File "
            "the Initial Report of Blocked Property within 10 business days."
        ),
    ),
    "stacking_exceeds_limit": HumanReason(
        code="stacking_exceeds_limit",
        title="Stacking exceeds limit",
        description=(
            "Active MCA position count is over AEGIS's tolerance. The deal "
            "cannot be stacked on top of the existing book."
        ),
    ),
    "debt_to_revenue_exceeds_40pct": HumanReason(
        code="debt_to_revenue_exceeds_40pct",
        title="Debt-to-revenue exceeds 40%",
        description=(
            "Existing MCA debt service consumes more than 40% of monthly "
            "revenue. Adding another payment is structurally unsupportable."
        ),
    ),
    "fraud_score_critical": HumanReason(
        code="fraud_score_critical",
        title="Fraud score critical",
        description=(
            "Composite fraud score (metadata + math + patterns) crossed the "
            "auto-decline threshold of 70. Tampering or fabrication signals "
            "dominate the deal; see the pattern findings below for the "
            "constituent flags."
        ),
    ),
    "incremental_pdf_saves": HumanReason(
        code="incremental_pdf_saves",
        title="Incremental PDF saves",
        description=(
            "The bank-statement PDF carries more than one %%EOF marker. "
            "Someone saved the file after the bank's original export — "
            "either a legitimate viewer re-save (Adobe / Preview) or an "
            "edit. Combined with other tampering signals, treat as decline."
        ),
    ),
    "revenue_below_minimum": HumanReason(
        code="revenue_below_minimum",
        title="Revenue below minimum",
        description=(
            "Monthly revenue is below the AEGIS floor. MCA math doesn't "
            "work at this revenue scale; the deal cannot be priced."
        ),
    ),
    "industry_excluded": HumanReason(
        code="industry_excluded",
        title="Industry excluded",
        description=(
            "The merchant's industry sits on Commera's avoid list "
            "(cannabis, firearms, adult, etc., per the configured roster). "
            "No funder will accept this NAICS regardless of cashflow."
        ),
    ),
    "days_negative_gt_15": HumanReason(
        code="days_negative_gt_15",
        title="More than 15 negative days",
        description=(
            "The bank account spent more than two weeks below $0 across "
            "the statement period. Chronic negative is structural cash "
            "stress, not a one-off processing accident."
        ),
    ),
    "nsf_count_gte_10": HumanReason(
        code="nsf_count_gte_10",
        title="10 or more NSF events",
        description=(
            "10+ non-sufficient-funds fees in the period. Bouncing payments "
            "at this rate is auto-decline territory — no funder takes this "
            "level of NSF history."
        ),
    ),
    "returned_ach_gt_5": HumanReason(
        code="returned_ach_gt_5",
        title="More than 5 returned ACHs",
        description=(
            "More than five returned ACH debits in the period. Bouncing "
            "ACHs at this rate prevents any new funder from establishing "
            "daily holdbacks."
        ),
    ),
    "tib_under_3_months": HumanReason(
        code="tib_under_3_months",
        title="Business under 3 months old",
        description=(
            "Time in business is below the 3-month floor. No funding "
            "appetite without operating history; revisit when the merchant "
            "has at least three months of statements."
        ),
    ),
    "validation_failed_manual_review_required": HumanReason(
        code="validation_failed_manual_review_required",
        title="Validation failed — manual review",
        description=(
            "Deterministic math gate did not reconcile (period totals, "
            "daily-balance walk, or intraday running-balance failed). The "
            "extraction is unusable as-is; the deal cannot proceed until "
            "the statement is re-parsed or re-uploaded."
        ),
    ),
    "prior_default": HumanReason(
        code="prior_default",
        title="Defaulted on prior MCA",
        description=(
            "On a renewal: the merchant defaulted on a previous AEGIS-routed "
            "advance. Repeat default risk is outside Commera's appetite."
        ),
    ),
    "dscr_below_1": HumanReason(
        code="dscr_below_1",
        title="Debt-service coverage below 1.0",
        description=(
            "Existing obligations plus the proposed daily debit exceed "
            "monthly revenue (DSCR < 1.00). The merchant cannot service "
            "the new advance even on paper."
        ),
    ),
    "acceleration_clause_triggered": HumanReason(
        code="acceleration_clause_triggered",
        title="Funder acceleration on prior MCA",
        description=(
            "A recurring MCA position broke and a single debit 5-10x larger "
            "than the prior median posted to the same payee. The funder "
            "called the loan after default — outside Commera's risk "
            "appetite. See the source debits on the stacking card."
        ),
    ),
    "unauthorized_withdrawal_dispute_active": HumanReason(
        code="unauthorized_withdrawal_dispute_active",
        title="Active unauthorized-withdrawal dispute",
        description=(
            "The merchant successfully reversed a prior funder's MCA debit "
            "(reversal credit paired to an MCA debit within 14 days at "
            "near-equal amount). Most funders won't touch a merchant who "
            "disputed a prior funder; treat as near-decline single-event "
            "signal."
        ),
    ),
    "bank_statement_tampering_confirmed": HumanReason(
        code="bank_statement_tampering_confirmed",
        title="Bank statement tampering confirmed",
        description=(
            "Composite forensic signals (font / page-layer / EOF / author) "
            "indicate the PDF was altered after the bank exported it. The "
            "extraction cannot be trusted; re-request a fresh export."
        ),
        confidence="best_guess",
    ),
}


# Soft-concern -> humanized copy. The scorer attaches these to the
# verdict's "soft concerns" list; the dossier renders them as
# "verify before submission" line items. Codes carry the same value
# tail discipline as hard declines.
SOFT_CONCERN_COPY: Final[dict[str, HumanReason]] = {
    "soft_score_below_threshold": HumanReason(
        code="soft_score_below_threshold",
        title="Composite score below tier floor",
        description=(
            "The soft-score aggregate landed in F-tier without a single "
            "hard decline firing — the merchant's profile is weak across "
            "many small factors rather than failing one big one. Treat as "
            "decline-equivalent for funder routing."
        ),
    ),
    "missing_time_in_business": HumanReason(
        code="missing_time_in_business",
        title="Time in business missing",
        description=(
            "Merchant did not supply months in business. Operator must "
            "confirm with the broker before routing; funder rate cards "
            "gate on this number."
        ),
    ),
    "missing_credit_score": HumanReason(
        code="missing_credit_score",
        title="Credit score missing",
        description=(
            "FICO not on file. Most A / B paper funders require it; "
            "request it from the broker before sending the deal out."
        ),
    ),
    "customer_concentration_severe": HumanReason(
        code="customer_concentration_severe",
        title="Operator-flagged customer concentration",
        description=(
            "The broker / operator entered a top-customer concentration "
            "above 60% on the merchant form. See the Customer "
            "Concentration finding below for the statement-derived view "
            "of the same fact."
        ),
    ),
    "top_5_expense_concentration": HumanReason(
        code="top_5_expense_concentration",
        title="Top-5 expense concentration",
        description=(
            "Five payees account for the bulk of the merchant's withdrawals. "
            "Verify the expense structure — heavy concentration on a few "
            "vendors implies single-source-of-supply risk."
        ),
    ),
    "payroll_absent_high_revenue": HumanReason(
        code="payroll_absent_high_revenue",
        title="Payroll absent at revenue scale",
        description=(
            "No payroll-processor activity detected at a revenue level "
            "where one would be expected. See the Payroll Absent pattern "
            "finding below for the period and revenue trigger. Confirm "
            "with the merchant whether workers are 1099 or off-statement."
        ),
    ),
    "ai_generated_statement_strong": HumanReason(
        code="ai_generated_statement_strong",
        title="Statement looks AI-generated (strong)",
        description=(
            "Composite score ≥85 across three sub-signals: descriptions "
            "look too-clean, digit-noise (trace IDs / confirmation numbers) "
            "is low, and round-number deposits dominate. Master plan §6.4 "
            "forbids auto-decline on this alone; manually request a fresh "
            "statement export on a screenshare and compare."
        ),
        confidence="best_guess",
    ),
    "ai_generated_statement_medium": HumanReason(
        code="ai_generated_statement_medium",
        title="Statement looks AI-generated (medium)",
        description=(
            "Composite score 70-84. Worth a closer look but not a blocker. "
            "Spot-check 3-5 transaction descriptions against typical "
            "statements from the same bank — real bank descriptions carry "
            "inconsistent capitalization and trace IDs."
        ),
        confidence="best_guess",
    ),
    "ai_generated_statement_weak": HumanReason(
        code="ai_generated_statement_weak",
        title="Statement looks AI-generated (weak)",
        description=(
            "Composite score 55-69. Often fires on clean modern statement "
            "formats (some credit unions, online-only banks) that are "
            "legitimately clean. Note but proceed."
        ),
        confidence="best_guess",
    ),
    # U8 — APR could not be computed for the suggested pricing terms.
    # Emitted by score.py when ``calculate_apr`` raises APRCalculationError
    # (scipy.optimize.brentq could not bracket a root for the deal's
    # advance / factor / holdback / term combination). Soft-concern path:
    # the deal still scores and tiers, but the APR disclosure block can't
    # render a number until pricing is adjusted.
    "apr_not_computable": HumanReason(
        code="apr_not_computable",
        title="APR could not be computed",
        description=(
            "The IRR solver could not bracket a root for the recommended "
            "factor / holdback / term combination. The deal still scores "
            "and tiers; APR disclosure is unavailable until pricing is "
            "tightened. Check the suggested advance against the funder's "
            "minimum factor envelope."
        ),
    ),
}


# Strip ``code: …`` -> ``(code, detail_or_empty)``. Mirrors the
# convention ``_flag_labels._split_code_detail`` already uses for
# ``[CATEGORY] code: detail`` flags.
_REASON_DETAIL_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<code>[a-z0-9_+]+)(?:\s*:\s*(?P<detail>.*))?$"
)


def _split_reason(raw: str) -> tuple[str, str]:
    """Split a raw reason / concern string into (code, detail).

    ``score.py`` emits identifiers like ``acceleration_clause_triggered``
    bare, OR ``stacking_exceeds_limit: 3 active positions`` with a value
    tail. The regex tolerates both; falls back to the trimmed input as
    the code when the regex misses so an unexpected format still renders
    with the raw string visible.
    """
    if not raw:
        return "", ""
    stripped = raw.strip()
    m = _REASON_DETAIL_RE.match(stripped)
    if m:
        return m.group("code"), (m.group("detail") or "").strip()
    return stripped, ""


def humanize_hard_decline(raw: str) -> HumanReason:
    """Return a ``HumanReason`` for a raw hard-decline reason string.

    Unknown codes fall back to a usable shape (raw code as title, raw
    detail passed through) — same fallback contract ``humanize_flag``
    uses, so an un-registered code never crashes the dossier.
    """
    code, detail = _split_reason(raw)
    spec = HARD_DECLINE_COPY.get(code)
    if spec is None:
        return HumanReason(
            code=code or "unknown",
            title=_humanize_unknown_code(code),
            description=(
                "No operator copy registered for this hard-decline "
                "reason. Treat as decline and surface the raw identifier "
                "below for engineering."
            ),
            detail=detail,
        )
    return HumanReason(
        code=spec.code,
        title=spec.title,
        description=spec.description,
        detail=detail,
        confidence=spec.confidence,
    )


def humanize_soft_concern(raw: str) -> HumanReason:
    """Return a ``HumanReason`` for a raw soft-concern string.

    Same fallback contract as ``humanize_hard_decline``. Additionally
    falls back to a prefix lookup for codes carrying a numeric suffix —
    ``score.py`` emits ``top_5_expense_concentration_{pct}pct`` with the
    actual percentage baked into the code rather than the detail tail.
    The prefix lookup pulls the suffix off, surfaces it as
    ``HumanReason.detail``, and maps the bare prefix to the registered
    copy so the worker still sees worker-language text.
    """
    code, detail = _split_reason(raw)
    spec = SOFT_CONCERN_COPY.get(code)
    if spec is not None:
        return HumanReason(
            code=spec.code,
            title=spec.title,
            description=spec.description,
            detail=detail,
            confidence=spec.confidence,
        )
    # Prefix fallback — covers the ``top_5_expense_concentration_45pct``
    # shape where the value is suffixed into the code. Picks the longest
    # registered prefix that matches so the most specific copy wins.
    prefix_match = max(
        (key for key in SOFT_CONCERN_COPY if code.startswith(key + "_")),
        key=len,
        default=None,
    )
    if prefix_match is not None:
        spec = SOFT_CONCERN_COPY[prefix_match]
        suffix = code[len(prefix_match) + 1 :]
        merged_detail = f"{suffix}; {detail}".strip("; ") if detail else suffix
        return HumanReason(
            code=spec.code,
            title=spec.title,
            description=spec.description,
            detail=merged_detail,
            confidence=spec.confidence,
        )
    return HumanReason(
        code=code or "unknown",
        title=_humanize_unknown_code(code),
        description="Verify before submission.",
        detail=detail,
    )


def _humanize_unknown_code(code: str) -> str:
    """Title-cased fallback for an un-registered reason / concern code.

    ``brand_new_reason`` -> ``Brand new reason``. Used only on the
    fallback path; registered titles are hand-authored.
    """
    if not code:
        return "(unknown reason)"
    return code.replace("_", " ").strip().capitalize()


def pattern_has_customer_concentration(
    pattern_analysis: PatternAnalysis | None,
) -> bool:
    """Return True iff a ``customer_concentration`` Pattern is emitted.

    Used by the dossier template to suppress the
    ``soft_signals.customer_concentration`` aggregate-derived card when
    the richer pattern-card view is already on the page — same fact, two
    sources (v2 catalog Bucket B.1). The pattern card carries severity
    banding and the source-row drill-down; the aggregate card carries
    nothing the pattern card lacks. Keeping the pattern card and
    suppressing the aggregate card gives the worker the same information
    without the visual double-render.
    """
    if pattern_analysis is None:
        return False
    return any(p.code == "customer_concentration" for p in pattern_analysis.patterns)


__all__ = [
    "HARD_DECLINE_COPY",
    "PATTERN_CODE_BY_HARD_DECLINE_REASON",
    "PATTERN_COPY",
    "SOFT_CONCERN_COPY",
    "HumanReason",
    "PatternCard",
    "build_pattern_cards",
    "humanize_hard_decline",
    "humanize_soft_concern",
    "pattern_has_customer_concentration",
]


def _emitted_pattern_codes_from_source() -> frozenset[str]:
    """AST-walk ``aegis.parser.patterns`` for every ``Pattern(code=...)``
    literal. Used by the regression test that prevents the silent-drop
    bug (a detector emits a code that ``build_pattern_cards`` doesn't
    recognize and quietly skips, hiding the card from the dossier).

    Exposed as a module-level helper so the test stays one-liner
    simple. Returns the union of every ``code=`` keyword argument
    passed to a ``Pattern(...)`` constructor in patterns.py.
    """
    import ast
    from pathlib import Path

    source_path = Path(__file__).resolve().parent.parent / "parser" / "patterns.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    codes: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "Pattern"):
            continue
        for kw in node.keywords:
            if kw.arg != "code":
                continue
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                codes.add(kw.value.value)
    return frozenset(codes)
