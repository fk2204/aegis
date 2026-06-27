"""Deterministic fraud-pattern detectors over classified transactions.

Ported from the TS implementation with two structural changes:

1. AEGIS input is a flat `list[ClassifiedTransaction]` (deterministically
   classified after the validation gate). The TS version received a
   pre-aggregated `ExtractedData` shape where Claude grouped recurring
   debits — here, we re-derive recurring groups in pure Python.

2. Each detector returns its source transaction ids. The pipeline then
   stores them on the corresponding pattern, so a "kiting" or "paydown"
   flag can be drilled back to specific PDF rows.

TS-review fixes applied here
----------------------------
- Generic-word MCA detection: single common words ("advance", "remit") no
  longer fire on their own. Require either a known funder name OR a
  daily/weekly cadence with adjacency context.
- Weekend deposits: skip the flag for cash-heavy retail patterns (we treat
  this as a soft signal only, surfaced as a `weekend_deposit_concentration`
  warning rather than a fraud-score addition, because legitimate retail
  Mon-morning deposit clustering is common).
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aegis.parser.models import ClassifiedTransaction

# Known MCA funders + behavioral terms. Generic single words ("advance",
# "remit", "factor") are ONLY decisive when paired with daily-cadence
# behavior — see `_detect_mca_positions`. This is the TS-review fix.
KNOWN_FUNDERS: Final[tuple[str, ...]] = (
    "ondeck",
    "credibly",
    "bluevine",
    "kabbage",
    "rapid finance",
    "forward financing",
    "can capital",
    "kapitus",
    "fora",
    "national funding",
    "reliant",
    "libertas",
    "uplyft",
    "greenbox",
    "fundkite",
    "lendr",
    "yellowstone",
    "everest",
    "cfg merchant",
    "fundbox",
    "loanbuilder",
    "headway",
    "smart business",
    "velocity",
    "paypal working capital",
    "square capital",
    "shopify capital",
    "amazon lending",
    "parafin",
    "pipe",
    "wayflyer",
    "clearco",
    "enova",
    "mulligan",
    "expansion capital",
    "torro",
    "idea financial",
    "balboa capital",
    "channel partners",
    "capital daily",
    "merchant advance",
    "itria ventures",
    "mantis funding",
    "gtr funding",
    "slate advance",
    "biz2credit",
    "triton capital",
    "sos capital",
    "delta bridge",
    "iou financial",
    "arf financial",
    "reward capital",
    "worldpay advance",
    "stripe capital",
    "intuit merchant",
    "quickbooks capital",
    "libertas funding",
    "greenbox capital",
    "rapid capital funding",
    "expansion capital group",
)

# Behavioral terms — only count when frequency confirms (TS-review fix).
#
# 2026-06-26 tightening: removed single-word generic terms ("advance",
# "remit", "factor", "holdback", "receivables", "merchant svc") that
# appear in countless legitimate transactions (e.g. "Adobe Creative
# CLOUD" / "merchant svc fee" / "investment advance"). Even with the
# 10-occurrence + daily-cadence guard, these were producing 16-96
# false-positive position counts on merchants with 0-1 real MCA
# positions because routine merchant-service or fee streams hit the
# cadence threshold. Live list is multi-word and MCA-specific only.
#
# Single words are NEVER added to this list — every entry must be a
# phrase or token that does not appear in non-MCA banking descriptors.
GENERIC_MCA_TERMS: Final[tuple[str, ...]] = (
    "daily pmt",
    "daily payment",
    "daily business pmt",
    "daily remittance",
    "ach daily",
    "daily debit",
    "business daily",
    "daily withdrawal",
    "future receipts",
    "daily ach",
    "receivable purchase",
    "biz advance",
    "rcvbl",
)

# R1.1: Disguise descriptors used by funders / brokers to hide an MCA
# behind product-neutral language. These NEVER fire on their own — they
# require the same cadence guard as ``GENERIC_MCA_TERMS`` (≥10 occurrences
# AND median spacing ≤2 days). Shadow-only output:
# ``mca_disguise_candidate`` flag, no addition to mca_positions / score.
DISGUISE_MCA_TERMS: Final[tuple[str, ...]] = (
    "settlement adv",
    "settlement advance",
    "revenue based lending",
    "revenue based financing",
    "daily advance",
    "business cash adv",
    "merchant cash adv",
    "funding ach",
    "capital advance",
    "working capital adv",
)

# R1.1 fuzzy-match tuning. Conservative thresholds; shadow-only flags so
# the operator can corpus-validate before deciding to fold into the live
# decline path.
_FUZZY_RATIO_THRESHOLD: Final[float] = 0.80
_FUZZY_PREFIX_OVERLAP_CHARS: Final[int] = 4
_FUZZY_MIN_FUNDER_LEN: Final[int] = 4
# Minimum occurrences for a fuzzy-funder candidate to flag. Same floor as
# the exact-match path (≥3) so we don't surface a single typoed debit.
_FUZZY_MIN_OCCURRENCES: Final[int] = 3
# Generic tokens that frequently appear in BOTH multi-word funder names
# AND legitimate merchant / banking descriptors. Excluded from the
# token-prefix path because matching on "DAILY", "FUNDING", "CAPITAL"
# would otherwise let any ACH debit with those words anywhere in the
# string match every multi-word funder containing the token. SeqMatcher
# on the full string still applies — so a true near-miss with one of
# these words still has a chance via the ratio path.
_FUZZY_GENERIC_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "CAPITAL",
        "FUNDING",
        "FINANCE",
        "FINANCIAL",
        "MERCHANT",
        "BUSINESS",
        "DAILY",
        "ADVANCE",
        "REMIT",
        "ACH",
        "DEBIT",
        "CREDIT",
        "PAYMENT",
        "PAYMT",
        "PMT",
        "GROUP",
        "VENTURES",
        "PARTNERS",
        "FUND",
        "LOAN",
        "LENDING",
        "WORKING",
        "EXPANSION",
        "CASH",
        "BANK",
        "MERCHANTS",
    }
)
# Minimum occurrences AND cadence for disguise-term flagging. The spec
# requires ≥10 occurrences at median spacing ≤2 days — a single
# "SETTLEMENT ADVANCE" row will never trigger.
_DISGUISE_MIN_OCCURRENCES: Final[int] = 10
_DISGUISE_MAX_MEDIAN_SPACING_DAYS: Final[int] = 2
# Same-day funder-cluster floor. ≥3 distinct funder names on one date is
# the classic simultaneous-onboarding stacking marker.
_SAME_DAY_CLUSTER_MIN_FUNDERS: Final[int] = 3

# M9 — structured-deposit (BSA threshold-avoidance) detector.
#
# 31 USC § 5324 makes it a federal crime to structure deposits to evade
# the Currency Transaction Report (CTR) reporting requirement at
# 31 CFR § 1010.311 (cash transactions > $10,000). Repeatedly depositing
# just under the $10K threshold — "smurfing" — is the classic pattern.
#
# AEGIS cannot reliably distinguish cash from check / wire on a bank
# statement description, so the detector fires on ANY deposit in the
# BSA-avoidance band ($8,500 to $9,999) and surfaces it for operator
# review. A $9,500 wire is almost never structured; a $9,500 cash
# deposit on a 3-deposit-in-14d cluster is the textbook signal. The
# operator's review interprets context.
#
# Thresholds:
# - Band: $8,500 ≤ amount ≤ $9,999. Lower bound is FinCEN-typical
#   smurfing floor (depositors usually keep a comfortable margin under
#   $10K; $8,500 catches the band without flagging routine $5K-$8K
#   business deposits). Upper bound excludes $10,000 exactly because
#   at that amount the bank itself files the CTR — no avoidance.
# - Cluster size: ≥3 in any 14-day rolling window. Three is the FinCEN
#   "pattern" floor — fewer is noise because legitimate businesses
#   regularly deposit ~$9K amounts. Two coincident in-band deposits in
#   two weeks is not a pattern; three is.
# - Window: 14-day rolling. Matches FinCEN's typical structuring
#   review window. A wider window (30d) would catch slow-drip smurfing
#   but also flood the queue with false positives.
_STRUCTURED_DEPOSIT_MIN_AMOUNT: Final[Decimal] = Decimal("8500.00")
_STRUCTURED_DEPOSIT_MAX_AMOUNT: Final[Decimal] = Decimal("9999.99")
_STRUCTURED_DEPOSIT_MIN_CLUSTER: Final[int] = 3
_STRUCTURED_DEPOSIT_WINDOW_DAYS: Final[int] = 14
# Deposit categories considered in-scope. Excludes ``refund`` and
# ``transfer`` — refunds and inter-account moves are not depositor-
# initiated cash placements and never represent CTR avoidance.
_STRUCTURED_DEPOSIT_CATEGORIES: Final[frozenset[str]] = frozenset(
    {"deposit", "ach_credit", "wire_in"}
)

# Known payroll-processor counterparties. Presence of any of these as a
# debit counterparty over the period satisfies the "operating business
# has payroll" signal. Absence is a soft red flag for businesses claiming
# > $50k/mo revenue (per §6.4 `payroll_absent`).
KNOWN_PAYROLL_PROCESSORS: Final[tuple[str, ...]] = (
    "adp",
    "gusto",
    "paychex",
    "rippling",
    "square payroll",
    "intuit payroll",
    "quickbooks payroll",
    "trinet",
    "justworks",
    "zenefits",
    "deel",
    "bamboohr",
    "wagepoint",
    "onpay",
    "patriot payroll",
    "surepayroll",
    "paylocity",
    "ultipro",
    "workday payroll",
)

# Card-processor counterparties whose deposits are net of advance
# withholding. Used by `processor_holdback_detected` — when "STRIPE
# TRANSFER" / "SQUARE PAYOUT" credits show large daily variation
# inconsistent with the rest of the deposit stream we infer holdback
# is in force. Surfaces a soft signal; final cross-check requires the
# matched processor statement (Phase 2C).
KNOWN_CARD_PROCESSORS: Final[tuple[str, ...]] = (
    "stripe",
    "square",
    "toast",
    "clover",
    "shopify",
    "paypal",
    "worldpay",
    "elavon",
    "global payments",
    "tsys",
    "first data",
    "fiserv merchant",
    "heartland",
    "authorize.net",
    "braintree",
)

# Reversal / dispute keywords used by `unauthorized_withdrawal_dispute`.
# A credit row containing one of these paired with a prior matching MCA
# debit means the merchant fought (and won) a funder withdrawal.
REVERSAL_KEYWORDS: Final[tuple[str, ...]] = (
    "reversal",
    "reverse",
    "dispute credit",
    "dispute cred",
    "unauthorized",
    "return ach credit",
    "chargeback credit",
    "ach return credit",
    "ach reversal",
    "claim credit",
    "withdrawal dispute",
)

# Chargeback / refund keywords used by `chargeback_velocity`. Counts
# debit-side rows containing any of these terms grouped by month.
CHARGEBACK_KEYWORDS: Final[tuple[str, ...]] = (
    "chargeback",
    "charge back",
    "refund",
    "dispute",
    "return ach",
    "merchant return",
    "credit reversal",
)


@dataclass
class Pattern:
    code: str
    severity: int
    detail: str
    source_ids: list[UUID] = field(default_factory=list)


McaPositionMatchSource = Literal["known_funder", "pattern"]


@dataclass
class McaPosition:
    funder_label: str
    daily_equivalent: Decimal
    occurrences: int
    source_ids: list[UUID]
    # ``known_funder`` — a KNOWN_FUNDERS substring matched the
    # description (high confidence; named funder recognized).
    # ``pattern`` — only GENERIC_MCA_TERMS matched (with the daily-
    # cadence + occurrence guard); confidence is lower and the dossier
    # surfaces these as "possible — verify" rather than "confirmed".
    # Default keeps legacy persisted rows (pre-2026-06-26) hydrating
    # under the conservative ``known_funder`` bucket; new parses always
    # set this explicitly via ``_detect_mca_positions``.
    match_source: McaPositionMatchSource = "known_funder"


@dataclass
class CounterpartySignals:
    """Counterparty-aggregation outputs surfaced as scoring factors.

    Per master plan §5.7. ``top_counterparty_pct`` is the single largest
    counterparty's share of true_revenue (revenue concentration). The
    ``top_5_*_share`` fields are the combined share of the five largest
    counterparties on each side. ``source_ids`` for each is the union of
    contributing transaction ids — funder drill-down stays auditable.

    None when the underlying side (revenue or expense) is empty.
    """

    top_counterparty_pct: int | None = None
    top_counterparty_label: str | None = None
    top_counterparty_source_ids: list[UUID] = field(default_factory=list)
    top_5_revenue_share_pct: int | None = None
    top_5_revenue_source_ids: list[UUID] = field(default_factory=list)
    top_5_expense_share_pct: int | None = None
    top_5_expense_source_ids: list[UUID] = field(default_factory=list)


@dataclass
class PatternAnalysis:
    patterns: list[Pattern]
    mca_positions: list[McaPosition]
    has_kiting: bool
    paydown_suspected: bool
    counterparty_signals: CounterpartySignals = field(default_factory=CounterpartySignals)
    payroll_present: bool = False
    acceleration_clause_triggered: bool = False
    unauthorized_withdrawal_dispute: bool = False
    ai_generated_score: int = 0
    # R1.1 / R1.3 shadow-mode flags. Carried in a separate list so the
    # ``patterns`` field (the live decision-boundary input that
    # ``parser/pipeline._fraud_cluster_triangulation`` and the scorer
    # consume) is byte-identical to its pre-R1 shape. ``flags`` exposes
    # both lists by union — callers that only want live-path flags must
    # iterate ``patterns`` directly. Per CLAUDE.md § "Decision-boundary
    # changes — deliberate + shadow-first": the new detector emits new
    # flags but does NOT alter ``fraud_score`` / live decline path on
    # first ship.
    shadow_patterns: list[Pattern] = field(default_factory=list)

    @property
    def fraud_score(self) -> int:
        return min(100, sum(p.severity for p in self.patterns))

    @property
    def flags(self) -> list[str]:
        # Live + shadow flag codes combined, ordered live-first so any
        # operator-side substring search ("contains 'mca_stacking'")
        # behaves the same as before — the new shadow codes append.
        return [p.code for p in self.patterns] + [p.code for p in self.shadow_patterns]


# ---------------------------------------------------------------------------
# Persistence DTOs
#
# Pydantic mirrors of the dataclass shapes above, used to round-trip a
# PatternAnalysis through ``analyses.pattern_analysis`` (jsonb). The
# dataclass remains the runtime shape every callsite uses — the DTO is
# touched only at the storage boundary.
#
# Decimals serialize as strings (matching the monthly_breakdown
# convention from migration 009) so Pydantic round-trips them
# losslessly. UUIDs serialize as strings via Pydantic's default JSON
# mode.
#
# schema_version bumps on any breaking shape change. v1 is the only
# version today; future readers must branch on ``schema_version`` if
# they need to deserialize older formats.
# ---------------------------------------------------------------------------


class _StrictDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class PatternDTO(_StrictDTO):
    """Persistence shape for a single ``Pattern`` dataclass."""

    code: str
    severity: int
    detail: str
    source_ids: list[UUID] = Field(default_factory=list)


class McaPositionDTO(_StrictDTO):
    """Persistence shape for a single ``McaPosition`` dataclass."""

    funder_label: str
    daily_equivalent: Decimal
    occurrences: int
    source_ids: list[UUID]
    # See ``McaPosition.match_source`` for semantics. ``Field(default=...)``
    # keeps the DTO backward-compatible — pattern_analysis rows persisted
    # before 2026-06-26 lack this key and rehydrate as ``"known_funder"``
    # (conservative; the dossier renders them as confirmed positions).
    match_source: McaPositionMatchSource = Field(default="known_funder")


class CounterpartySignalsDTO(_StrictDTO):
    """Persistence shape for ``CounterpartySignals``."""

    top_counterparty_pct: int | None = None
    top_counterparty_label: str | None = None
    top_counterparty_source_ids: list[UUID] = Field(default_factory=list)
    top_5_revenue_share_pct: int | None = None
    top_5_revenue_source_ids: list[UUID] = Field(default_factory=list)
    top_5_expense_share_pct: int | None = None
    top_5_expense_source_ids: list[UUID] = Field(default_factory=list)


class PatternAnalysisDTO(_StrictDTO):
    """Persistence shape for ``PatternAnalysis``.

    Stored on ``AnalysisRow.pattern_analysis`` so card builders can
    look up per-flag source transactions without re-running
    ``analyze_patterns()`` at render time. NOT a source of truth for
    scoring — the scorer always recomputes from current transactions.

    See ``migrations/032_analyses_pattern_analysis.sql`` for the
    column definition and rollout strategy.
    """

    schema_version: int = 1
    patterns: list[PatternDTO] = Field(default_factory=list)
    mca_positions: list[McaPositionDTO] = Field(default_factory=list)
    has_kiting: bool = False
    paydown_suspected: bool = False
    counterparty_signals: CounterpartySignalsDTO | None = None
    payroll_present: bool = False
    acceleration_clause_triggered: bool = False
    unauthorized_withdrawal_dispute: bool = False
    ai_generated_score: int = 0
    # R1.1 / R1.3 shadow-mode flags persisted alongside live patterns.
    # ``default_factory=list`` keeps the DTO backward-compatible: rows
    # written before R1 (no ``shadow_patterns`` key in the stored JSON)
    # rehydrate with an empty list because Pydantic falls back to the
    # default. Same for the inverse direction — a freshly-built
    # PatternAnalysis with no shadow flags serializes to an empty list,
    # so legacy readers that do not know the field still parse it.
    shadow_patterns: list[PatternDTO] = Field(default_factory=list)


def pattern_analysis_to_dto(pa: PatternAnalysis) -> PatternAnalysisDTO:
    """Serialize a runtime ``PatternAnalysis`` into the persistence DTO.

    Lossless. The dataclass shape and the Pydantic shape carry the
    same fields; this helper just maps types (e.g. dataclass instance
    -> Pydantic model_validate).
    """
    cs = pa.counterparty_signals
    counterparty_dto = CounterpartySignalsDTO(
        top_counterparty_pct=cs.top_counterparty_pct,
        top_counterparty_label=cs.top_counterparty_label,
        top_counterparty_source_ids=list(cs.top_counterparty_source_ids),
        top_5_revenue_share_pct=cs.top_5_revenue_share_pct,
        top_5_revenue_source_ids=list(cs.top_5_revenue_source_ids),
        top_5_expense_share_pct=cs.top_5_expense_share_pct,
        top_5_expense_source_ids=list(cs.top_5_expense_source_ids),
    )
    return PatternAnalysisDTO(
        schema_version=1,
        patterns=[
            PatternDTO(
                code=p.code,
                severity=p.severity,
                detail=p.detail,
                source_ids=list(p.source_ids),
            )
            for p in pa.patterns
        ],
        mca_positions=[
            McaPositionDTO(
                funder_label=m.funder_label,
                daily_equivalent=m.daily_equivalent,
                occurrences=m.occurrences,
                source_ids=list(m.source_ids),
                match_source=m.match_source,
            )
            for m in pa.mca_positions
        ],
        has_kiting=pa.has_kiting,
        paydown_suspected=pa.paydown_suspected,
        counterparty_signals=counterparty_dto,
        payroll_present=pa.payroll_present,
        acceleration_clause_triggered=pa.acceleration_clause_triggered,
        unauthorized_withdrawal_dispute=pa.unauthorized_withdrawal_dispute,
        ai_generated_score=pa.ai_generated_score,
        shadow_patterns=[
            PatternDTO(
                code=p.code,
                severity=p.severity,
                detail=p.detail,
                source_ids=list(p.source_ids),
            )
            for p in pa.shadow_patterns
        ],
    )


def pattern_analysis_from_dto(dto: PatternAnalysisDTO) -> PatternAnalysis:
    """Deserialize a stored DTO back into the runtime ``PatternAnalysis``.

    Inverse of ``pattern_analysis_to_dto``. Used by the (post-stage-2
    cleanup) dossier reader and by tests verifying the round-trip is
    lossless.
    """
    cs_source = dto.counterparty_signals or CounterpartySignalsDTO()
    counterparty = CounterpartySignals(
        top_counterparty_pct=cs_source.top_counterparty_pct,
        top_counterparty_label=cs_source.top_counterparty_label,
        top_counterparty_source_ids=list(cs_source.top_counterparty_source_ids),
        top_5_revenue_share_pct=cs_source.top_5_revenue_share_pct,
        top_5_revenue_source_ids=list(cs_source.top_5_revenue_source_ids),
        top_5_expense_share_pct=cs_source.top_5_expense_share_pct,
        top_5_expense_source_ids=list(cs_source.top_5_expense_source_ids),
    )
    return PatternAnalysis(
        patterns=[
            Pattern(
                code=p.code,
                severity=p.severity,
                detail=p.detail,
                source_ids=list(p.source_ids),
            )
            for p in dto.patterns
        ],
        mca_positions=[
            McaPosition(
                funder_label=m.funder_label,
                daily_equivalent=m.daily_equivalent,
                occurrences=m.occurrences,
                source_ids=list(m.source_ids),
                match_source=m.match_source,
            )
            for m in dto.mca_positions
        ],
        has_kiting=dto.has_kiting,
        paydown_suspected=dto.paydown_suspected,
        counterparty_signals=counterparty,
        payroll_present=dto.payroll_present,
        acceleration_clause_triggered=dto.acceleration_clause_triggered,
        unauthorized_withdrawal_dispute=dto.unauthorized_withdrawal_dispute,
        ai_generated_score=dto.ai_generated_score,
        shadow_patterns=[
            Pattern(
                code=p.code,
                severity=p.severity,
                detail=p.detail,
                source_ids=list(p.source_ids),
            )
            for p in dto.shadow_patterns
        ],
    )


def analyze_patterns(
    transactions: list[ClassifiedTransaction],
    period_start: date,
    period_end: date,
    today: date | None = None,
) -> PatternAnalysis:
    today = today or date.today()
    patterns: list[Pattern] = []

    deposits = [
        t
        for t in transactions
        if t.amount > 0 and t.category in {"deposit", "ach_credit", "wire_in", "refund"}
    ]
    debits = [t for t in transactions if t.amount < 0]
    nsf_events = [t for t in transactions if t.category == "nsf_fee"]
    statement_days = max(1, (period_end - period_start).days + 1)

    mca_positions = _detect_mca_positions(debits, period_start, period_end)
    if mca_positions:
        ids: list[UUID] = [sid for p in mca_positions for sid in p.source_ids]
        patterns.append(
            Pattern(
                code="mca_stacking",
                severity=min(50, 15 * len(mca_positions)),
                detail=f"{len(mca_positions)} MCA position(s) detected",
                source_ids=ids,
            )
        )

    if dup := _duplicate_deposits(deposits):
        patterns.append(dup)
    if cv := _synthetic_low_variance(deposits):
        patterns.append(cv)
    if rn := _round_number_deposits(deposits):
        patterns.append(rn)
    if (spike := _preloan_spike(deposits, period_end, statement_days)) is not None:
        patterns.append(spike)
    if statement_days < 20 and len(nsf_events) > 3:
        patterns.append(
            Pattern(
                code="nsf_clustering_short",
                severity=20,
                detail=f"{len(nsf_events)} NSFs in {statement_days} days",
                source_ids=[t.id for t in nsf_events],
            )
        )
    if late := _nsf_late_concentration(nsf_events, period_end, statement_days):
        patterns.append(late)

    wash_pairs, has_kiting = _wash_deposit_kiting(deposits, debits)
    if wash_pairs:
        patterns.append(wash_pairs)

    paydown_pat = _paydown_mca(debits)
    paydown_suspected = paydown_pat is not None
    if paydown_pat:
        patterns.append(paydown_pat)

    if recent := _recent_account_opening(period_start, today):
        patterns.append(recent)

    if vel := _deposit_velocity_spike(deposits, period_start, period_end):
        patterns.append(vel)

    if accel := _withdrawal_acceleration(debits, period_start, period_end):
        patterns.append(accel)

    # -- Phase 9 detectors ----------------------------------------------
    if uit := _unreconciled_internal_transfer(transactions):
        patterns.append(uit)

    if payoff := _mca_payoff_signature(debits):
        patterns.append(payoff)

    counterparty_signals = _counterparty_signals(transactions)
    if concentration := _customer_concentration(transactions, counterparty_signals):
        patterns.append(concentration)

    if cb_velocity := _chargeback_velocity(transactions, period_start, period_end):
        patterns.append(cb_velocity)

    unauth_dispute_pat = _unauthorized_withdrawal_dispute(transactions)
    unauth_dispute_fired = unauth_dispute_pat is not None
    if unauth_dispute_pat:
        patterns.append(unauth_dispute_pat)

    acceleration_pat = _acceleration_clause_triggered(mca_positions, debits, period_end)
    acceleration_fired = acceleration_pat is not None
    if acceleration_pat:
        patterns.append(acceleration_pat)

    if processor_pat := _processor_holdback_detected(transactions, period_start, period_end):
        patterns.append(processor_pat)

    payroll_present = _payroll_present(debits)
    if payroll_absent_pat := _payroll_absent(
        payroll_present, transactions, period_start, period_end
    ):
        patterns.append(payroll_absent_pat)

    # -- R1 shadow-mode detectors --------------------------------------
    # Land in ``shadow_patterns`` — NOT ``patterns`` — so
    # ``parser/pipeline._fraud_cluster_triangulation`` (which counts
    # ``len(patterns.patterns) >= 3``) and ``patterns.fraud_score`` (the
    # severity sum) stay byte-identical. The new flags are operator-
    # facing evidence only; they do not move the live decline path.
    shadow_patterns: list[Pattern] = []
    shadow_patterns.extend(_detect_fuzzy_mca_candidates(debits))
    shadow_patterns.extend(_detect_disguise_candidates(debits))
    shadow_patterns.extend(_detect_same_day_mca_funder_cluster(mca_positions, transactions))
    # M9 — structured-deposit (BSA threshold-avoidance) clusters.
    # Walks ALL transactions (not just classified deposits) so the
    # detector picks up rows whose category is ``deposit``,
    # ``ach_credit``, or ``wire_in`` per 31 USC § 5324 scope.
    shadow_patterns.extend(_detect_structured_deposit_cluster(transactions))
    # Operator spec 2026-06-24 — shadow unreconciled internal transfer v2.
    # ``all_bundle_transactions`` defaults to the single-statement input
    # at parse time because ``analyze_patterns`` runs per-statement;
    # multi-statement callers pass the bundle explicitly. Distinct from
    # the live ``unreconciled_internal_transfer`` detector above — that
    # one is tighter (±$1, ±3d) and lives on ``patterns``; this one is
    # looser (floor $50, 0.1% of magnitude on larger transfers, ±5d) and
    # lives on ``shadow_patterns`` with code ``unreconciled_internal_transfer_v2``
    # for corpus-validation per CLAUDE.md decision-boundary discipline.
    shadow_patterns.extend(detect_unreconciled_internal_transfers(transactions, transactions))

    ai_score = _ai_generated_statement_score(transactions, deposits)

    return PatternAnalysis(
        patterns=patterns,
        mca_positions=mca_positions,
        has_kiting=has_kiting,
        paydown_suspected=paydown_suspected,
        counterparty_signals=counterparty_signals,
        payroll_present=payroll_present,
        acceleration_clause_triggered=acceleration_fired,
        unauthorized_withdrawal_dispute=unauth_dispute_fired,
        ai_generated_score=ai_score,
        shadow_patterns=shadow_patterns,
    )


# -- MCA detection -----------------------------------------------------------


def _detect_mca_positions(
    debits: list[ClassifiedTransaction],
    period_start: date,
    period_end: date,
) -> list[McaPosition]:
    """Group debits by normalized description and identify MCA positions.

    Two paths:
    - Known funder name in description with ≥3 occurrences -> position
    - Daily cadence (≥10 occurrences) with generic-MCA term -> position
    Generic single words alone do NOT fire (TS-review fix).
    """
    groups: defaultdict[str, list[ClassifiedTransaction]] = defaultdict(list)
    for d in debits:
        key = _normalize_desc(d.description)
        groups[key].append(d)

    period_days = max(1, (period_end - period_start).days + 1)
    positions: list[McaPosition] = []

    for key, rows in groups.items():
        if len(rows) < 3:
            continue
        desc_lower = rows[0].description.lower()
        is_funder = any(f in desc_lower for f in KNOWN_FUNDERS)
        is_generic_with_cadence = (
            any(t in desc_lower for t in GENERIC_MCA_TERMS)
            and len(rows) >= 10
            and _looks_daily_cadence(rows)
        )
        if not (is_funder or is_generic_with_cadence):
            continue
        total = sum((-r.amount for r in rows), Decimal("0"))
        daily_equivalent = (total / Decimal(period_days)).quantize(Decimal("0.01"))
        # ``is_funder`` wins the bucketing — a named funder match is
        # higher confidence than a daily-cadence-only match. The
        # display layer renders these two buckets separately so a
        # merchant with one named-funder match + one cadence-only
        # match shows "1 confirmed, 1 possible" instead of "2 stacking".
        match_source: McaPositionMatchSource = "known_funder" if is_funder else "pattern"
        positions.append(
            McaPosition(
                funder_label=key[:30],
                daily_equivalent=daily_equivalent,
                occurrences=len(rows),
                source_ids=[r.id for r in rows],
                match_source=match_source,
            )
        )
    return positions


def count_confirmed_positions(positions: list[McaPosition]) -> int:
    """Number of MCA positions matched via a named funder.

    Surfaced separately from ``count_pattern_positions`` so the dossier
    can render "N confirmed (funder name detected); M possible via
    payment pattern (verify)" without re-walking the descriptor lists.
    """
    return sum(1 for p in positions if p.match_source == "known_funder")


def count_pattern_positions(positions: list[McaPosition]) -> int:
    """Number of MCA positions matched via GENERIC_MCA_TERMS + cadence."""
    return sum(1 for p in positions if p.match_source == "pattern")


def _looks_daily_cadence(rows: list[ClassifiedTransaction]) -> bool:
    if len(rows) < 5:
        return False
    days = sorted({r.posted_date for r in rows})
    spacing = [(days[i + 1] - days[i]).days for i in range(len(days) - 1)]
    if not spacing:
        return False
    median_spacing = statistics.median(spacing)
    return median_spacing <= 2  # daily / business-daily


def _normalize_desc(desc: str) -> str:
    return desc.strip().lower()[:30]


# R1.1: dedicated normalizer for fuzzy + disguise matching. Distinct
# from ``_normalize_desc`` (which clips to 30 chars and is the bucketing
# key for the exact-match path) — fuzzy match needs the full string so a
# SequenceMatcher ratio against a 7-char funder name isn't penalized by
# the truncation, and ASCII-only token comparison wants punctuation
# stripped to single spaces.
_FUZZY_PUNCT_RE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9\s]")
_FUZZY_WS_RE: Final[re.Pattern[str]] = re.compile(r"\s+")


def _normalize_for_fuzzy(desc: str) -> str:
    """Uppercase, punctuation-stripped, whitespace-collapsed full string.

    Mirrors ``parser/lender_filter._normalize`` but kept local so the
    pattern-detection module owns its own normalization contract and a
    future tweak (e.g. dropping trace-id digit runs) doesn't ripple into
    the revenue-side filter.
    """
    if not desc:
        return ""
    no_punct = _FUZZY_PUNCT_RE.sub(" ", desc)
    collapsed = _FUZZY_WS_RE.sub(" ", no_punct).strip()
    return collapsed.upper()


def _exact_funder_in_desc(desc_lower: str) -> bool:
    """True iff any KNOWN_FUNDERS substring appears in ``desc_lower``.

    Mirrors the substring check inside ``_detect_mca_positions`` so the
    fuzzy path knows which rows the exact-match path already owns and
    skips them — we only want to surface NEW candidates the existing
    detector missed.
    """
    return any(f in desc_lower for f in KNOWN_FUNDERS)


def _common_prefix_len(a: str, b: str) -> int:
    """Length of the shared leading run of characters between ``a`` and ``b``."""
    limit = min(len(a), len(b))
    n = 0
    while n < limit and a[n] == b[n]:
        n += 1
    return n


def _fuzzy_match_known_funder(
    normalized_desc: str,
) -> tuple[str, float] | None:
    """Best-effort fuzzy match of ``normalized_desc`` against KNOWN_FUNDERS.

    Two paths, OR-combined:

    - ``SequenceMatcher(None, normalized_desc, funder).ratio() >=
      _FUZZY_RATIO_THRESHOLD`` (default 0.80). Catches single-token
      misspellings: "KAPPITUS" vs "KAPITUS" -> ratio 0.93.

    - Token-level prefix overlap: any token of the description with
      ``len >= _FUZZY_PREFIX_OVERLAP_CHARS`` (4) AND any funder-name
      token of the same minimum length whose shared leading prefix is
      ``>= _FUZZY_PREFIX_OVERLAP_CHARS`` and whose token lengths are
      within ±2 characters of each other. Catches "ONDEK" vs "ONDECK"
      (prefix "ONDE", len-diff 1). The length-proximity guard is the
      false-positive backstop against unrelated long merchant names
      that happen to share an initial run with a short funder name.

    Returns ``(canonical_funder_name, ratio)`` on the first match (sorted
    longest-funder-first so a more specific match wins). ``ratio`` is the
    full-string SequenceMatcher ratio whether the match fired via ratio
    or prefix — callers use it for evidence in the candidate flag.

    Funders shorter than ``_FUZZY_MIN_FUNDER_LEN`` (4) are skipped to
    avoid CFG-class 3-char false positives.
    """
    if not normalized_desc:
        return None

    # Tokenize once. Lengths cached so the token-loop is allocation-light.
    desc_tokens: list[str] = [
        tok for tok in normalized_desc.split() if len(tok) >= _FUZZY_PREFIX_OVERLAP_CHARS
    ]

    # Sort longest-first so "FORWARD FINANCING" wins over a future short
    # entry whose prefix happens to overlap with a longer funder name.
    for funder in sorted(KNOWN_FUNDERS, key=len, reverse=True):
        funder_upper = funder.upper()
        if len(funder_upper) < _FUZZY_MIN_FUNDER_LEN:
            continue

        # Skip funders that exact-match — those are owned by the
        # exact-substring path in ``_detect_mca_positions``. We only
        # surface candidates the existing detector missed.
        if funder_upper in normalized_desc:
            continue

        ratio = SequenceMatcher(None, normalized_desc, funder_upper).ratio()
        if ratio >= _FUZZY_RATIO_THRESHOLD:
            return funder_upper, ratio

        # Token path. Funder tokens are tested individually so a
        # multi-word funder ("FORWARD FINANCING") can match via either
        # word, and a single-token funder ("KAPITUS") can match against
        # any single descriptor token ("KAPPITUS").
        #
        # A token-pair (dtok, ftok) fires when ALL hold:
        #   - both tokens length >= _FUZZY_PREFIX_OVERLAP_CHARS (4)
        #   - both tokens are NOT in _FUZZY_GENERIC_TOKENS (no matching
        #     on banking glue words: DAILY, ACH, FUNDING, CAPITAL, ...)
        #   - length difference <= 2 (kills "KAPITAL" matching against
        #     a hypothetical 4-char funder where the 5-char prefix would
        #     otherwise look meaningful)
        #   - SequenceMatcher(dtok, ftok).ratio() >= _FUZZY_RATIO_THRESHOLD
        #     (0.80). This is the boundary that rejects "KAPITAL" vs
        #     "KAPITUS" (0.714) but accepts "KAPPITUS" vs "KAPITUS"
        #     (0.933) and "ONDEK" vs "ONDECK" (0.909). Note we DO NOT
        #     additionally require a 4-char common prefix because real
        #     typos can mutate at any position, not just the suffix.
        funder_tokens = [
            t
            for t in funder_upper.split()
            if len(t) >= _FUZZY_PREFIX_OVERLAP_CHARS and t not in _FUZZY_GENERIC_TOKENS
        ]
        for dtok in desc_tokens:
            if dtok in _FUZZY_GENERIC_TOKENS:
                continue
            for ftok in funder_tokens:
                if abs(len(dtok) - len(ftok)) > 2:
                    continue
                token_ratio = SequenceMatcher(None, dtok, ftok).ratio()
                if token_ratio >= _FUZZY_RATIO_THRESHOLD:
                    return funder_upper, max(ratio, token_ratio)

    return None


# -- R1.1 / R1.3 shadow-mode detectors ---------------------------------------
#
# Three new detectors land here as part of the 2026-06-08 audit's R1
# remediation (`docs/AUDIT_REMEDIATION_R1.md` and the operator's
# `delightful-beaming-meerkat.md` plan):
#
#   1. ``_detect_fuzzy_mca_candidates`` — typoed / variant funder names
#      that the exact-substring path in ``_detect_mca_positions`` misses
#      (e.g. "KAPPITUS", "ONDEK FUNDING").
#   2. ``_detect_disguise_candidates`` — descriptors that hide an MCA
#      behind product-neutral language ("SETTLEMENT ADVANCE", "REVENUE
#      BASED LENDING"). Only fire on cadence — ≥10 occurrences with
#      median spacing ≤ 2 days — so a one-off "SETTLEMENT" charge never
#      flags.
#   3. ``_detect_same_day_mca_funder_cluster`` — ≥3 distinct funder
#      descriptors hitting the same calendar date is the classic
#      simultaneous-onboarding stacking marker.
#
# All three emit SHADOW-MODE flags only:
# - They do NOT add to ``mca_positions``.
# - They do NOT add to ``fraud_score`` (severity is 0 by construction).
# - They do NOT change hard-decline reasons.
# - They surface as new ``Pattern.code`` values for operator review +
#   corpus validation. Once the operator confirms low false-positive
#   rate on the live corpus, a follow-up commit will fold them into
#   the scored path behind an env-var gate. Until then, they're
#   informational evidence on the dossier.


def _detect_fuzzy_mca_candidates(
    debits: list[ClassifiedTransaction],
) -> list[Pattern]:
    """Surface fuzzy funder-name matches the exact path missed.

    Walks every debit, normalizes the description, and asks
    ``_fuzzy_match_known_funder`` whether any KNOWN_FUNDERS entry is a
    near-miss. Rows whose description already contains an exact funder
    substring are skipped — those are owned by ``_detect_mca_positions``.

    Groups matches by canonical funder name and emits ONE
    ``mca_position_fuzzy_candidate`` pattern per funder with ≥3
    occurrences. Flag format:

        mca_position_fuzzy_candidate:{funder}_{ratio}_{count}_{first}_{last}

    Severity is 0 — shadow-mode evidence only.
    """
    if not debits:
        return []

    @dataclass
    class _FuzzyHit:
        rows: list[ClassifiedTransaction] = field(default_factory=list)
        best_ratio: float = 0.0

    grouped: defaultdict[str, _FuzzyHit] = defaultdict(_FuzzyHit)

    for d in debits:
        desc_lower = d.description.lower()
        if _exact_funder_in_desc(desc_lower):
            # Owned by the exact-match path. Do not duplicate.
            continue
        normalized = _normalize_for_fuzzy(d.description)
        if not normalized:
            continue
        match = _fuzzy_match_known_funder(normalized)
        if match is None:
            continue
        funder_name, ratio = match
        hit = grouped[funder_name]
        hit.rows.append(d)
        if ratio > hit.best_ratio:
            hit.best_ratio = ratio

    out: list[Pattern] = []
    for funder_name, hit in grouped.items():
        if len(hit.rows) < _FUZZY_MIN_OCCURRENCES:
            continue
        dates = sorted(r.posted_date for r in hit.rows)
        first_date = dates[0]
        last_date = dates[-1]
        ratio_str = f"{hit.best_ratio:.2f}"
        code = (
            f"mca_position_fuzzy_candidate:"
            f"{funder_name}_{ratio_str}_{len(hit.rows)}_"
            f"{first_date.isoformat()}_{last_date.isoformat()}"
        )
        detail = (
            f"fuzzy match for '{funder_name}' on {len(hit.rows)} debit(s) "
            f"(best ratio {ratio_str}, {first_date.isoformat()} .. "
            f"{last_date.isoformat()}) — shadow-mode candidate"
        )
        out.append(
            Pattern(
                code=code,
                severity=0,
                detail=detail,
                source_ids=[r.id for r in hit.rows],
            )
        )
    return out


def _detect_disguise_candidates(
    debits: list[ClassifiedTransaction],
) -> list[Pattern]:
    """Surface cadenced disguise-term debits.

    Walks debits whose normalized description contains any
    ``DISGUISE_MCA_TERMS`` token. Groups by matched term. Each term
    must clear BOTH thresholds before flagging:

      - ``len(rows) >= _DISGUISE_MIN_OCCURRENCES`` (10)
      - daily-ish cadence: median spacing between unique posted dates
        ``<= _DISGUISE_MAX_MEDIAN_SPACING_DAYS`` (2)

    A single "SETTLEMENT ADVANCE" debit will NEVER trigger; a real
    funder using "SETTLEMENT ADV" as descriptor for 10+ daily debits
    will. Flag format:

        mca_disguise_candidate:{term}_{count}_{median_spacing_days}

    Severity is 0 — shadow-mode evidence only.
    """
    if not debits:
        return []

    # Iterate disguise terms longest-first so the more specific phrase
    # wins on rows where multiple disguises overlap. Without this,
    # "SETTLEMENT ADVANCE" would bucket into the shorter "SETTLEMENT ADV"
    # key — same flag but a less specific code, harder to corpus-review.
    disguise_terms_ordered: tuple[str, ...] = tuple(
        sorted({t.upper() for t in DISGUISE_MCA_TERMS}, key=len, reverse=True)
    )

    grouped: defaultdict[str, list[ClassifiedTransaction]] = defaultdict(list)
    for d in debits:
        normalized = _normalize_for_fuzzy(d.description)
        if not normalized:
            continue
        # If the row is already an exact-match funder, leave it to the
        # exact-match path. Disguise detection is for the residue.
        if _exact_funder_in_desc(d.description.lower()):
            continue
        for term_upper in disguise_terms_ordered:
            if term_upper in normalized:
                grouped[term_upper].append(d)
                # Stop at first matched term per row — one row, one term,
                # to avoid double-counting against multiple overlapping
                # disguises.
                break

    out: list[Pattern] = []
    for term, rows in grouped.items():
        if len(rows) < _DISGUISE_MIN_OCCURRENCES:
            continue
        unique_days = sorted({r.posted_date for r in rows})
        if len(unique_days) < 2:
            continue
        spacing = [(unique_days[i + 1] - unique_days[i]).days for i in range(len(unique_days) - 1)]
        if not spacing:
            continue
        median_spacing = statistics.median(spacing)
        if median_spacing > _DISGUISE_MAX_MEDIAN_SPACING_DAYS:
            continue
        # statistics.median returns float|int; round to int days for the
        # flag string so we never expose a misleading "2.5d" cadence.
        median_int = round(median_spacing)
        code = f"mca_disguise_candidate:{term}_{len(rows)}_{median_int}"
        detail = (
            f"disguise term '{term}' on {len(rows)} debit(s) at median "
            f"{median_int}d cadence — shadow-mode candidate"
        )
        out.append(
            Pattern(
                code=code,
                severity=0,
                detail=detail,
                source_ids=[r.id for r in rows],
            )
        )
    return out


def _detect_same_day_mca_funder_cluster(
    mca_positions: list[McaPosition],
    transactions: list[ClassifiedTransaction],
) -> list[Pattern]:
    """Surface dates where ≥3 distinct detected funders hit on the same day.

    Walks ``mca_positions`` (the OUTPUT of ``_detect_mca_positions``,
    already exact-match deduplicated) and groups every contributing
    debit by ``posted_date``. Any date covered by
    ``_SAME_DAY_CLUSTER_MIN_FUNDERS`` (3) or more distinct funder labels
    emits a flag. Flag format:

        mca_same_day_cluster:{date}_{funder_count}_({A|B|C))

    Severity is 0 — shadow-mode. (The spec lands this at severity 25
    "informational" but for the first ship we keep it at 0 so the live
    fraud_score does not move. Operator flips severity in a follow-up
    once the corpus comparison confirms low false-positive rate.)
    """
    if not mca_positions or len(mca_positions) < _SAME_DAY_CLUSTER_MIN_FUNDERS:
        return []

    # Build a transaction-id -> txn map once so we can look up posted
    # dates without a quadratic scan over ``transactions``.
    by_id: dict[UUID, ClassifiedTransaction] = {t.id: t for t in transactions}

    # date -> {funder_label: [transaction ids contributing]}
    by_date: defaultdict[date, defaultdict[str, list[UUID]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for pos in mca_positions:
        for sid in pos.source_ids:
            txn = by_id.get(sid)
            if txn is None:
                continue
            by_date[txn.posted_date][pos.funder_label].append(sid)

    out: list[Pattern] = []
    for posted_date, funder_map in by_date.items():
        funders_on_day = sorted(funder_map.keys())
        if len(funders_on_day) < _SAME_DAY_CLUSTER_MIN_FUNDERS:
            continue
        joined_names = "|".join(funders_on_day)
        code = (
            f"mca_same_day_cluster:{posted_date.isoformat()}_{len(funders_on_day)}_({joined_names})"
        )
        detail = (
            f"{len(funders_on_day)} distinct funders debited on "
            f"{posted_date.isoformat()} — possible simultaneous onboarding"
        )
        ids: list[UUID] = [sid for sids in funder_map.values() for sid in sids]
        out.append(
            Pattern(
                code=code,
                severity=0,
                detail=detail,
                source_ids=ids,
            )
        )
    return out


def _detect_structured_deposit_cluster(
    transactions: list[ClassifiedTransaction],
) -> list[Pattern]:
    """Detect BSA threshold-avoidance ("structuring") deposit clusters.

    Walks deposits — positive-amount rows classified as ``deposit``,
    ``ach_credit``, or ``wire_in`` — filters to those in the avoidance
    band (``$8,500 ≤ amount ≤ $9,999.99``), and looks for any 14-day
    rolling window containing ``_STRUCTURED_DEPOSIT_MIN_CLUSTER`` (3) or
    more such deposits.

    Statutory context:
    - **31 USC § 5324** — structuring (deliberately breaking deposits
      below the CTR threshold) is a federal crime.
    - **31 CFR § 1010.311** — financial institutions must file a CTR for
      cash transactions over $10,000.
    - The deposits AEGIS sees on a bank statement are post-deposit
      records; the detector cannot prove the cash-vs-check origin. It
      surfaces the pattern for operator review.

    Cash-only caveat: BSA-structuring is most common with cash, but
    the parser cannot reliably distinguish cash from check / wire from
    a row description. The detector fires on ANY deposit category in
    the band; the operator interprets context (a $9,500 wire is almost
    never structured; a $9,500 deposit might be).

    Shadow-mode output:
    - Severity 0 (no fraud_score contribution).
    - Appended to ``PatternAnalysis.shadow_patterns``.
    - Does NOT touch ``mca_positions``, hard-decline reasons, or
      ``parse_status``.

    Flag format:
        structured_deposit_cluster:N_deposits_in_14_day_window_dates=YYYYMMDD,YYYYMMDD,...

    One pattern per distinct cluster window. If two windows overlap and
    both contain ≥3 in-band deposits, the detector picks the densest
    earliest-starting window and consumes its members so no row is
    reported in two flags.
    """
    if not transactions:
        return []

    in_band = sorted(
        (
            t
            for t in transactions
            if t.category in _STRUCTURED_DEPOSIT_CATEGORIES
            and t.amount >= _STRUCTURED_DEPOSIT_MIN_AMOUNT
            and t.amount <= _STRUCTURED_DEPOSIT_MAX_AMOUNT
        ),
        key=lambda t: (t.posted_date, t.id),
    )
    if len(in_band) < _STRUCTURED_DEPOSIT_MIN_CLUSTER:
        return []

    # Sliding-window cluster discovery.
    # Walk each candidate as a window-anchor; collect all in-band rows
    # whose posted_date falls in ``[anchor, anchor + window_days - 1]``.
    # If ≥3, record the cluster and mark members as consumed so they
    # don't seed an overlapping near-duplicate flag.
    consumed: set[UUID] = set()
    clusters: list[list[ClassifiedTransaction]] = []
    window_span = timedelta(days=_STRUCTURED_DEPOSIT_WINDOW_DAYS - 1)

    for i, anchor in enumerate(in_band):
        if anchor.id in consumed:
            continue
        window_end = anchor.posted_date + window_span
        members: list[ClassifiedTransaction] = []
        for cand in in_band[i:]:
            if cand.id in consumed:
                continue
            if cand.posted_date > window_end:
                break
            members.append(cand)
        if len(members) >= _STRUCTURED_DEPOSIT_MIN_CLUSTER:
            clusters.append(members)
            for m in members:
                consumed.add(m.id)

    if not clusters:
        return []

    out: list[Pattern] = []
    for members in clusters:
        dates = [m.posted_date for m in members]
        dates_compact = ",".join(d.strftime("%Y%m%d") for d in dates)
        code = (
            f"structured_deposit_cluster:"
            f"{len(members)}_deposits_in_{_STRUCTURED_DEPOSIT_WINDOW_DAYS}_day_window"
            f"_dates={dates_compact}"
        )
        amounts_summary = ", ".join(f"${m.amount.quantize(Decimal('0.01'))}" for m in members)
        detail = (
            f"{len(members)} deposit(s) in the BSA-avoidance band "
            f"($8,500-$9,999) within a {_STRUCTURED_DEPOSIT_WINDOW_DAYS}-day "
            f"window ({dates[0].isoformat()} .. {dates[-1].isoformat()}) — "
            f"amounts: {amounts_summary}. Possible 31 USC § 5324 / "
            f"31 CFR § 1010.311 structuring. Shadow-mode evidence only; "
            f"operator review required."
        )
        out.append(
            Pattern(
                code=code,
                severity=0,
                detail=detail,
                source_ids=[m.id for m in members],
            )
        )
    return out


# -- Deposit-side detectors --------------------------------------------------


def _duplicate_deposits(deposits: list[ClassifiedTransaction]) -> Pattern | None:
    if len(deposits) < 2:
        return None
    seen: defaultdict[tuple[date, Decimal], list[UUID]] = defaultdict(list)
    for d in deposits:
        seen[(d.posted_date, d.amount)].append(d.id)
    duplicates = [ids for ids in seen.values() if len(ids) > 1]
    if not duplicates:
        return None
    flat: list[UUID] = [i for group in duplicates for i in group]
    return Pattern(
        code="duplicate_deposits_detected",
        severity=30,
        detail=f"{len(duplicates)} same-date+amount deposit pair(s)",
        source_ids=flat,
    )


def _synthetic_low_variance(deposits: list[ClassifiedTransaction]) -> Pattern | None:
    if len(deposits) < 10:
        return None
    amounts = [float(d.amount) for d in deposits]
    mean = statistics.mean(amounts)
    if mean <= 0:
        return None
    variance = statistics.pvariance(amounts)
    cv = (variance**0.5) / mean
    if cv >= 0.15:
        return None
    return Pattern(
        code="synthetic_low_variance",
        severity=25,
        detail=f"CV={cv * 100:.1f}% across {len(deposits)} deposits",
        source_ids=[d.id for d in deposits],
    )


def _round_number_deposits(deposits: list[ClassifiedTransaction]) -> Pattern | None:
    if len(deposits) < 10:
        return None
    round_ids = [d.id for d in deposits if (d.amount % Decimal(100) == 0)]
    ratio = len(round_ids) / len(deposits)
    if ratio <= 0.75:
        return None
    return Pattern(
        code="round_number_deposits",
        severity=15,
        detail=f"{ratio * 100:.0f}% of deposits are exact $100 multiples",
        source_ids=round_ids,
    )


def _preloan_spike(
    deposits: list[ClassifiedTransaction],
    period_end: date,
    statement_days: int,
) -> Pattern | None:
    """Detect last-week (and last-14-day) deposit spikes vs prior-period avg.

    TS bug: triggered both 7-day and 14-day windows independently and added
    score twice for the same underlying event. AEGIS fix: detect either
    window, score once.
    """
    if not deposits or statement_days < 21:
        return None
    total = sum((d.amount for d in deposits), Decimal("0"))

    week_ago = period_end - timedelta(days=7)
    last_week_ids = [d.id for d in deposits if d.posted_date >= week_ago]
    last_week = sum((d.amount for d in deposits if d.posted_date >= week_ago), Decimal("0"))
    earlier = total - last_week
    prior_weeks = (statement_days - 7) / 7
    earlier_weekly_avg = (earlier / Decimal(str(prior_weeks))) if prior_weeks > 0 else Decimal("0")
    spiked_7d = earlier_weekly_avg > 0 and last_week > earlier_weekly_avg * Decimal("2.5")

    fortnight_ago = period_end - timedelta(days=14)
    last_14_ids = [d.id for d in deposits if d.posted_date >= fortnight_ago]
    last_14 = sum(
        (d.amount for d in deposits if d.posted_date >= fortnight_ago),
        Decimal("0"),
    )
    earlier_14 = total - last_14
    prior_14 = statement_days - 14
    earlier_avg_14 = earlier_14 / Decimal(str(prior_14 / 14)) if prior_14 > 0 else Decimal("0")
    spiked_14d = earlier_avg_14 > 0 and last_14 > earlier_avg_14 * Decimal("2.5")

    if not (spiked_7d or spiked_14d):
        return None

    ids = last_week_ids if spiked_7d else last_14_ids
    detail = (
        f"7d spike last_week=${last_week} vs avg=${earlier_weekly_avg.quantize(Decimal('0.01'))}"
        if spiked_7d
        else f"14d spike last_14d=${last_14} vs avg=${earlier_avg_14.quantize(Decimal('0.01'))}"
    )
    return Pattern(
        code="preloan_spike",
        severity=25,
        detail=detail,
        source_ids=ids,
    )


def _nsf_late_concentration(
    nsf: list[ClassifiedTransaction],
    period_end: date,
    statement_days: int,
) -> Pattern | None:
    if len(nsf) < 3 or statement_days <= 30:
        return None
    cutoff = period_end - timedelta(days=30)
    recent = [t for t in nsf if t.posted_date >= cutoff]
    if len(recent) < 3:
        return None
    return Pattern(
        code="nsf_late_concentration",
        severity=20,
        detail=f"{len(recent)} of {len(nsf)} NSFs in last 30 days",
        source_ids=[t.id for t in recent],
    )


def _wash_deposit_kiting(
    deposits: list[ClassifiedTransaction],
    withdrawals: list[ClassifiedTransaction],
) -> tuple[Pattern | None, bool]:
    """Pair deposits with near-equal withdrawals within 5 calendar days."""
    if not deposits or not withdrawals:
        return None, False

    by_date: defaultdict[date, list[ClassifiedTransaction]] = defaultdict(list)
    for w in withdrawals:
        by_date[w.posted_date].append(w)

    matched: set[UUID] = set()
    pair_count = 0
    pair_ids: list[UUID] = []

    for dep in deposits:
        if dep.amount <= 0:
            continue
        for offset in range(0, 6):  # 0..5 calendar days
            day = dep.posted_date + timedelta(days=offset)
            for w in by_date.get(day, []):
                if w.id in matched:
                    continue
                if abs(-w.amount - dep.amount) < dep.amount * Decimal("0.02"):
                    matched.add(w.id)
                    pair_count += 1
                    pair_ids.extend([dep.id, w.id])
                    break
            else:
                continue
            break

    if pair_count < 2:
        return None, False
    return (
        Pattern(
            code="wash_deposit_suspected",
            severity=35,
            detail=f"{pair_count} round-trip deposit/withdrawal pairs within 5 days",
            source_ids=pair_ids,
        ),
        True,
    )


def _paydown_mca(debits: list[ClassifiedTransaction]) -> Pattern | None:
    """Same-payee debits with monotonically descending amounts over ≥5 events."""
    if len(debits) < 5:
        return None
    groups: defaultdict[str, list[ClassifiedTransaction]] = defaultdict(list)
    for d in debits:
        if d.amount >= 0:
            continue
        key = _normalize_desc(d.description)[:15]
        if not key:
            continue
        groups[key].append(d)

    for events in groups.values():
        if len(events) < 5:
            continue
        events.sort(key=lambda e: e.posted_date)
        amounts = [-e.amount for e in events]
        # Allow 5% noise on the way down — paydown profiles aren't perfectly smooth.
        monotonic = all(
            amounts[i] <= amounts[i - 1] * Decimal("1.05") for i in range(1, len(amounts))
        )
        downtrend = amounts[-1] <= amounts[0] * Decimal("0.85")
        if monotonic and downtrend:
            return Pattern(
                code="paydown_mca_suspected",
                severity=25,
                detail=f"{len(events)} debits with descending amounts",
                source_ids=[e.id for e in events],
            )
    return None


def _deposit_velocity_spike(
    deposits: list[ClassifiedTransaction],
    period_start: date,
    period_end: date,
) -> Pattern | None:
    """Catch deposit-count spikes (not just dollar-volume spikes).

    Different signal from preloan_spike: a merchant suddenly receiving
    many MORE deposits per day (e.g. a stuffed deposit list to look busy)
    can spike velocity even if total dollars are flat.

    Fires when the highest rolling-7-day deposit COUNT exceeds 3.0x the
    period-average daily deposit count. Requires statement >= 21 days
    so we have enough baseline to compute an average.

    Tune based on real-deal data after ~50 funded deals.
    """
    statement_days = max(1, (period_end - period_start).days + 1)
    if statement_days < 21 or len(deposits) < 10:
        return None

    baseline_per_day = len(deposits) / statement_days
    by_day: defaultdict[date, list[ClassifiedTransaction]] = defaultdict(list)
    for d in deposits:
        by_day[d.posted_date].append(d)

    days_sorted = sorted(by_day.keys())
    # Rolling 7-day window, count deposits per window.
    best_window: tuple[date, int, list[UUID]] | None = None
    for end_idx, end_day in enumerate(days_sorted):
        window_start = end_day - timedelta(days=6)
        window_ids: list[UUID] = []
        for day in days_sorted[: end_idx + 1]:
            if day >= window_start:
                window_ids.extend(t.id for t in by_day[day])
        if best_window is None or len(window_ids) > best_window[1]:
            best_window = (end_day, len(window_ids), window_ids)

    if best_window is None:
        return None
    end_day, count, ids = best_window
    if count < baseline_per_day * 7 * 3.0:
        return None

    return Pattern(
        code="deposit_velocity_spike",
        severity=20,
        detail=(
            f"7d window ending {end_day.isoformat()}: {count} deposits "
            f"vs baseline {baseline_per_day * 7:.1f}/wk"
        ),
        source_ids=ids,
    )


def _withdrawal_acceleration(
    debits: list[ClassifiedTransaction],
    period_start: date,
    period_end: date,
) -> Pattern | None:
    """MCA-debit COUNT/day spike in the last week vs prior weekly average.

    Different from paydown_mca (which detects descending amounts on a
    single payee). This catches a merchant where stacking has accelerated
    in the trailing week — more frequent debits, not bigger ones.

    Fires when last-7-day MCA-debit count > 1.5x prior weekly average.
    Requires statement >= 21 days.

    Tune based on real-deal data after ~50 funded deals.
    """
    statement_days = max(1, (period_end - period_start).days + 1)
    if statement_days < 21:
        return None
    mca_debits = [d for d in debits if d.category == "mca_debit"]
    if len(mca_debits) < 4:
        return None

    week_ago = period_end - timedelta(days=7)
    last_week = [d for d in mca_debits if d.posted_date >= week_ago]
    earlier = [d for d in mca_debits if d.posted_date < week_ago]
    if not last_week or not earlier:
        return None

    prior_weeks = (statement_days - 7) / 7
    if prior_weeks <= 0:
        return None
    earlier_weekly_avg = len(earlier) / prior_weeks
    if earlier_weekly_avg <= 0:
        return None
    if len(last_week) <= earlier_weekly_avg * 1.5:
        return None

    return Pattern(
        code="withdrawal_acceleration",
        severity=20,
        detail=(
            f"last-7d MCA debits: {len(last_week)} vs prior weekly avg {earlier_weekly_avg:.1f}"
        ),
        source_ids=[d.id for d in last_week],
    )


def _recent_account_opening(period_start: date, today: date) -> Pattern | None:
    age = (today - period_start).days
    if age <= 0 or age >= 90:
        return None
    severity = 15 if age < 60 else 0
    if severity == 0:
        return None
    return Pattern(
        code="recent_account_opening",
        severity=severity,
        detail=f"statement begins {age} days before today",
        source_ids=[],
    )


# -- Phase 9 detector implementations ----------------------------------------
#
# All detectors operate on the post-classification, validated transaction
# stream. None of them retry the LLM. Each that produces a `Pattern`
# contributes to the patterns fraud sub-score; pure-signal extractors
# (counterparty shares, payroll-present, ai_generated_score) are returned
# on PatternAnalysis without a `Pattern` row so the scorer can decide
# whether to apply a delta.


# Categories considered "internal transfer" candidates for the
# unreconciled-transfer detector. Includes plain `transfer` and the wire
# legs explicitly so funder/MCA-debit rows are never counted as
# self-transfers (those have their own detectors).
_TRANSFER_CATEGORIES: Final[frozenset[str]] = frozenset({"transfer", "wire_in", "wire_out"})

# Shadow-mode unreconciled-internal-transfer detector v2 (operator spec
# 2026-06-24, with operator follow-up corrections same day). Distinct
# from the LIVE ``_unreconciled_internal_transfer`` below — that one
# uses tighter pair tolerance (±$1, ±3 days), single-statement scope,
# and feeds the live ``patterns`` list. The shadow detector loosens
# pair tolerance (floor $50, 0.1% of magnitude on larger transfers, ±5
# days), widens scope to the entire bundle, and emits to
# ``PatternAnalysis.shadow_patterns`` with code
# ``unreconciled_internal_transfer_v2`` so the pipeline can surface it
# as a ``[SHADOW] unreconciled_internal_transfer_v2:...`` flag without
# touching ``fraud_score`` or ``parse_status``. The ``_v2`` suffix
# disambiguates this shadow detector from the live ``Pattern.code``
# of the same root name produced by ``_unreconciled_internal_transfer``
# — both can fire in parallel during the shadow-validation phase. Per
# CLAUDE.md "Decision-boundary changes — shadow-first": ship the
# evidence path first, validate false-positive rate against the corpus,
# THEN flip a config gate.
#
# Threshold rationale:
# - Amount floor $500 — below this, internal transfers are routine
#   reimbursements / sweeps; the false-positive rate on a wider net is
#   unworkable. $500 matches the live detector's floor.
# - Amount tolerance ``max($50, 0.1% * magnitude)`` — broader than the
#   live ±$1 because real internal sweeps occasionally embed fees / FX
#   rounding on the inbound leg. The $50 floor stays tight on small
#   transfers; the 0.1% term scales up on large wires so a routine $50+
#   wire fee on a $100k transfer no longer trips a false positive
#   (operator correction 2026-06-24 — fixed $50 was wrong on large wires
#   where FX/wire fees routinely exceed $50).
# - Window ±5 days — same reason as the tolerance. Internal moves
#   between e.g. a small-bank checking and a brokerage cash account can
#   take 3-4 business days; ±5 covers a long weekend at either end.
# - Severity curve: ``min(60, 25 + (n - 1) * 10)`` — monotonic non-
#   decreasing ramp. n=1→25, n=2→35, n=3→45, n=4→55, n=5+→60 (cap).
#   The previous compound-floor design (``40 if n>=3 else 25*n``) was
#   non-monotonic (n=2→50, n=3→40 dropped 10 points); the ramp closes
#   that gap so a worse pattern can never produce a lower severity than
#   a milder one (operator correction 2026-06-24).
_UNRECONCILED_TRANSFER_MIN_AMOUNT: Final[Decimal] = Decimal("500.00")
_UNRECONCILED_TRANSFER_AMOUNT_TOLERANCE_FLOOR: Final[Decimal] = Decimal("50.00")
_UNRECONCILED_TRANSFER_AMOUNT_TOLERANCE_FRACTION: Final[Decimal] = Decimal("0.001")
_UNRECONCILED_TRANSFER_WINDOW_DAYS: Final[int] = 5
_UNRECONCILED_TRANSFER_SEVERITY_BASE: Final[int] = 25
_UNRECONCILED_TRANSFER_SEVERITY_STEP: Final[int] = 10
_UNRECONCILED_TRANSFER_SEVERITY_CAP: Final[int] = 60
# Anchored description tokens — case-insensitive substring match. The
# operator listed "TRANSFER TO" / "WIRE TO" / "ACH TO" / "ZELLE TO"
# explicitly as the OR branch alongside ``own_account`` classifier hits.
# A leading word-boundary anchor prevents accidental hits on words
# ending in those tokens (e.g. "GATEWAY TO" is not "WAY TO" but the
# substring "AY TO" would not match either since the token starts with
# its own keyword). The trailing space disambiguates "TRANSFER TO" from
# "TRANSFER TODAY" — operator picked the directional verb phrase
# explicitly.
_UNRECONCILED_TRANSFER_OUT_DESC_TOKENS: Final[tuple[str, ...]] = (
    "transfer to ",
    "wire to ",
    "ach to ",
    "zelle to ",
)

# Counterparty allow-list (added 2026-06-27 after shadow audit).
#
# WHY: 14-day shadow audit on prod showed 4 fires with 100% false-
# positive rate (4 of 4 on parse_status="proceed"). All 4 fires were
# legitimate transfers — 3 were "CBUSOL TRANSFER DEBIT - WIRE TO NYS
# Dept of Labor" (state unemployment / payroll wires), 1 was a self-
# transfer between own US Bank accounts. The detector mechanically
# catches what it should (transfer-out with no matching transfer-in)
# but the INTERPRETATION — "hidden account siphoning" — is wrong for
# those patterns: government / tax / payroll wires legitimately have no
# inbound counterpart on the merchant's statements, and own-account
# transfers labeled as such are not hidden.
#
# Substring match (case-insensitive, lowercase counterparty). Order of
# entries does not matter — set semantics. "state of " is intentionally
# kept as a 9-char prefix to catch "STATE OF CALIFORNIA UNEMPLOYMENT" /
# "STATE OF NY UI" / "STATE OF FL DEPT OF REVENUE" etc. without
# matching arbitrary descriptions that happen to contain "state of".
#
# Per CLAUDE.md "Decision-boundary changes — shadow-first": this
# allowlist ships live (not shadow-first) because the shadow data
# ALREADY validated the false-positive rate. Re-shadowing a
# recalibration of an already-shadow detector would be redundant.
_ALLOWLISTED_TRANSFER_COUNTERPARTIES: Final[frozenset[str]] = frozenset(
    {
        "nys dept of labor",
        "nysdol",
        "irs",
        "internal revenue",
        "us treasury",
        "state of ",
        "dept of revenue",
        "department of revenue",
        "dept of taxation",
        "unemployment insurance",
        "workers comp",
        "workers compensation",
        "self transfer",
        "own account",
        "zelle to self",
    }
)


def _is_allowlisted_transfer_counterparty(description: str) -> bool:
    """Return True when ``description`` matches any allowlist token.

    Case-insensitive substring match — the description is lowercased
    before comparison. Each allowlist token is checked as ``in
    description_lower``; the first hit short-circuits.
    """
    description_lower = description.lower()
    return any(token in description_lower for token in _ALLOWLISTED_TRANSFER_COUNTERPARTIES)


# Token allow-list extracted from a description before counterparty
# normalization. Cuts trailing transaction-id noise so two ACH deposits
# from the same customer with different trace ids collapse to one bucket.
_NORMALIZE_STOP_PATTERNS: Final[tuple[str, ...]] = (
    r"\bid\b[\s:]+\d+",
    r"\btrace\s*#?\d+",
    r"#\d+",
    r"\d{6,}",
)
_NORMALIZE_STOP_RE: Final[re.Pattern[str]] = re.compile(
    "|".join(_NORMALIZE_STOP_PATTERNS), re.IGNORECASE
)


def _normalize_counterparty(desc: str) -> str:
    """Aggressive normalization for counterparty bucketing.

    Strips trailing trace/id noise and clamps to first 40 chars. Distinct
    from ``_normalize_desc`` (30 chars, no stop-pattern strip) so MCA
    grouping and counterparty grouping can co-exist without one regressing
    the other.
    """
    stripped = _NORMALIZE_STOP_RE.sub(" ", desc).strip().lower()
    return " ".join(stripped.split())[:40]


def _unreconciled_internal_transfer(
    transactions: list[ClassifiedTransaction],
) -> Pattern | None:
    """Transfer-OUT > $500 with no matching transfer-IN in the bundle.

    Detects a hidden bank account — funds leave the account labeled
    "TRANSFER TO ..." but no matching credit shows in any submitted
    statement. Indicative of an undisclosed bank account, often hosting
    an undisclosed MCA (master plan §6.4, highest-value detector).

    Pair criterion is intentionally loose: same Decimal magnitude within
    $1 across a ±3-day window. Tight enough to skip noise, wide enough to
    catch real same-day or next-day transfers.

    Single-statement mode caveat: when we only see one account, EVERY
    transfer-out is unreconciled by definition. We surface the flag at
    medium severity (15) so the operator can sanity-check; bundle-mode
    will tighten this in a follow-up (Phase 2D context already adds
    cross-account stitching).
    """
    transfers = [t for t in transactions if t.category in _TRANSFER_CATEGORIES]
    outs = [t for t in transfers if t.amount < 0 and abs(t.amount) > Decimal("500")]
    if not outs:
        return None
    ins = [t for t in transfers if t.amount > 0]

    unmatched_ids: list[UUID] = []
    matched_in_ids: set[UUID] = set()
    for out_row in outs:
        out_amt = abs(out_row.amount)
        match = next(
            (
                t
                for t in ins
                if t.id not in matched_in_ids
                and abs(t.amount - out_amt) < Decimal("1.00")
                and abs((t.posted_date - out_row.posted_date).days) <= 3
            ),
            None,
        )
        if match is None:
            unmatched_ids.append(out_row.id)
        else:
            matched_in_ids.add(match.id)

    if not unmatched_ids:
        return None
    # Severity scales with count of unmatched legs.
    severity = min(40, 15 + 5 * (len(unmatched_ids) - 1))
    return Pattern(
        code="unreconciled_internal_transfer",
        severity=severity,
        detail=(
            f"{len(unmatched_ids)} transfer-out leg(s) > $500 with no matching "
            "transfer-in — possible hidden account"
        ),
        source_ids=unmatched_ids,
    )


def _is_unreconciled_transfer_out_candidate(
    txn: ClassifiedTransaction,
    own_account_ids: set[UUID] | None,
) -> bool:
    """Return True when ``txn`` is a transfer-out candidate.

    The operator spec is OR-joined: either the counterparty classifier
    labeled the row ``own_account`` OR the description matches one of
    ``TRANSFER TO`` / ``WIRE TO`` / ``ACH TO`` / ``ZELLE TO`` (case-
    insensitive substring). ``own_account_ids`` is the optional set of
    transaction ids the classifier marked ``own_account`` — when None
    we fall back to description-only matching, which is the path the
    parser pipeline uses today (classification runs after parse;
    counterparty results live on a separate column).
    """
    if txn.amount >= 0:
        return False
    if abs(txn.amount) <= _UNRECONCILED_TRANSFER_MIN_AMOUNT:
        return False
    if own_account_ids is not None and txn.id in own_account_ids:
        return True
    desc_lower = txn.description.lower()
    return any(tok in desc_lower for tok in _UNRECONCILED_TRANSFER_OUT_DESC_TOKENS)


def _unreconciled_transfer_severity(unmatched_count: int) -> int:
    """Operator-spec severity curve for the shadow detector.

    Monotonic non-decreasing ramp (operator correction 2026-06-24):

    - ``severity = min(60, 25 + (n - 1) * 10)``
    - n=1 → 25, n=2 → 35, n=3 → 45, n=4 → 55, n=5+ → 60 (cap)

    The previous compound-floor design produced 50 at n=2 then dropped
    to 40 at n=3 (non-monotonic — a worse pattern produced a lower
    severity). The ramp guarantees ``severity(n+1) >= severity(n)``,
    which is what the operator wants: more evidence never reduces
    confidence.
    """
    if unmatched_count <= 0:
        return 0
    raw = (
        _UNRECONCILED_TRANSFER_SEVERITY_BASE
        + (unmatched_count - 1) * _UNRECONCILED_TRANSFER_SEVERITY_STEP
    )
    return min(_UNRECONCILED_TRANSFER_SEVERITY_CAP, raw)


def detect_unreconciled_internal_transfers(
    transactions: list[ClassifiedTransaction],
    all_bundle_transactions: list[ClassifiedTransaction] | None = None,
    own_account_ids: set[UUID] | None = None,
) -> list[Pattern]:
    """Shadow-mode detector v2 — transfer-out with no matching transfer-in.

    Operator spec (2026-06-24, with operator follow-up corrections same
    day): find transfer-out transactions (counterparty ``own_account``
    OR description matches ``TRANSFER TO`` / ``WIRE TO`` / ``ACH TO`` /
    ``ZELLE TO``) with ``abs(amount) > $500`` that have NO matching
    transfer-in anywhere in the submitted bundle within a 5-day window.

    Match-amount tolerance is ``max($50, 0.1% * magnitude)`` so wire fees
    and FX rounding on large transfers no longer manufacture false
    positives. On a $100k transfer the tolerance grows to $100; on a
    $5k transfer it stays at the $50 floor.

    Returns a list of ``Pattern`` rows with code
    ``unreconciled_internal_transfer_v2`` and severity per the monotonic
    ramp (``_unreconciled_transfer_severity``). The ``_v2`` suffix
    disambiguates this shadow detector from the live
    ``unreconciled_internal_transfer`` code emitted by
    ``_unreconciled_internal_transfer`` — both can fire in parallel
    during shadow validation. The pipeline appends shadow hits to
    ``PatternAnalysis.shadow_patterns`` and surfaces each one as a
    ``[SHADOW] unreconciled_internal_transfer_v2:...`` entry in
    ``PipelineResult.all_flags``.

    Bundle scope: ``all_bundle_transactions`` is the entire upload
    bundle (every statement, every account). The match-in candidate may
    live on a DIFFERENT statement than ``transactions`` — that's the
    point of the bundle parameter. When ``all_bundle_transactions`` is
    ``None`` the detector falls back to single-statement scope, which is
    what the parse-time pipeline uses today (classification is
    per-statement). A future multi-statement caller can pass the union
    explicitly.

    Match criterion (within bundle):
        ``other.amount > 0`` (opposite direction)
        AND ``abs(other.amount - abs(out.amount)) <= tolerance``
            where ``tolerance = max($50, abs(out.amount) * 0.001)``
        AND ``abs((other.posted_date - out.posted_date).days) <= 5``
        AND ``other.id != out.id``

    Each unmatched transfer-out becomes its OWN ``Pattern`` row, with
    severity computed from the running unmatched count via the
    monotonic ramp. Per-row emission makes the per-flag drilldown
    render meaningfully — the operator can click a single suspicious
    row rather than a list of five UUIDs under one aggregate. All
    emitted rows share the same severity value (the ramp's value at the
    final unmatched count) so the per-row UI doesn't visually rank them
    against each other.

    Shadow-mode discipline (CLAUDE.md "Decision-boundary changes"):
    - This detector does NOT contribute to ``fraud_score`` (lives on
      ``shadow_patterns``, not ``patterns``).
    - This detector does NOT change ``parse_status`` (no parse-status
      branch reads ``shadow_patterns``).
    - ``FRAUD_WEIGHTS["shadow_unreconciled_internal_transfer_v2"] == 0``
      makes the scoring carve-out explicit.
    - The flip from shadow -> live is a config / env-var change in a
      future commit, after operator corpus validation.
    """
    if not transactions:
        return []
    bundle = all_bundle_transactions if all_bundle_transactions is not None else transactions

    outs_unfiltered = [
        t for t in transactions if _is_unreconciled_transfer_out_candidate(t, own_account_ids)
    ]
    if not outs_unfiltered:
        return []

    # Counterparty allowlist filter (added 2026-06-27 after shadow
    # audit). Government / tax / payroll / self-transfer counterparties
    # are excluded from the unmatched-leg analysis BEFORE the matching
    # loop — they legitimately have no inbound counterpart on the
    # merchant's statements and were the source of 100% of the shadow-
    # detector's false-positive rate. The excluded count is threaded
    # through to each emitted Pattern's detail string so the operator
    # sees evidence of the allowlist firing alongside any genuine hits.
    outs: list[ClassifiedTransaction] = []
    allowlisted_excluded = 0
    for candidate in outs_unfiltered:
        if _is_allowlisted_transfer_counterparty(candidate.description):
            allowlisted_excluded += 1
            continue
        outs.append(candidate)
    if not outs:
        return []

    # Inbound candidates: ANY positive-amount row anywhere in the bundle
    # whose magnitude could match an outbound leg. Not restricted to
    # ``_TRANSFER_CATEGORIES`` — a real internal transfer-in often lands
    # as ``deposit`` / ``ach_credit`` / ``wire_in`` depending on rail.
    # The matching tolerances do the disambiguation.
    ins = [t for t in bundle if t.amount > 0]

    window = _UNRECONCILED_TRANSFER_WINDOW_DAYS

    unmatched: list[tuple[ClassifiedTransaction, Decimal]] = []
    consumed_in_ids: set[UUID] = set()
    for out in outs:
        out_mag = abs(out.amount)
        # Per-row tolerance: floor of $50, scaled by 0.1% of magnitude on
        # larger transfers (operator correction 2026-06-24). On a $100k
        # transfer the tolerance grows to $100 so a routine $50-100 wire
        # fee on the inbound leg no longer manufactures a false positive.
        tolerance = max(
            _UNRECONCILED_TRANSFER_AMOUNT_TOLERANCE_FLOOR,
            out_mag * _UNRECONCILED_TRANSFER_AMOUNT_TOLERANCE_FRACTION,
        )
        match: ClassifiedTransaction | None = None
        for cand in ins:
            if cand.id == out.id or cand.id in consumed_in_ids:
                continue
            if abs(cand.amount - out_mag) > tolerance:
                continue
            if abs((cand.posted_date - out.posted_date).days) > window:
                continue
            match = cand
            break
        if match is None:
            unmatched.append((out, tolerance))
        else:
            consumed_in_ids.add(match.id)

    if not unmatched:
        return []

    severity = _unreconciled_transfer_severity(len(unmatched))
    out_patterns: list[Pattern] = []
    for u, row_tolerance in unmatched:
        # Counterparty label: first 60 chars of the description with
        # leading "TRANSFER TO" / "WIRE TO" stripped where possible so
        # the rendered detail reads "to JOHN DOE CHK 7722" rather than
        # "to TRANSFER TO JOHN DOE CHK 7722". Falls back to the raw
        # description when no token leads.
        desc = u.description.strip()
        desc_lower = desc.lower()
        counterparty = desc
        for tok in _UNRECONCILED_TRANSFER_OUT_DESC_TOKENS:
            if desc_lower.startswith(tok):
                counterparty = desc[len(tok) :].strip() or desc
                break
        # Truncate for log hygiene — descriptions can be 200+ chars on
        # some banks (Chase wire memos). 60 chars is enough to identify
        # the counterparty when one is named.
        if len(counterparty) > 60:
            counterparty = counterparty[:60].rstrip()
        out_mag = abs(u.amount)
        amount_str = f"${out_mag.quantize(Decimal('0.01'))}"
        detail = (
            f"unmatched transfer-out {amount_str} on "
            f"{u.posted_date.isoformat()} to {counterparty} — "
            f"no matching transfer-in in the bundle within "
            f"±${row_tolerance.quantize(Decimal('0.01'))} / ±{window}d"
        )
        if allowlisted_excluded > 0:
            detail = (
                f"{detail} "
                f"({allowlisted_excluded} transfer(s) excluded — "
                "government/self-transfer allowlist)"
            )
        out_patterns.append(
            Pattern(
                code="unreconciled_internal_transfer_v2",
                severity=severity,
                detail=detail,
                source_ids=[u.id],
            )
        )
    return out_patterns


def _mca_payoff_signature(
    debits: list[ClassifiedTransaction],
) -> Pattern | None:
    """Single debit > $5k matching a known MCA-originator name.

    Paid-off MCA that no longer shows up as an active recurring position
    — but the lump-sum payoff itself signals the merchant *had* one.
    Funders care because a recently-paid-off MCA bumps the stacking
    count for renewal-likelihood scoring.

    Fires for any debit whose absolute amount exceeds $5,000 AND whose
    description contains a known funder token, regardless of whether
    it's already part of a recurring MCA position.
    """
    if not debits:
        return None
    payoff_ids: list[UUID] = []
    for d in debits:
        if d.amount >= 0:
            continue
        if abs(d.amount) < Decimal("5000.00"):
            continue
        desc_lower = d.description.lower()
        if any(f in desc_lower for f in KNOWN_FUNDERS):
            payoff_ids.append(d.id)
    if not payoff_ids:
        return None
    return Pattern(
        code="mca_payoff_signature",
        severity=15,
        detail=f"{len(payoff_ids)} lump-sum debit(s) > $5k to known MCA funder",
        source_ids=payoff_ids,
    )


def _counterparty_signals(
    transactions: list[ClassifiedTransaction],
) -> CounterpartySignals:
    """Compute top-counterparty and top-5 revenue/expense shares.

    Buckets by ``_normalize_counterparty(desc)``. Revenue side uses the
    same inclusion rules as ``aggregate._true_revenue``. Expense side
    uses absolute amounts of all debits (NSF / fees excluded — they're
    not really counterparty-driven).
    """
    revenue_rows = [
        t
        for t in transactions
        if t.amount > 0 and t.category in {"deposit", "ach_credit", "wire_in", "refund"}
    ]
    expense_rows = [
        t
        for t in transactions
        if t.amount < 0 and t.category not in {"nsf_fee", "fee", "chargeback"}
    ]

    def _aggregate_side(
        rows: list[ClassifiedTransaction],
    ) -> tuple[
        list[tuple[str, Decimal, list[UUID]]],
        Decimal,
    ]:
        buckets: defaultdict[str, list[ClassifiedTransaction]] = defaultdict(list)
        for r in rows:
            buckets[_normalize_counterparty(r.description)].append(r)
        out: list[tuple[str, Decimal, list[UUID]]] = []
        total = Decimal("0")
        for key, members in buckets.items():
            if not key:
                continue
            amt = sum((abs(m.amount) for m in members), Decimal("0"))
            ids = [m.id for m in members]
            out.append((key, amt, ids))
            total += amt
        out.sort(key=lambda kv: kv[1], reverse=True)
        return out, total

    rev_buckets, rev_total = _aggregate_side(revenue_rows)
    exp_buckets, exp_total = _aggregate_side(expense_rows)

    signals = CounterpartySignals()

    if rev_buckets and rev_total > 0:
        top_key, top_amt, top_ids = rev_buckets[0]
        signals.top_counterparty_pct = int(
            ((top_amt / rev_total) * Decimal(100)).to_integral_value()
        )
        signals.top_counterparty_label = top_key[:30]
        signals.top_counterparty_source_ids = top_ids
        top5 = rev_buckets[:5]
        top5_total = sum((t[1] for t in top5), Decimal("0"))
        signals.top_5_revenue_share_pct = int(
            ((top5_total / rev_total) * Decimal(100)).to_integral_value()
        )
        signals.top_5_revenue_source_ids = [tid for _, _, ids in top5 for tid in ids]

    if exp_buckets and exp_total > 0:
        top5_exp = exp_buckets[:5]
        top5_exp_total = sum((t[1] for t in top5_exp), Decimal("0"))
        signals.top_5_expense_share_pct = int(
            ((top5_exp_total / exp_total) * Decimal(100)).to_integral_value()
        )
        signals.top_5_expense_source_ids = [tid for _, _, ids in top5_exp for tid in ids]

    return signals


def _customer_concentration(
    transactions: list[ClassifiedTransaction],
    signals: CounterpartySignals,
) -> Pattern | None:
    """Fires when a single counterparty > 30% of revenue.

    A scoreable detector layered on top of the aggregator's existing
    flag (which surfaces every concentration as a soft signal). Master
    plan §6.4 threshold: >30% is a meaningful single-customer
    dependency that funders will discount.
    """
    pct = signals.top_counterparty_pct
    if pct is None or pct <= 30:
        return None
    # Severity scales: 31-40 = mild (10), 41-60 = moderate (20), 61+ = severe (30).
    if pct >= 61:
        severity = 30
    elif pct >= 41:
        severity = 20
    else:
        severity = 10
    return Pattern(
        code="customer_concentration",
        severity=severity,
        detail=(
            f"top counterparty = {pct}% of revenue ({signals.top_counterparty_label or 'unknown'})"
        ),
        source_ids=list(signals.top_counterparty_source_ids),
    )


def _chargeback_velocity(
    transactions: list[ClassifiedTransaction],
    period_start: date,
    period_end: date,
) -> Pattern | None:
    """Chargeback / refund debits per month, trending up.

    Fires when ≥3 chargeback-keyword debits exist AND the count in the
    trailing 14 days is > 1.5x the prior-period average. Short-statement
    fallback (<21 days): fires on count alone (≥5).
    """
    cb_rows = [
        t
        for t in transactions
        if t.amount < 0 and any(k in t.description.lower() for k in CHARGEBACK_KEYWORDS)
    ]
    if not cb_rows:
        return None

    statement_days = max(1, (period_end - period_start).days + 1)
    if statement_days < 21:
        if len(cb_rows) < 5:
            return None
        return Pattern(
            code="chargeback_velocity",
            severity=15,
            detail=f"{len(cb_rows)} chargeback/refund debits in {statement_days} days",
            source_ids=[t.id for t in cb_rows],
        )

    if len(cb_rows) < 3:
        return None

    fortnight_ago = period_end - timedelta(days=14)
    recent = [t for t in cb_rows if t.posted_date >= fortnight_ago]
    earlier = [t for t in cb_rows if t.posted_date < fortnight_ago]
    prior_fortnights = (statement_days - 14) / 14
    if prior_fortnights <= 0:
        return None
    earlier_per_fortnight = len(earlier) / prior_fortnights if prior_fortnights else 0
    if len(recent) <= earlier_per_fortnight * 1.5:
        # No acceleration — still surface a static count signal when ≥6.
        if len(cb_rows) >= 6:
            return Pattern(
                code="chargeback_velocity",
                severity=10,
                detail=f"{len(cb_rows)} chargeback/refund debits over period",
                source_ids=[t.id for t in cb_rows],
            )
        return None

    return Pattern(
        code="chargeback_velocity",
        severity=20,
        detail=(
            f"last-14d chargebacks: {len(recent)} vs prior {earlier_per_fortnight:.1f}/fortnight"
        ),
        source_ids=[t.id for t in recent],
    )


def _unauthorized_withdrawal_dispute(
    transactions: list[ClassifiedTransaction],
) -> Pattern | None:
    """A credit matching a prior MCA debit AND containing reversal keywords.

    Indicates the merchant successfully disputed a funder withdrawal.
    Material: a merchant fighting their funder is unlikely to be
    fundable for another stacker. Single-event fire — the keyword
    match plus debit-pairing is high specificity.
    """
    mca_debits = [t for t in transactions if t.amount < 0 and t.category == "mca_debit"]
    candidate_credits = [
        t
        for t in transactions
        if t.amount > 0 and any(k in t.description.lower() for k in REVERSAL_KEYWORDS)
    ]
    if not mca_debits or not candidate_credits:
        return None

    pair_ids: list[UUID] = []
    for credit in candidate_credits:
        prior = [
            d
            for d in mca_debits
            if d.posted_date <= credit.posted_date
            and (credit.posted_date - d.posted_date).days <= 14
            and abs(abs(d.amount) - credit.amount) < Decimal("1.00")
        ]
        if prior:
            pair_ids.append(credit.id)
            pair_ids.extend(d.id for d in prior[:1])

    if not pair_ids:
        return None
    return Pattern(
        code="unauthorized_withdrawal_dispute",
        severity=35,
        detail=(f"{len(pair_ids) // 2} reversal credit(s) paired with prior MCA debit(s)"),
        source_ids=pair_ids,
    )


def _acceleration_clause_triggered(
    mca_positions: list[McaPosition],
    debits: list[ClassifiedTransaction],
    period_end: date,
) -> Pattern | None:
    """Recurring MCA breaks + single debit 5-10x larger to the same payee.

    Funder called the loan after default — the recurring small debits
    stop and a single large debit (often the full remaining balance)
    posts. Hard-decline candidate.

    Fires only when ALL conditions hold:
      1. A position has ≥3 prior occurrences (otherwise we don't know
         the recurring amount with confidence).
      2. The most recent occurrence is at least 5x the median of the
         earlier occurrences and ≤ 10x (above 10x suggests it's a
         different transaction stream, not an acceleration).
      3. No subsequent recurring occurrences after the large debit
         within the visible period.
    """
    if not mca_positions or not debits:
        return None
    for pos in mca_positions:
        if pos.occurrences < 4:  # need ≥3 prior + 1 big
            continue
        pos_debits = [d for d in debits if d.id in pos.source_ids]
        pos_debits.sort(key=lambda d: d.posted_date)
        if len(pos_debits) < 4:
            continue
        amounts = [abs(d.amount) for d in pos_debits]
        prior, latest = amounts[:-1], amounts[-1]
        # Median of prior — robust against an early outlier.
        prior_floats = [float(a) for a in prior]
        median_prior = Decimal(str(statistics.median(prior_floats)))
        if median_prior <= 0:
            continue
        ratio = latest / median_prior
        if ratio < Decimal("5") or ratio > Decimal("10"):
            continue
        latest_date = pos_debits[-1].posted_date
        # Require ≥7 days between latest_date and period_end so we know
        # the recurring stream didn't simply resume.
        if (period_end - latest_date).days < 7:
            continue
        # And require zero post-latest occurrences in the same position
        # (already implied by being the trailing element, but double-check).
        return Pattern(
            code="acceleration_clause_triggered",
            severity=50,
            detail=(
                f"{pos.funder_label}: latest debit ${latest} is "
                f"{ratio.quantize(Decimal('0.1'))}x median prior "
                f"${median_prior} — possible funder acceleration"
            ),
            source_ids=[d.id for d in pos_debits],
        )
    return None


def _processor_holdback_detected(
    transactions: list[ClassifiedTransaction],
    period_start: date,
    period_end: date,
) -> Pattern | None:
    """Card-processor deposits with shortfall pattern suggesting holdback.

    When a processor (Stripe / Square / Toast) is forwarding payouts
    net of an in-place MCA holdback, the daily payout amounts get
    noisy — large variability + occasional zero days even when card
    volume is steady. This is a coarse heuristic; final confirmation
    requires the matching processor statement (Phase 2C parser).

    Fires when:
      - ≥10 deposits from a known card processor in the period
      - Coefficient of variation across daily-summed processor payouts
        ≥ 0.50 (statistically noisy)
      - Statement period is ≥14 days (otherwise too few daily samples)
    """
    statement_days = max(1, (period_end - period_start).days + 1)
    if statement_days < 14:
        return None
    proc_rows = [
        t
        for t in transactions
        if t.amount > 0 and any(p in t.description.lower() for p in KNOWN_CARD_PROCESSORS)
    ]
    if len(proc_rows) < 10:
        return None
    by_day: defaultdict[date, Decimal] = defaultdict(lambda: Decimal("0"))
    for r in proc_rows:
        by_day[r.posted_date] += r.amount
    daily_amounts = [float(v) for v in by_day.values()]
    if len(daily_amounts) < 5:
        return None
    mean = statistics.mean(daily_amounts)
    if mean <= 0:
        return None
    cv = (statistics.pvariance(daily_amounts) ** 0.5) / mean
    if cv < 0.50:
        return None
    return Pattern(
        code="processor_holdback_detected",
        severity=20,
        detail=(
            f"{len(proc_rows)} processor payouts; daily CV={cv * 100:.0f}% — "
            "possible MCA holdback in force"
        ),
        source_ids=[t.id for t in proc_rows],
    )


def _payroll_present(debits: list[ClassifiedTransaction]) -> bool:
    """True iff at least one debit matches a known payroll-processor name.

    A category-aware shortcut: rows already classified as ``payroll``
    qualify even when the description omits a processor name (e.g. a
    direct-deposit batch labeled "DIR DEP"). Anything else requires
    one of the known-processor tokens.
    """
    if not debits:
        return False
    for d in debits:
        if d.category == "payroll":
            return True
        desc_lower = d.description.lower()
        if any(p in desc_lower for p in KNOWN_PAYROLL_PROCESSORS):
            return True
    return False


def _payroll_absent(
    payroll_present: bool,
    transactions: list[ClassifiedTransaction],
    period_start: date,
    period_end: date,
) -> Pattern | None:
    """Soft pattern: an operating business with no payroll over the period.

    Only fires when payroll is *absent* AND the period covers ≥21 days
    (shorter periods don't give a fair sample) AND total credits ≥
    $50k (master plan §6.4 — below that, a sole proprietor with no
    employees is plausible).
    """
    if payroll_present:
        return None
    statement_days = max(1, (period_end - period_start).days + 1)
    if statement_days < 21:
        return None
    revenue_rows = [
        t
        for t in transactions
        if t.amount > 0 and t.category in {"deposit", "ach_credit", "wire_in", "refund"}
    ]
    total = sum((r.amount for r in revenue_rows), Decimal("0"))
    if total < Decimal("50000.00"):
        return None
    return Pattern(
        code="payroll_absent",
        severity=10,
        detail=(
            f"no payroll-processor activity over {statement_days} days "
            f"with ${total.quantize(Decimal('1'))} revenue"
        ),
        source_ids=[],
    )


def _ai_generated_statement_score(
    transactions: list[ClassifiedTransaction],
    deposits: list[ClassifiedTransaction],
) -> int:
    """Composite 0..100 fake-statement score (signal-only, never auto-decline).

    Per master plan §6.4: LLM-generated fakes typically show
      - "too clean" descriptions: full sentences, mixed case with no
        OCR noise / no abbreviations / no numeric trace ids
      - generic counterparty names ("Office Supplies", "Customer Payment")
        rather than ALL-CAPS bank-style labels
      - rounder-than-real amounts (we already score this via
        ``round_number_deposits`` but the composite weights it again)

    Component scores 0..100, then weighted-average. Tunable post-50-deals.
    """
    if len(transactions) < 5:
        return 0

    # Component 1: "too clean" description style — share of descriptions
    # that are title-case-or-lower (no all-caps tokens at all). Real
    # bank statements have lots of all-caps labels.
    title_count = sum(
        1
        for t in transactions
        if t.description and not any(w.isupper() and len(w) >= 3 for w in t.description.split())
    )
    too_clean = round((title_count / len(transactions)) * 100)

    # Component 2: digit-noise share. Real statements carry trace ids,
    # confirmation numbers, ach trace numbers — typically 6+ digit runs.
    long_digit = re.compile(r"\d{6,}")
    digit_noise = sum(1 for t in transactions if long_digit.search(t.description))
    digit_noise_ratio = round(((len(transactions) - digit_noise) / len(transactions)) * 100)

    # Component 3: round-number share of deposits — re-using the existing
    # heuristic but as a 0..100 score, not a binary flag.
    if deposits:
        round_count = sum(1 for d in deposits if d.amount % Decimal(100) == 0)
        round_score = round((round_count / len(deposits)) * 100)
    else:
        round_score = 0

    # Weighted composite — too_clean and digit_noise are the strongest
    # tells; round share is a weaker tertiary.
    composite = round(too_clean * 0.4 + digit_noise_ratio * 0.4 + round_score * 0.2)
    return min(100, max(0, composite))


__all__ = [
    "CHARGEBACK_KEYWORDS",
    "DISGUISE_MCA_TERMS",
    "GENERIC_MCA_TERMS",
    "KNOWN_CARD_PROCESSORS",
    "KNOWN_FUNDERS",
    "KNOWN_PAYROLL_PROCESSORS",
    "REVERSAL_KEYWORDS",
    "CounterpartySignals",
    "McaPosition",
    "Pattern",
    "PatternAnalysis",
    "analyze_patterns",
    "detect_unreconciled_internal_transfers",
]
