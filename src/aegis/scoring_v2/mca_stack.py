"""MCA-stack aggregation — first-class output for dossier chips.

Reads the merchant's classified transactions and produces four numbers
the underwriter sees alongside the existing fraud / cashflow chips:

* ``active_mca_count`` — how many distinct MCA funder counterparties
  (grouped by the ``_stacking_card._lender_label`` heuristic) have
  debits in the observation window.
* ``mca_monthly_load`` — projected monthly MCA debt-service total.
  Daily-total convention: ``sum(|mca_debit|) / period_days * 22``
  business days, matching ``parser.aggregate._debt_to_revenue`` and
  ``web._stacking_card``.
* ``estimated_combined_holdback_pct`` — ``mca_monthly_load /
  monthly_revenue * 100``. ``None`` when revenue is zero / negative
  (the chip then renders "—" rather than a misleading 0%).
* ``largest_single_mca_monthly`` — the biggest single counterparty's
  projected monthly debit total, with the lender label and source
  transaction ids for drill-down.

Every figure carries its source transaction ids per the AEGIS
"every aggregate stores its sources" rule (CLAUDE.md "Auditability").

DECISION-BOUNDARY POSTURE — SHADOW ONLY
---------------------------------------
``MCA_STACK_OVERLOADED_PCT = 50`` and ``MCA_STACK_COUNT_THRESHOLD = 4``
gates emit ``mca_stack_overloaded_shadow`` / ``mca_stack_count_shadow``
entries on ``MCAStackAggregation.shadow_triggers`` — they do NOT
auto-decline. The existing live rules in ``aegis.scoring.score`` are
more conservative (``MAX_DEBT_TO_REVENUE=40%``,
``MCA_POSITIONS_HARD_DECLINE > 2``) and continue to govern the
decline-boundary. Per CLAUDE.md "Decision-boundary changes —
shadow-first": validate against the corpus before any flip from
shadow to live, and the flip itself is a config change, not code.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aegis.money import Money
from aegis.parser.models import ClassifiedTransaction
from aegis.scoring_v2.lender_label import lender_label


def _counterparty_key(description: str) -> str:
    """Group-by key for an MCA debit description.

    Reuses the shared ``lender_label`` heuristic (first-3-alphanumeric-
    tokens-minus-noise) but pre-strips pure-digit tokens first. Real
    bank descriptions routinely embed a sequence number after the
    funder name (``KAPITUS DEBIT 12345``, ``KAPITUS DEBIT 12346``),
    and ``lender_label`` treats those digits as content — without the
    pre-strip ``KAPITUS 12345`` and ``KAPITUS 12346`` would land in
    distinct groups and over-count ``active_mca_count``.

    The display side of the existing stacking card uses
    ``lender_label`` directly (one debit per row, sequence number
    surfaces as part of the label and that's fine). Clustering is a
    new requirement specific to this aggregation.
    """
    cleaned_tokens = [tok for tok in description.strip().split() if not tok.isdigit()]
    cleaned = " ".join(cleaned_tokens) if cleaned_tokens else description
    return lender_label(cleaned)


MCA_STACK_OVERLOADED_PCT: Final[Decimal] = Decimal("50")
"""Combined-holdback shadow threshold (percent). Above this, emit
``mca_stack_overloaded_shadow``. Strict ``>`` — exactly 50% does not
fire."""

MCA_STACK_COUNT_THRESHOLD: Final[int] = 4
"""Active-counterparty-count shadow threshold. At or above this count,
emit ``mca_stack_count_shadow``. ``>=`` — exactly 4 fires."""

BUSINESS_DAYS_PER_MONTH: Final[Decimal] = Decimal("22")
"""Same convention ``parser.aggregate._debt_to_revenue`` and
``web._stacking_card`` use for projecting daily MCA burden to monthly."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
        frozen=False,
    )


class MCAStackAggregation(_StrictModel):
    """MCA-stack rollup for one merchant's observation window.

    Every metric stores the list of contributing ``transaction_id``s
    per the AEGIS audit-trail rule. ``estimated_combined_holdback_pct``
    shares ``mca_monthly_load``'s source ids because revenue is a
    scalar caller-provided input, not transaction-derived inside this
    module.
    """

    active_mca_count: int = Field(
        ge=0,
        description=(
            "Distinct MCA funder counterparties with at least one debit "
            "in the period (grouped by ``_lender_label`` heuristic). "
            "0 when no MCA debits are present."
        ),
    )
    active_mca_source_ids: tuple[UUID, ...] = Field(
        default_factory=tuple,
        description=(
            "Every classified mca_debit row in the observation window. "
            "The ``active_mca_count`` is derived from these; drill-down "
            "lists each debit row's page/line."
        ),
    )
    mca_monthly_load: Money = Field(
        description=(
            "Projected monthly MCA debt-service. "
            "``sum(|mca_debit|) / period_days * 22 business_days``."
        ),
    )
    mca_monthly_load_source_ids: tuple[UUID, ...] = Field(
        default_factory=tuple,
        description="Same set as ``active_mca_source_ids`` by construction.",
    )
    estimated_combined_holdback_pct: Decimal | None = Field(
        default=None,
        description=(
            "``mca_monthly_load / monthly_revenue * 100``. ``None`` "
            "when revenue is zero / negative — the chip suppresses "
            "rather than rendering a misleading 0%."
        ),
    )
    largest_single_mca_monthly: Money = Field(
        description=(
            "Projected monthly debit total for the largest single "
            "counterparty (by daily-total share). ``0`` when no MCA "
            "debits are present."
        ),
    )
    largest_single_mca_lender: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Lender label of the largest counterparty (from "
            "``_lender_label`` heuristic). ``None`` when no MCA debits "
            "are present."
        ),
    )
    largest_single_mca_source_ids: tuple[UUID, ...] = Field(
        default_factory=tuple,
        description="Debit rows belonging to the largest counterparty.",
    )
    shadow_triggers: tuple[str, ...] = Field(
        default_factory=tuple,
        description=(
            "Shadow-mode decline annotations. Possible entries: "
            "``mca_stack_overloaded_shadow:{pct}%`` when holdback > 50, "
            "``mca_stack_count_shadow:{count}`` when count >= 4. "
            "Operator-facing only — not consumed by the live decline path."
        ),
    )


def aggregate_mca_stack(
    transactions: list[ClassifiedTransaction],
    monthly_revenue: Decimal,
    period_days: int,
) -> MCAStackAggregation:
    """Compute the four MCA-stack numbers for the dossier chips.

    Parameters
    ----------
    transactions
        Every classified transaction in the observation window. Only
        rows with ``category == "mca_debit"`` and ``amount < 0`` are
        considered MCA debits; deposits / credits / non-MCA rows are
        ignored. Caller may pass the full bundle — the function
        filters internally.
    monthly_revenue
        The merchant's normalised monthly revenue (``Decimal``). Used
        as the holdback-percent denominator. Pass the same value the
        existing dossier consumes (``AnalysisRow.monthly_revenue`` or
        the equivalent multi-month figure). Zero / negative produces
        ``estimated_combined_holdback_pct = None``.
    period_days
        Total observation-window length in days. Used to project the
        observed MCA-debit total to a daily average:
        ``sum(|amount|) / period_days``. Caller computes this from
        the statement period (or sum of periods across a bundle).
        Must be ``>= 1``; a value of ``0`` is treated as ``1`` to
        avoid divide-by-zero on a no-data edge case (matches
        ``parser.aggregate._mca_daily_total``).

    Returns
    -------
    MCAStackAggregation
        Always populated, never ``None``. Zero MCA debits produces
        an aggregation with ``active_mca_count = 0`` and empty
        source-id tuples — the caller decides whether to render the
        chips (the existing dossier hides the stacking-card section
        when no MCA is detected; the chips can follow the same rule).
    """
    safe_period_days = Decimal(period_days if period_days >= 1 else 1)

    mca_debits = [t for t in transactions if t.category == "mca_debit" and t.amount < 0]

    if not mca_debits:
        return MCAStackAggregation(
            active_mca_count=0,
            active_mca_source_ids=(),
            mca_monthly_load=Decimal("0.00"),
            mca_monthly_load_source_ids=(),
            estimated_combined_holdback_pct=None,
            largest_single_mca_monthly=Decimal("0.00"),
            largest_single_mca_lender=None,
            largest_single_mca_source_ids=(),
            shadow_triggers=(),
        )

    # Group by lender label. ``_lender_label`` already normalises noise
    # tokens (DEBIT/ACH/PMT/...), so two ``KAPITUS DEBIT N`` rows
    # cluster into a single ``KAPITUS`` group.
    by_lender_amounts: defaultdict[str, list[Decimal]] = defaultdict(list)
    by_lender_ids: defaultdict[str, list[UUID]] = defaultdict(list)
    all_ids: list[UUID] = []
    for t in mca_debits:
        label = _counterparty_key(t.description)
        by_lender_amounts[label].append(-t.amount)  # store positive magnitude
        by_lender_ids[label].append(t.id)
        all_ids.append(t.id)

    active_mca_count = len(by_lender_amounts)

    # Aggregate monthly load = total / period_days * 22.
    total_debits = sum(
        (a for amounts in by_lender_amounts.values() for a in amounts),
        Decimal("0"),
    )
    mca_monthly_load = (total_debits / safe_period_days * BUSINESS_DAYS_PER_MONTH).quantize(
        Decimal("0.01")
    )

    # Holdback percent. ``None`` when revenue is unusable rather than
    # silently rendering a meaningless 0% (mirrors
    # ``_stacking_card.mca_pct_of_deposits``).
    estimated_combined_holdback_pct: Decimal | None
    if monthly_revenue <= 0:
        estimated_combined_holdback_pct = None
    else:
        estimated_combined_holdback_pct = (
            mca_monthly_load / monthly_revenue * Decimal("100")
        ).quantize(Decimal("0.01"))

    # Largest single counterparty by monthly load. Ties broken by the
    # lender-label sort order so the result is deterministic.
    per_lender_monthly = {
        label: (sum(amounts, Decimal("0")) / safe_period_days * BUSINESS_DAYS_PER_MONTH).quantize(
            Decimal("0.01")
        )
        for label, amounts in by_lender_amounts.items()
    }
    largest_label = max(
        per_lender_monthly.keys(),
        key=lambda lab: (per_lender_monthly[lab], lab),
    )
    largest_single_mca_monthly = per_lender_monthly[largest_label]
    largest_single_mca_source_ids = tuple(by_lender_ids[largest_label])

    # Shadow triggers. Strict ``>`` on the percent gate, ``>=`` on the
    # count gate — matches the wording in the spec ("> 50" and ">= 4").
    triggers: list[str] = []
    if (
        estimated_combined_holdback_pct is not None
        and estimated_combined_holdback_pct > MCA_STACK_OVERLOADED_PCT
    ):
        triggers.append(f"mca_stack_overloaded_shadow:{estimated_combined_holdback_pct}%")
    if active_mca_count >= MCA_STACK_COUNT_THRESHOLD:
        triggers.append(f"mca_stack_count_shadow:{active_mca_count}")

    return MCAStackAggregation(
        active_mca_count=active_mca_count,
        active_mca_source_ids=tuple(all_ids),
        mca_monthly_load=mca_monthly_load,
        mca_monthly_load_source_ids=tuple(all_ids),
        estimated_combined_holdback_pct=estimated_combined_holdback_pct,
        largest_single_mca_monthly=largest_single_mca_monthly,
        largest_single_mca_lender=largest_label,
        largest_single_mca_source_ids=largest_single_mca_source_ids,
        shadow_triggers=tuple(triggers),
    )


__all__ = [
    "BUSINESS_DAYS_PER_MONTH",
    "MCA_STACK_COUNT_THRESHOLD",
    "MCA_STACK_OVERLOADED_PCT",
    "MCAStackAggregation",
    "aggregate_mca_stack",
]
