"""Compute the Track C context panel from a parse bundle.

Single entry point: ``compute_context_panel(transactions_by_doc,
classifications)``. Returns a ``ConcentrationContextPanel`` ready for
the dossier to render. Pure function; no I/O.

The compute follows three steps:

1. **Aggregate** via ``aegis.scoring_v2.aggregation.aggregate_bundle``
   to get the per-class rollups and the revenue denominator.
2. **Frame** each revenue class via ``framing.frame_for_class``,
   producing the human-readable durability copy + render hint.
3. **Stress** by dropping the top revenue class entirely and recomputing
   the remaining revenue. (The simplest credible stress; the
   underwriter can drill deeper from the per-class details if a
   single named counterparty within a class is the real concern.)

Non-revenue inflow (own_account moves netted, book_wire_unresolved,
card_paydown on incoming side) is surfaced on the panel as separate
totals so the operator sees the full cash-flow picture without
double-counting non-revenue movements as revenue.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from aegis.counterparty.models import CounterpartyClassification
from aegis.money import Money
from aegis.parser.models import ClassifiedTransaction
from aegis.scoring_v2.aggregation import (
    REVENUE_CLASSES,
    UNKNOWN_INCOMING_WARN_THRESHOLD,
    aggregate_bundle,
)
from aegis.scoring_v2.track_c.framing import frame_for_class, frame_stress
from aegis.scoring_v2.track_c.models import (
    ConcentrationByClass,
    ConcentrationContextPanel,
    StressView,
)


def _share_pct(part: Decimal, whole: Decimal) -> Decimal:
    """Compute the share of ``whole`` ``part`` represents, as a
    Decimal percentage with 2 decimal places. Returns 0 when
    ``whole`` is 0."""
    if whole <= Decimal("0"):
        return Decimal("0.00")
    raw = (part / whole) * Decimal("100")
    return raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_context_panel(
    transactions_by_doc: Mapping[str, list[ClassifiedTransaction]],
    classifications: Mapping[UUID, CounterpartyClassification],
) -> ConcentrationContextPanel:
    """Build the Track C panel for one parse bundle.

    The bundle's transactions + their counterparty labels are the
    only inputs. The function reads no scoring config, calls no LLM,
    queries no database — fully deterministic and unit-testable.
    """
    agg = aggregate_bundle(transactions_by_doc, classifications)

    # Build the per-revenue-class rows. Non-revenue classes are
    # surfaced separately on the panel and never appear in this list.
    by_class_rows: list[ConcentrationByClass] = []
    for rollup in agg.by_class:
        if rollup.counterparty not in REVENUE_CLASSES:
            continue
        if rollup.incoming_total == Money(Decimal("0")):
            continue
        share = _share_pct(
            Decimal(str(rollup.incoming_total)),
            Decimal(str(agg.revenue_total)),
        )
        framing, severity = frame_for_class(rollup.counterparty, share)
        by_class_rows.append(
            ConcentrationByClass(
                counterparty=rollup.counterparty,
                transaction_count=rollup.transaction_count,
                incoming_total=rollup.incoming_total,
                share_pct=share,
                framing=framing,
                severity=severity,
            )
        )

    # Sort by share desc so the top class is first; the dossier
    # renders top-down.
    by_class_rows.sort(key=lambda r: r.share_pct, reverse=True)

    # Stress view: drop the top revenue class entirely. None when
    # there's no revenue or only one row.
    stress: StressView | None = None
    if by_class_rows and agg.revenue_total > Money(Decimal("0")):
        top = by_class_rows[0]
        base = Decimal(str(agg.revenue_total))
        top_total = Decimal(str(top.incoming_total))
        stress_revenue = max(Decimal("0"), base - top_total)
        drop_pct = _share_pct(top_total, base)
        stress = StressView(
            top_class=top.counterparty,
            top_class_total=top.incoming_total,
            base_revenue=agg.revenue_total,
            stress_revenue=Money(stress_revenue),
            revenue_drop_pct=drop_pct,
            framing=frame_stress(
                top.counterparty, base, stress_revenue, drop_pct
            ),
        )

    # Pull the unconfirmed-account last-4 list from classifications.
    # Same source the BundleSummary already exposes; re-derived here
    # so the panel is self-contained.
    unconfirmed: set[str] = set()
    for cc in classifications.values():
        if (
            cc.counterparty == "own_account_unconfirmed"
            and cc.other_account_last4
        ):
            unconfirmed.add(cc.other_account_last4)

    # Book-wire totals (incoming + outgoing). The single
    # ``book_wire_unresolved`` rollup carries both directions.
    book_in = Money(Decimal("0"))
    book_out = Money(Decimal("0"))
    for rollup in agg.by_class:
        if rollup.counterparty == "book_wire_unresolved":
            book_in = rollup.incoming_total
            book_out = rollup.outgoing_total
            break

    # Warnings. Today: incoming-unknown threshold check, and the
    # "no revenue at all" case (the panel still renders, but the
    # underwriter sees zero revenue with potentially large
    # non-revenue inflows — usually a sign of incomplete bundle or
    # mis-classification upstream).
    warnings: list[str] = []
    if agg.unknown_incoming_total > Money(UNKNOWN_INCOMING_WARN_THRESHOLD):
        warnings.append(
            f"${agg.unknown_incoming_total:,.2f} of incoming amounts "
            "classified as unknown — revenue denominator may understate. "
            "Review classifier coverage in the reclassify CLI before "
            "trusting share %s."
        )
    if agg.revenue_total == Money(Decimal("0")) and agg.excluded_inflow_total > Money(
        Decimal("0")
    ):
        warnings.append(
            f"Zero revenue in named classes but ${agg.excluded_inflow_total:,.2f} "
            "of non-revenue inflow detected (own_account, book_wire, etc.). "
            "Bundle may be incomplete or mis-classified upstream."
        )

    return ConcentrationContextPanel(
        revenue_basis=agg.revenue_total,
        by_class=tuple(by_class_rows),
        stress=stress,
        unconfirmed_account_last4s=tuple(sorted(unconfirmed)),
        book_wire_unresolved_total_incoming=book_in,
        book_wire_unresolved_total_outgoing=book_out,
        warnings=tuple(warnings),
    )


__all__ = ["compute_context_panel"]
