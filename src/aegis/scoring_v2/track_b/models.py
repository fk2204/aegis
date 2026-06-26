"""Pydantic models for Track B — Business Risk Band.

The output (``BusinessRiskBand``) is the explainable 4-band score
plus the per-factor reasons that produced it. The reasons list is
the load-bearing structural feature: each reason names the factor
(``"true_revenue"``, ``"nsf"``, ``"stacking"``, etc.), its severity,
and a human-readable detail. An underwriter reading the band sees
WHY at a glance.

CRITICAL — no field on this model maps to a decline boundary in any
consumer. A guard test (``test_band_has_no_decline_or_score_field``)
reads the Pydantic schema and asserts the absence of decline-related
fields, preventing a future accidental wiring of Track B into the
live decline path.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from aegis.money import Money

# Band-level taxonomy. Order matters for max-severity computation:
# ``high`` is the most concerning; ``low`` is the cleanest.
BandLevel = Literal["low", "moderate", "elevated", "high"]


# Operator-action mapping (Q1 decided). Both moderate and elevated
# map to review_neutral; the BAND is finer than the action so the
# underwriter can prioritize moderate vs elevated within the queue.
BandAction = Literal[
    "auto_forward",
    "review_neutral",
    "review_decline_default",
]


# Signal severity. Each factor (revenue, NSF, MCA, etc.) is mapped to
# one of these. The band is the worst-case severity across the
# observed factors.
SignalSeverity = Literal[
    "positive",
    "neutral",
    "concern",
    "elevated",
    "critical",
]


# Map from worst-case severity to band. The band is the worst severity
# the underwriter would notice — multiple critical signals don't push
# past ``high``; the band is the ceiling. Underwriter reads the
# reasons list for the full picture.
SEVERITY_TO_BAND: dict[SignalSeverity, BandLevel] = {
    "positive": "low",
    "neutral": "low",
    "concern": "moderate",
    "elevated": "elevated",
    "critical": "high",
}


# Map from band to operator action.
BAND_TO_ACTION: dict[BandLevel, BandAction] = {
    "low": "auto_forward",
    "moderate": "review_neutral",
    "elevated": "review_neutral",
    "high": "review_decline_default",
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
        frozen=False,
    )


class CashflowSignals(_StrictModel):
    """The deterministic cashflow numbers Track B reads from the
    counterparty-aware aggregation.

    All money values use the counterparty-aware revenue basis
    (excludes own_account, own_account_unconfirmed,
    book_wire_unresolved, card_paydown). Track C and Track B share
    the same denominator — that's the foundation pay-off.
    """

    true_revenue_total: Money = Field(
        description=(
            "Sum of incoming amounts across revenue counterparty "
            "classes (processor + end_customer + international_client). "
            "Matches Track C's ``revenue_basis``."
        ),
    )
    statement_period_days: int = Field(
        ge=0,
        description=(
            "Span of the parse bundle in days (max period_end minus "
            "min period_start). Used to normalise revenue to a monthly "
            "equivalent without over-counting overlapping months from "
            "different accounts."
        ),
    )
    monthly_revenue_estimate: Money = Field(
        description=(
            "true_revenue_total / statement_period_days * 30. The "
            "comparable-to-typical-deal revenue figure. Zero when "
            "statement_period_days is zero."
        ),
    )
    average_daily_balance: Money | None = Field(
        default=None,
        description=(
            "Mean of running_balance across days with at least one "
            "transaction. None when running_balance is sparse "
            "(< 25% of rows carry it) — surface as 'insufficient data' "
            "rather than fabricate."
        ),
    )
    lowest_balance: Money | None = Field(
        default=None,
        description=(
            "Minimum running_balance observed. None when sparse. The "
            "deepest negative point in the period — a stronger distress "
            "indicator than average."
        ),
    )
    negative_days: int = Field(
        ge=0,
        description=(
            "Distinct calendar days where running_balance went "
            "negative. Counted only on rows where running_balance is "
            "non-null."
        ),
    )
    nsf_count: int = Field(
        ge=0,
        description=(
            "Count of transactions classified as ``category='nsf_fee'`` "
            "by the parser. NSF frequency is a classical distress "
            "signal; the underwriter cross-references against the "
            "balance trace."
        ),
    )
    mca_position_count: int = Field(
        ge=0,
        description=(
            "Count of transactions classified as ``category='mca_debit'``. "
            "Each represents an active MCA position; stacking is the "
            "load-bearing default-risk signal (~40% of defaults link "
            "to stacking per the operator's underwriting research)."
        ),
    )
    # 2026-06-26 split. ``mca_position_count == mca_confirmed_count +
    # mca_pattern_count`` — exhaustive partition of mca_debit rows by
    # description bucket so the dossier renders "N confirmed; M possible
    # via payment pattern (verify)" instead of one combined number.
    # Default 0/0 keeps the model backward-compatible with callers
    # constructed before the split landed; ``compute_risk_band`` always
    # populates them.
    mca_confirmed_count: int = Field(
        default=0,
        ge=0,
        description=(
            "Subset of ``mca_position_count`` whose description carries "
            "a ``KNOWN_FUNDERS`` substring — named funder recognized, "
            "high confidence."
        ),
    )
    mca_pattern_count: int = Field(
        default=0,
        ge=0,
        description=(
            "Subset of ``mca_position_count`` with no ``KNOWN_FUNDERS`` "
            "match — classified by the LLM via the description but "
            "without a named funder. Renders as 'possible — verify'."
        ),
    )
    international_client_share_pct: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
        le=Decimal("100"),
        description=(
            "Share of revenue from international_client class (the "
            "concentration signal Track C surfaces as durability). "
            "Track B reads it as a band-modifying factor — high "
            "concentration nudges the band up by one notch from "
            "cashflow-only signals."
        ),
    )


class FactorReason(_StrictModel):
    """One factor's contribution to the band decision."""

    factor: str = Field(
        max_length=48,
        description=(
            "Short token identifying the factor (matches "
            "CashflowSignals field name where applicable: "
            "``true_revenue``, ``nsf``, ``mca_positions``, "
            "``negative_days``, ``lowest_balance``, "
            "``international_concentration``, ``trend_volatility``, "
            "``insufficient_data``)."
        ),
    )
    severity: SignalSeverity
    detail: str = Field(
        max_length=240,
        description=(
            "Human-readable one-line explanation. Reads in the "
            "underwriter's voice — 'No NSFs observed across X "
            "transactions' rather than 'nsf_count==0'."
        ),
    )


class BusinessRiskBand(_StrictModel):
    """Track B output. Explainable band + reasons. NO decline gate.

    Read flow for the dossier:

    1. Render ``band`` as the headline chip (color: green / yellow /
       orange / red).
    2. Render ``action`` as the operator-routing hint ("auto-forward",
       "review", "review-decline-default").
    3. Render ``reasons`` as the WHY — one row per factor, ordered by
       severity desc so the most concerning factor is first.
    4. Surface ``cashflow`` as the raw underlying numbers the
       underwriter may want to drill into.

    The dossier renders this as INFORMATIONAL — the live decline
    boundary remains the legacy ``fraud_score`` until Step 2 of the
    redesign deliberately replaces it.
    """

    band: BandLevel
    action: BandAction
    cashflow: CashflowSignals
    reasons: tuple[FactorReason, ...] = Field(
        description=(
            "Every factor whose severity contributed to band decision. "
            "Sorted by severity desc so the most concerning factor "
            "(the one that set the band) is first."
        ),
    )
    insufficient_data_factors: tuple[str, ...] = Field(
        default_factory=tuple,
        description=(
            "Factor names that could not be computed (e.g. ``adb`` "
            "when running_balance is sparse). The band reflects only "
            "the factors that WERE computable; the underwriter sees "
            "this list as a signal of what to ask for."
        ),
    )


__all__ = [
    "BAND_TO_ACTION",
    "SEVERITY_TO_BAND",
    "BandAction",
    "BandLevel",
    "BusinessRiskBand",
    "CashflowSignals",
    "FactorReason",
    "SignalSeverity",
]
