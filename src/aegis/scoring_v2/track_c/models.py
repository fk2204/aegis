"""Pydantic models for Track C — Concentration Context Panel.

The panel is the output of the track. It carries the per-class
breakdown, the stress view, and the durability framing copy. It is
ALWAYS informational — no field on this model maps to a decline
boundary in any consumer.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from aegis.counterparty.models import CounterpartyClass
from aegis.money import Money

# Severity tokens for the dossier render. ``info`` = render in the
# neutral panel color; ``review`` = render with a yellow attention
# marker but no auto-decline; ``durability`` = a specific class of
# review marker we want operators to recognize as the
# "international/concentration durability question" reframe.
PanelSeverity = Literal["info", "review", "durability"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
        frozen=False,
    )


class ConcentrationByClass(_StrictModel):
    """One row on the panel: a counterparty class's revenue share."""

    counterparty: CounterpartyClass
    transaction_count: int = Field(ge=0)
    incoming_total: Money
    share_pct: Decimal = Field(
        ge=Decimal("0"),
        le=Decimal("100"),
        max_digits=5,
        decimal_places=2,
        description=(
            "Class incoming_total / panel revenue_total x 100. Rounded "
            "to 2 decimal places. Zero when revenue_total is zero."
        ),
    )
    framing: str = Field(
        max_length=300,
        description=(
            "Human-readable durability reframe for this class. The "
            "literal copy the dossier renders next to the share %."
        ),
    )
    severity: PanelSeverity = Field(
        description=(
            "How the dossier should style this row. NEVER blocks a "
            "decision; severity is rendering hint only."
        ),
    )


class StressView(_StrictModel):
    """The 'what if the top counterparty's inflow dropped?' panel.

    Mirrors the SBA underwriting practice the research note cites:
    base case + stress case, surfaced for human judgment. We do NOT
    compute a coverage ratio against any specific remittance amount —
    that's deal-specific and the underwriter brings it.
    """

    top_class: CounterpartyClass
    top_class_total: Money
    base_revenue: Money
    stress_revenue: Money = Field(
        description=(
            "base_revenue minus top_class_total. The revenue that "
            "remains if the dominant counterparty's inflow disappears "
            "entirely."
        ),
    )
    revenue_drop_pct: Decimal = Field(
        ge=Decimal("0"),
        le=Decimal("100"),
        max_digits=5,
        decimal_places=2,
    )
    framing: str = Field(
        max_length=400,
        description=(
            "Underwriter-facing explanation of what the stress case "
            "answers and what it does NOT answer. Reads like 'if the "
            "international counterparty stops paying, $X of monthly "
            "revenue remains — underwriter checks vs deal remittance'."
        ),
    )


class ConcentrationContextPanel(_StrictModel):
    """The Track C output. Informational, gates nothing.

    The dossier renders this as a panel of class shares + the stress
    view + the unconfirmed-accounts follow-up list. Track B may read
    the rollups to inform its band reasoning; Track C does NOT
    independently fire a decline.
    """

    revenue_basis: Money = Field(
        description=(
            "Sum of incoming amounts across REVENUE classes only "
            "(processor + end_customer + international_client). The "
            "denominator for the share percentages on by_class. "
            "Excludes own_account, own_account_unconfirmed, "
            "book_wire_unresolved, card_paydown, and unknown."
        ),
    )
    by_class: tuple[ConcentrationByClass, ...] = Field(
        description=(
            "One row per revenue class that appeared with non-zero "
            "incoming. Sorted by share_pct desc so the top class is "
            "first; the dossier renders top-down."
        ),
    )
    stress: StressView | None = Field(
        default=None,
        description=(
            "The drop-the-top-class scenario. None when revenue_basis "
            "is zero (nothing to stress) or only one class is present "
            "with one transaction (stress trivially zero)."
        ),
    )
    unconfirmed_account_last4s: tuple[str, ...] = Field(
        default_factory=tuple,
        description=(
            "Last-4s of accounts that appeared as "
            "own_account_unconfirmed somewhere in the bundle. The "
            "operator follow-up list — 'ask for the missing 7722 "
            "month' / 'ask if 9940 is theirs'."
        ),
    )
    book_wire_unresolved_total_incoming: Money = Field(
        description=(
            "Sum of incoming book_wire_unresolved amounts. Surfaced "
            "separately because it's NOT counted as revenue but IS a "
            "real cash inflow shape the operator must resolve."
        ),
    )
    book_wire_unresolved_total_outgoing: Money = Field(
        description="Symmetric outgoing total for book_wire_unresolved."
    )
    warnings: tuple[str, ...] = Field(
        default_factory=tuple,
        description=(
            "Operator-facing warnings about the denominator (e.g. "
            "'$X of incoming classified unknown — revenue denominator "
            "may understate; review classifier coverage'). Never "
            "blocks; always informational."
        ),
    )


# What constitutes "incoming-unknown leak" worth a warning. Stays in
# sync with aggregation.UNKNOWN_INCOMING_WARN_THRESHOLD; re-exported
# here as a panel-side knob so a future tuning is co-located with
# the warning string.
INCOMING_UNKNOWN_WARN_THRESHOLD: Final[Decimal] = Decimal("100.00")


__all__ = [
    "INCOMING_UNKNOWN_WARN_THRESHOLD",
    "ConcentrationByClass",
    "ConcentrationContextPanel",
    "PanelSeverity",
    "StressView",
]
