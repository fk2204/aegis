"""Pydantic models for the scoring/matching pipeline.

ScoreInput is the merged view of merchant + statement aggregates that the
scorer reads. ScoreResult is what comes out (recommendation + reasons).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from aegis.money import Money

Recommendation = Literal["approve", "decline", "refer"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )


class MonthBreakdown(_StrictModel):
    """One calendar month's roll-up. Used for revenue trend + volatility scoring."""

    month: str = Field(pattern=r"^\d{4}-\d{2}$", description="ISO YYYY-MM")
    deposits: Money
    withdrawals: Money
    avg_balance: Money


class ScoreInput(_StrictModel):
    """Inputs to the scorer. Merged from multiple sources before scoring runs.

    Field groupings (each block lists its source):

    - Merchant identity (merchants table): `merchant_id`, `business_name`,
      `owner_name`, `state`, `industry_naics`, `industry_risk_tier`,
      `time_in_business_months`, `credit_score`. Hand-entered during
      onboarding; missing values become soft concerns rather than
      silent passes.

    - Parser aggregates (parser/aggregate.py): `avg_daily_balance`,
      `true_revenue`, `monthly_revenue` (true_revenue projected to 30
      days), `lowest_balance`, `num_nsf`, `days_negative`,
      `mca_positions`, `mca_daily_total`, `debt_to_revenue`,
      `payroll_detected`, `returned_ach_count`,
      `customer_concentration_pct`, `statement_period_*`,
      `statement_days`. All deterministic — no LLM influence.

    - Parser pipeline findings (parser/pipeline.py): `fraud_score`,
      `eof_markers`, `validation_passed`, `extraction_confidence`. Each
      maps to a hard-decline rule (eof > 1, validation failed) or a
      soft-scoring penalty.

    - Requested terms (operator-entered or broker-quoted):
      `requested_amount`, `requested_factor`, `requested_term_days`.
      The scorer may override these with `suggested_max_advance` /
      `recommended_factor_rate` based on tier.

    - Renewal context (optional, populated when this is a renewal):
      `is_renewal`, `prior_payoff_performance`, `prior_advance_count`.
      A `prior_payoff_performance == "default"` triggers a hard decline.

    - Multi-month context (optional, when sufficient history available):
      `monthly_breakdown`. Drives revenue-trend (3-month) and CV-based
      volatility (4-month) soft scoring.

    - DSCR inputs (optional, both required together):
      `total_monthly_obligations`, `proposed_daily_payment`. When both
      present, scoring runs DSCR hard decline (< 1.0) and adds a
      tier-based soft adjustment.
    """

    merchant_id: UUID
    business_name: str
    owner_name: str
    state: str = Field(min_length=2, max_length=2, description="USPS state code")
    industry_naics: str | None = None
    industry_risk_tier: Literal["low", "moderate", "elevated", "high", "avoid"] | None = None
    # Close Lead-side ``Industry`` choice string (em-dash form) the
    # merchant carries on ``merchant.industry_choice``. Drives the
    # ``match_funder`` exclusion gate (word-set match against funder
    # ``excluded_industries``). ``None`` falls through to the NAICS-
    # derived industry-name fallback in ``aegis.scoring.match_funders``.
    industry_choice: str | None = None
    time_in_business_months: int | None = Field(default=None, ge=0)
    credit_score: int | None = Field(default=None, ge=300, le=850)

    # Aggregates from the parser
    avg_daily_balance: Money
    true_revenue: Money
    monthly_revenue: Money
    lowest_balance: Money
    num_nsf: int = Field(ge=0)
    days_negative: int = Field(ge=0)
    mca_positions: int = Field(ge=0)
    mca_daily_total: Money
    debt_to_revenue: Decimal = Field(ge=Decimal("0"))
    payroll_detected: bool = False
    returned_ach_count: int = Field(default=0, ge=0)
    customer_concentration_pct: int | None = Field(default=None, ge=0, le=100)
    statement_period_start: date
    statement_period_end: date
    statement_days: int = Field(ge=0, description="length of the statement period in days")

    # Parser pipeline findings
    fraud_score: int = Field(ge=0, le=100)
    eof_markers: int = Field(default=1, ge=0)
    validation_passed: bool = True
    extraction_confidence: int = Field(default=100, ge=0, le=100)

    # Requested terms
    requested_amount: Money
    requested_factor: Decimal = Field(gt=Decimal("1"), le=Decimal("2"))
    requested_term_days: int = Field(ge=1, le=730)

    # Renewal context (optional)
    is_renewal: bool = False
    prior_payoff_performance: Literal["early", "on_time", "late", "default"] | None = None
    prior_advance_count: int = Field(default=0, ge=0)

    # Multi-month context for trend / volatility scoring (optional)
    monthly_breakdown: list[MonthBreakdown] = Field(default_factory=list)

    # DSCR inputs (optional). When BOTH are present, scoring runs DSCR
    # hard decline (< 1.0) and soft scoring (1.5 / 1.15 / 1.0 thresholds).
    total_monthly_obligations: Money | None = None
    proposed_daily_payment: Money | None = None

    # Counterparty signals (master plan §5.7). Populated from
    # ``patterns.CounterpartySignals``. None when the underlying side
    # is empty (e.g. zero deposits in the window).
    top_counterparty_pct: int | None = Field(default=None, ge=0, le=100)
    top_counterparty_label: str | None = None
    top_5_revenue_share_pct: int | None = Field(default=None, ge=0, le=100)
    top_5_expense_share_pct: int | None = Field(default=None, ge=0, le=100)
    payroll_present: bool = False

    # Phase 9 hard-decline triggers (master plan §19 tasks 3).
    # ``acceleration_clause_triggered`` and ``unauthorized_withdrawal_dispute``
    # each promote to a hard-decline reason when True.
    # ``tampering_confirmed`` is the composite signal from the pipeline
    # (metadata + math + patterns); set True when fraud_score already
    # crossed the hard threshold via the multi-layer composite path,
    # so we can attach the named hard-decline reason in addition.
    acceleration_clause_triggered: bool = False
    unauthorized_withdrawal_dispute: bool = False
    tampering_confirmed: bool = False

    # R3.4 — advance-fee shadow flag (FL / GA). True when the deal charges
    # merchant-side advance fees (sourced from
    # ``funder.charges_merchant_advance_fees`` for the matched funder at
    # build-input time). Drives the FL / GA shadow flag in
    # ``_state_disclosure_flag``. ``None`` means "unknown" — the flag does
    # not fire under uncertainty (default of all pre-R3.4 callers).
    advance_fees_charged: bool | None = None
    # 0..100 composite "looks AI-generated" score. Scored softly,
    # never auto-declined (per §6.4 — false positives kill real deals).
    ai_generated_score: int = Field(default=0, ge=0, le=100)


PaperGrade = Literal["A", "B", "C", "D"]


class ScoreResult(_StrictModel):
    """Output of scoring. `recommendation` is the gate the rest of the system uses."""

    score: int = Field(ge=0, le=100)
    tier: Literal["A", "B", "C", "D", "F"]
    recommendation: Recommendation
    hard_decline_reasons: list[str] = Field(default_factory=list)
    soft_concerns: list[str] = Field(default_factory=list)
    breakdown: list[dict[str, object]] = Field(default_factory=list)
    suggested_max_advance: Money = Decimal("0.00")
    recommended_factor_rate: Decimal = Decimal("0.00")
    recommended_holdback_pct: Decimal = Decimal("0.00")
    estimated_payback_days: int | None = Field(default=None, ge=0)
    apr: Decimal | None = None
    decline_details: dict[str, list[dict[str, str]]] = Field(default_factory=dict)
    """Structured detail attached to specific hard declines that need
    downstream audit + reporting. Currently populated for
    ``ofac_sanctions_match`` with key ``"ofac_matches"`` and entries shaped
    ``{"input_field": "business_name"|"owner_name", "matched_name": str,
    "sdn_uid": str}``. ``sdn_uid`` may be empty when the cached SDN feed
    pre-dates the uid plumbing or for hand-built test fixtures."""

    # Phase 9: industry-standard paper grade (master plan §5.8). Distinct
    # from ``tier`` (AEGIS internal score). Paper grade is the funder-
    # facing classification: A = first-position prime, B = mainstream,
    # C = sub-prime, D = last-resort. Hard-declined deals carry the
    # last paper grade they would have received before the decline
    # rules fired (defaults to D when a soft scoring path is not taken).
    paper_grade: PaperGrade = "D"
    paper_grade_reasons: list[str] = Field(default_factory=list)
    """Codes explaining which §5.8 criteria the deal satisfied / missed
    for each grade (e.g. ``"tib_24mo+"``, ``"adb_lt_8pct"``). Empty when
    paper_grade was not computed (hard decline path)."""

    shadow_flags: list[str] = Field(default_factory=list)
    """Shadow-mode evidence annotations from R3 / R4 audit-driven changes.
    Per CLAUDE.md "Decision-boundary changes — shadow-first": new
    detectors / policy reconciliations log what they WOULD do here without
    altering ``score``, ``tier``, ``recommendation``, or any existing
    field. The operator validates against the corpus + live shadow rows
    before flipping severity via config. Currently sourced from:

    - R4.6 EOF policy mismatch (``eof_policy_mismatch:...``) — emitted
      when the legacy scorer hard-declines at >1 EOF while pipeline
      policy treats 2 EOFs as review (per ``docs/AUDIT_2026_05_10.md``
      line 46).
    - R4.4 industry-aware seasonality on the CV penalty path
      (``seasonality_recategorized:...`` or
      ``seasonality_observed_but_volatility_extreme:...``) — emitted on
      a high CV when the merchant's NAICS prefix is in the known-seasonal
      set. Existing ``-12 high_revenue_volatility`` penalty still fires.
    - R3.4 state-by-state enforcement signals
      (``state_enforcement_concern:TX_HB700_tx_merchant_review`` or
      ``state_enforcement_concern:FL_GA_advance_fee_prohibition``) —
      operator-side review hints; no tier / recommendation change."""


class EstimatedTerms(_StrictModel):
    """Per-funder pricing guidance for an offered deal.

    Computed in ``scoring/match_funders.compute_estimated_terms`` from the
    funder's ``typical_factor_low/high`` and ``typical_holdback_low/high``
    pricing envelope plus the deal's score tier. The interpolation runs
    from "best end of the funder's range" (tier A) to "worst end" (tier
    E/F), so an A-tier deal gets the favorable factor, an F-tier deal gets
    the most defensive factor the funder lists.

    All fields are Decimal; nothing here is float. APR uses the Reg Z
    Appendix J actuarial method via ``compliance.apr.calculate_apr`` —
    never hand-rolled. ``estimated_apr`` is ``None`` (not silent zero)
    when ``calculate_apr`` cannot bracket a root: a 0.00% APR rendered
    next to a 1.30x factor is a regulator-grade lie, so refuse to render
    rather than fabricate.

    ``interpolation_evidence`` exposes the linear-interpolation step the
    operator would otherwise need to re-derive (tier=C → "50% along
    factor range 1.20-1.40 → 1.30"). Useful for explaining a quote to a
    funder rep without re-opening the model.
    """

    estimated_advance: Money
    """Suggested advance clamped to ``[funder.min_advance, funder.max_advance]``.
    Derived from ``ScoreResult.suggested_max_advance``."""

    estimated_factor: Decimal
    """Factor rate (1.0+) interpolated within the funder's
    ``typical_factor_low..typical_factor_high`` range."""

    estimated_holdback_pct: Decimal
    """Holdback as a fraction (0.12 for 12%), interpolated within
    ``typical_holdback_low..typical_holdback_high``."""

    estimated_daily_payment: Money
    """``advance * factor / payback_days``. Uses
    ``ScoreResult.estimated_payback_days`` for the term."""

    estimated_apr: Decimal | None
    """APR as a fraction (0.365 → 36.5%) via the Reg Z Appendix J
    actuarial method on the synthesized daily payment stream. ``None``
    when the optimizer cannot converge — NOT silently 0%."""

    interpolation_evidence: str
    """Human-readable derivation, e.g.
    ``"tier=C → 50% along factor range 1.20-1.40 → 1.30"``. Surfaced in
    the dossier so the operator can sanity-check the quote."""


class TierMatch(_StrictModel):
    """Per-tier qualification evidence for a funder with structured tiers (U28).

    Funders with `tiers` JSONB populated (operator-curated underwriting
    matrices — e.g. Logic Advance's Elite/Premium/Standard/High-Risk or
    UCS's seven product lines) get one ``TierMatch`` per tier on the
    parent ``FunderMatch``. The matcher evaluates each tier's published
    criteria against the merchant; ``qualifies`` is ``True`` only when
    every constraint the tier specifies is satisfied.

    SHADOW MODE — per CLAUDE.md "Decision-boundary changes — shadow-first":
    this signal is annotation-only. It does NOT change
    ``FunderMatch.match_score``, ``soft_concerns``, ``reasons``, or the
    parent ``FunderRow``-level qualification result. The operator
    validates per-tier results against the corpus, then a future code
    change promotes tier-aware matching to drive the live decision.

    Per-tier economics (``estimated_factor_*``, ``estimated_holdback``,
    ``estimated_advance``) are sourced from the tier's own
    ``buy_rate_low/high``, ``max_holdback`` and ``max_advance`` fields
    rather than the funder's top-level ``typical_*`` envelope; they are
    ``None`` when the tier did not publish that axis.
    """

    tier_name: str = Field(min_length=1)
    """Verbatim tier label from ``FunderTier.name`` (e.g. ``"Elite"``,
    ``"Standard"``, ``"MCA"``)."""

    qualifies: bool
    """True when the merchant satisfies every constraint this tier
    specifies. A tier with no constraints (all-None thresholds) is
    treated as qualifying — absence of a policy is not failure."""

    disqualifying_reasons: list[str] = Field(default_factory=list)
    """Per-axis failure codes (e.g. ``"credit 620 < min 700"``,
    ``"tib 8mo < min 12mo"``). Empty when ``qualifies`` is True.
    Mirrors the shape of ``FunderMatch.soft_concerns`` strings so the UI
    can render them with the same component."""

    estimated_factor_low: Decimal | None = None
    """Lower bound of the tier's buy-rate range (``FunderTier.buy_rate_low``)."""

    estimated_factor_high: Decimal | None = None
    """Upper bound of the tier's buy-rate range (``FunderTier.buy_rate_high``)."""

    estimated_holdback: Decimal | None = None
    """Tier's ``max_holdback`` as a fraction (0.15 for 15%). The tier
    publishes a ceiling, not a range, so a single value rather than
    low/high."""

    estimated_advance: Money | None = None
    """``ScoreResult.suggested_max_advance`` clamped down to
    ``FunderTier.max_advance`` (the tier's ceiling). ``None`` when the
    tier did not publish a max_advance."""

    estimated_payback_total: Money | None = None
    """``advance * buy_rate_midpoint`` — the simple total the merchant
    repays at this tier. ``None`` when ``estimated_advance`` is ``None``
    or the tier omits ``buy_rate_low/high``. Computed by
    ``scoring.pricing.estimate_tier_pricing`` (U37). Operator-facing
    pricing hint only; NOT a Reg Z APR — see that module's docstring."""

    estimated_daily_payment: Money | None = None
    """``payback_total / 252`` (MCA business-day convention). ``None``
    under the same conditions as ``estimated_payback_total``. Surfaced
    next to the factor range in the matched-funders inline panel so the
    operator can sanity-check "what does the daily debit look like at
    this tier"."""


class FunderMatch(_StrictModel):
    """A funder candidate ranked against this merchant's profile."""

    funder_id: UUID
    funder_name: str
    match_score: int = Field(ge=0, le=100)
    reasons: list[str] = Field(default_factory=list)
    soft_concerns: list[str] = Field(default_factory=list)
    estimated_terms: EstimatedTerms | None = None
    """Per-funder pricing guidance. ``None`` when the funder has no
    ``typical_factor_*`` / ``typical_holdback_*`` envelope, or when
    ``estimated_payback_days`` is missing from the score result, or when
    the score tier falls outside the interpolation table."""

    tier_matches: list[TierMatch] = Field(default_factory=list)
    """U28 — per-tier qualification evidence for funders with structured
    ``tiers`` JSONB. Empty for funders whose ``tiers`` tuple is empty
    (legacy / pre-extraction funders). SHADOW MODE: present for
    operator review; does not influence ``match_score``,
    ``soft_concerns``, or ``reasons``. See ``TierMatch`` docstring."""


class SubmissionPackage(_StrictModel):
    """Everything a funder needs to evaluate the deal."""

    id: UUID = Field(default_factory=uuid4)
    score_input: ScoreInput
    score_result: ScoreResult
    matched_funders: list[FunderMatch]
    email_subject: str
    email_body: str


class DealMatchResult(_StrictModel):
    """Combined score + funder matches for a deal.

    Returned by ``POST /deals/score-with-matches``. Lighter than
    ``SubmissionPackage`` because the dashboard match panel doesn't need
    the email-template fields.
    """

    score: ScoreResult
    matched_funders: list[FunderMatch]


__all__ = [
    "DealMatchResult",
    "EstimatedTerms",
    "FunderMatch",
    "PaperGrade",
    "Recommendation",
    "ScoreInput",
    "ScoreResult",
    "SubmissionPackage",
    "TierMatch",
]
