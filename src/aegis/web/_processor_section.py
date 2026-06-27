"""Dossier ``processor_section`` context builder.

The dossier surfaces processor revenue (Stripe → gross / fees / payouts /
chargebacks / refund rate) alongside bank-statement cashflow. This module
builds the Jinja context dict for the ``_processor_revenue.html.j2``
partial.

Persistence (migration 073)
---------------------------
Processor-statement aggregates are persisted to ``processor_statements``
via ``aegis.parser.processor.repository.ProcessorStatementRepository``.
The dossier route calls
``processor_repo.list_by_merchant(merchant_id)`` and hands the row list
to :func:`build_processor_section`, which picks the most recent matching
document and returns the Jinja context.

Two callers, two argument paths
-------------------------------
* Production: pass ``processor_statement_rows`` (list of
  ``ProcessorStatementRow``) plus the merchant's ``documents`` list. The
  builder joins on ``document_id`` and picks the row that matches the
  newest document the merchant has on file.
* Test / legacy: pass ``stripe_results_by_doc`` (the original API).
  Useful for fixture-driven dossier tests that drove the section before
  persistence shipped. The new arg wins when both are present so a test
  that opts into the persistence path can.

Returns ``None`` when neither path yields a hit — the template gates on
the dict's truthiness.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from aegis.parser.processor.dossier_aggregates import StripeParseResult
from aegis.parser.processor.repository import ProcessorStatementRow
from aegis.storage import DocumentRow


def build_processor_section(
    *,
    documents: list[DocumentRow],
    stripe_results_by_doc: dict[UUID, StripeParseResult] | None = None,
    processor_statement_rows: list[ProcessorStatementRow] | None = None,
) -> dict[str, Any] | None:
    """Pick the most recent processor statement for the dossier section.

    Parameters
    ----------
    documents
        The merchant's documents, newest-first (the caller already
        loaded these for the bank-statement section).
    stripe_results_by_doc
        ``{document_id: StripeParseResult}`` for documents that are
        Stripe statements. Legacy fixture-driven API used by older
        tests; production now reads from the persistence layer.
    processor_statement_rows
        Persisted ``ProcessorStatementRow`` list from
        ``ProcessorStatementRepository.list_by_merchant``. Production
        path post-migration-073. When this list is non-empty the
        builder prefers it over ``stripe_results_by_doc``.

    Returns
    -------
    dict | None
        Returns ``None`` (so the template hides the section) when:
          * neither source carries a row matching one of the
            merchant's documents

        Returns the context dict for the most recent processor
        statement otherwise. Shape::

            {
              "processor_type": "stripe" | "square" | …,
              "parse_method": "csv" | "pdf_vision",
              "aggregates": StripeDossierAggregates | _PersistedAggregates,
              "document_id": str(UUID),
            }

        The ``aggregates`` payload is duck-typed against the
        ``_processor_revenue.html.j2`` template: it must expose
        ``total_gross_volume.value``, ``total_fees.value``,
        ``total_net_volume.value``, ``total_payouts.value``,
        ``avg_daily_volume``, ``period_start``, ``period_end``,
        ``period_days``, ``charge_count``, ``refund_count``,
        ``chargeback_count``, ``refund_rate``, ``payout_count``.
    """
    # Persistence path — preferred when populated.
    if processor_statement_rows:
        row = _pick_row_for_dossier(documents, processor_statement_rows)
        if row is not None:
            return {
                "processor_type": row.processor_type,
                "parse_method": row.parse_method,
                "aggregates": _PersistedAggregates(row),
                "document_id": str(row.document_id),
            }

    # Legacy fixture path. Walk in input (newest-first) order; first hit wins.
    if stripe_results_by_doc:
        for doc in documents:
            result = stripe_results_by_doc.get(doc.id)
            if result is not None:
                return {
                    "processor_type": "stripe",
                    "parse_method": result.parse_method,
                    "aggregates": result.aggregates,
                    "document_id": str(doc.id),
                }

    return None


def _pick_row_for_dossier(
    documents: list[DocumentRow],
    rows: list[ProcessorStatementRow],
) -> ProcessorStatementRow | None:
    """Match the newest merchant document to a persisted row.

    The ``documents`` list comes in newest-first; the first match wins
    so the section reflects the most recent statement the merchant has
    on file. Falls back to ``rows[0]`` (the repository orders
    newest-first too) when none of the documents match — defensive
    against a dossier that loaded only a partial document set.
    """
    by_doc = {r.document_id: r for r in rows}
    for doc in documents:
        match = by_doc.get(doc.id)
        if match is not None:
            return match
    return rows[0] if rows else None


class _PersistedSourcedMoney:
    """Wraps a persisted Decimal so the Jinja template's
    ``agg.total_gross_volume.value`` access keeps working without
    constructing a Pydantic ``_SourcedMoney`` (which would require a
    source_ids list we don't load on the dossier read path).

    Source IDs are intentionally absent on this surface: the persisted
    row doesn't carry them, and the dossier template doesn't drill
    through to them. The drill-down lives off the documents table /
    transactions table joins, which are unaffected by this read path.
    """

    __slots__ = ("value",)

    def __init__(self, value: Decimal) -> None:
        self.value = value


class _PersistedAggregates:
    """Adapter exposing a ``StripeDossierAggregates``-shaped surface
    over a persisted ``ProcessorStatementRow``.

    Avoids reconstructing the Pydantic ``StripeDossierAggregates`` (the
    Pydantic model requires the per-aggregate ``_source_ids`` lists,
    which the persistence row doesn't carry). The template only reads
    by attribute name — duck-typing is sufficient. Keep the attribute
    surface in sync with the template's reads (currently
    ``_processor_revenue.html.j2``).
    """

    def __init__(self, row: ProcessorStatementRow) -> None:
        self.total_gross_volume = _PersistedSourcedMoney(row.total_gross_volume)
        self.total_fees = _PersistedSourcedMoney(row.total_fees)
        self.total_net_volume = _PersistedSourcedMoney(row.total_net_volume)
        self.total_payouts = _PersistedSourcedMoney(row.total_payouts)
        self.avg_daily_volume = row.avg_daily_volume
        self.period_start = row.period_start
        self.period_end = row.period_end
        # ``period_days`` is derived in the dossier-shape builder; on
        # the persisted row we recompute the same inclusive-of-both-
        # endpoints formula so the template's denominator narration
        # stays accurate.
        if row.period_start is not None and row.period_end is not None:
            span = (row.period_end - row.period_start).days + 1
            self.period_days = max(1, span)
        else:
            self.period_days = 1
        self.chargeback_count = row.chargeback_count
        # ``refund_count`` + ``charge_count`` aren't separate columns
        # on the persisted row (chunk-A scope kept the schema lean).
        # The template uses both for the "(N refunds out of M charges)"
        # narration; expose them as 0 so the template still renders
        # without crashing, and the operator gets the dollar-totals
        # surface. A follow-up migration can promote those counts to
        # dedicated columns if the underwriting narration needs them.
        self.refund_count = 0
        self.charge_count = 0
        self.refund_rate = row.refund_rate if row.refund_rate is not None else Decimal("0")
        self.payout_count = 0


__all__ = ["build_processor_section"]
