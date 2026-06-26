"""Dossier ``processor_section`` context builder.

The dossier surfaces processor revenue (Stripe → gross / fees / payouts /
chargebacks / refund rate) alongside bank-statement cashflow. This module
builds the Jinja context dict for the ``_processor_revenue.html.j2``
partial.

Persistence note
----------------
Processor-statement aggregates are NOT yet persisted to a dedicated DB
table — the worker writes a ``document.parse.processor_complete``
``audit_log`` row with the aggregates, but the dossier doesn't read from
``audit_log``. Until the ``processor_statements`` table lands (deferred
follow-up — see ``aegis.workers._run_processor_branch`` docstring), this
builder returns ``None`` for any document that lacks a pre-built
``StripeParseResult`` on its row attributes.

The builder is shaped so the dossier template + tests can drive it from
either:
  * a list of ``(DocumentRow, StripeParseResult)`` pairs assembled by
    the route (production once persistence lands), OR
  * a synthesized fixture in a test that hands a single Stripe parse
    result directly.

When persistence lands, route code calls ``build_processor_section`` with
the production pairs. Until then, the production call returns ``None``
and the dossier section stays hidden — the template gates on the key's
presence.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from aegis.parser.processor.dossier_aggregates import StripeParseResult
from aegis.storage import DocumentRow


def build_processor_section(
    *,
    documents: list[DocumentRow],
    stripe_results_by_doc: dict[UUID, StripeParseResult] | None = None,
) -> dict[str, Any] | None:
    """Pick the most recent Stripe statement for the dossier section.

    Parameters
    ----------
    documents
        The merchant's documents, newest-first (the caller already
        loaded these for the bank-statement section).
    stripe_results_by_doc
        ``{document_id: StripeParseResult}`` for documents that are
        Stripe statements. Production fills this from the persistence
        layer once the ``processor_statements`` table ships. Tests
        hand-feed it.

    Returns
    -------
    dict | None
        Returns ``None`` (so the template hides the section) when:
          * ``stripe_results_by_doc`` is None / empty
          * none of the merchant's documents have a Stripe result on file

        Returns the context dict for the most recent Stripe statement
        otherwise. Shape::

            {
              "processor_type": "stripe",
              "parse_method": "csv" | "pdf_vision",
              "aggregates": StripeDossierAggregates,
              "document_id": str(UUID),
            }
    """
    if not stripe_results_by_doc:
        return None

    # Walk in input (newest-first) order; first hit wins.
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


__all__ = ["build_processor_section"]
