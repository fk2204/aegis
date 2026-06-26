"""Dossier processor-revenue section rendering.

The dossier's § 2b "Processor revenue" section renders ONLY when a
``processor_section`` context value is present. When absent, the
section is hidden completely — card-light merchants don't see an
empty Stripe panel.

These tests render the partial directly through the same Jinja
environment the dossier route uses. They lock the gating contract
into place: presence of the key drives presence of the section.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from aegis.parser.processor.dossier_aggregates import (
    StripeDossierAggregates,
    StripeParseResult,
    _SourcedMoney,
)
from aegis.parser.processor.models import (
    ExtractedProcessorStatement,
    ProcessorLineItem,
    ProcessorSummary,
)
from aegis.web._processor_section import build_processor_section
from aegis.web._templates import templates


def _make_stripe_result() -> StripeParseResult:
    """Synthesize a minimal but realistic Stripe parse result.

    Mirrors the shape ``aegis.parser.processor.csv_stripe.extract_stripe_csv``
    produces, but constructed in-process so the test doesn't depend on
    the CSV fixture file.
    """
    charge_id = uuid4()
    fee_id = uuid4()
    payout_id = uuid4()

    rows = [
        ProcessorLineItem(
            id=charge_id,
            posted_date=date(2026, 3, 1),
            description="Charge ch_test_001",
            kind="gross_charge",
            amount=Decimal("1000.00"),
            source_page=1,
            source_line=2,
        ),
        ProcessorLineItem(
            id=fee_id,
            posted_date=date(2026, 3, 1),
            description="Stripe fee on charge",
            kind="fee",
            amount=Decimal("30.00"),
            source_page=1,
            source_line=2,
        ),
        ProcessorLineItem(
            id=payout_id,
            posted_date=date(2026, 3, 15),
            description="STRIPE PAYOUT",
            kind="payout",
            amount=Decimal("970.00"),
            source_page=1,
            source_line=3,
        ),
    ]
    summary = ProcessorSummary(
        processor="stripe",
        business_name="Acme Tech LLC",
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
        gross_volume=Decimal("1000.00"),
        refunds_total=Decimal("0.00"),
        chargebacks_total=Decimal("0.00"),
        fees_total=Decimal("30.00"),
        payouts_total=Decimal("970.00"),
        transaction_count=1,
    )
    extraction = ExtractedProcessorStatement(summary=summary, transactions=rows)
    aggregates = StripeDossierAggregates(
        total_gross_volume=_SourcedMoney(value=Decimal("1000.00"), source_ids=[charge_id]),
        total_fees=_SourcedMoney(value=Decimal("30.00"), source_ids=[fee_id]),
        total_net_volume=_SourcedMoney(value=Decimal("970.00"), source_ids=[charge_id, fee_id]),
        total_payouts=_SourcedMoney(value=Decimal("970.00"), source_ids=[payout_id]),
        payout_count=1,
        avg_daily_volume=(Decimal("1000.00") / Decimal("31")).quantize(Decimal("0.01")),
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
        period_days=31,
        chargeback_count=0,
        refund_count=0,
        charge_count=1,
        refund_rate=Decimal("0"),
    )
    return StripeParseResult(
        extraction=extraction,
        aggregates=aggregates,
        parse_method="csv",
        period_days=31,
    )


def _render_partial(processor_section: dict[str, Any] | None) -> str:
    """Render the processor-revenue partial in isolation."""
    template = templates.get_template("_processor_revenue.html.j2")
    return template.render(processor_section=processor_section)


# ---------------------------------------------------------------------------
# Section gating — presence drives rendering
# ---------------------------------------------------------------------------


def test_processor_section_renders_when_stripe_result_present() -> None:
    """Stripe parse result on the context → section heading + gross
    volume number both surface in the HTML."""
    result = _make_stripe_result()
    section = {
        "processor_type": "stripe",
        "parse_method": result.parse_method,
        "aggregates": result.aggregates,
        "document_id": "11111111-1111-1111-1111-111111111111",
    }
    html = _render_partial(section)
    assert "Processor" in html
    assert "revenue" in html  # the <em>revenue</em> heading
    assert "1,000.00" in html  # gross volume formatted
    assert "Stripe" in html
    assert "CSV export" in html  # parse_method label


def test_processor_section_hidden_when_context_absent() -> None:
    """No ``processor_section`` in context → empty render. The bank-
    statement-only merchant doesn't see the panel at all."""
    html = _render_partial(None)
    assert "Processor" not in html
    assert "Gross Volume" not in html


def test_processor_section_pdf_vision_label_renders() -> None:
    """``parse_method="pdf_vision"`` surfaces the right label, not the
    CSV one."""
    result = _make_stripe_result()
    section = {
        "processor_type": "stripe",
        "parse_method": "pdf_vision",
        "aggregates": result.aggregates,
        "document_id": "11111111-1111-1111-1111-111111111111",
    }
    html = _render_partial(section)
    assert "PDF (vision)" in html


# ---------------------------------------------------------------------------
# Builder gating
# ---------------------------------------------------------------------------


def test_builder_returns_none_when_no_stripe_results() -> None:
    """``build_processor_section`` returns None when the merchant has
    no Stripe documents. Driving force of the dossier hiding the
    section on bank-only merchants."""
    assert build_processor_section(documents=[], stripe_results_by_doc=None) is None
    assert build_processor_section(documents=[], stripe_results_by_doc={}) is None


def test_builder_picks_first_doc_with_stripe_result() -> None:
    """When a Stripe result exists for one document, the builder returns
    a fully-populated section dict for that document."""

    # Minimal DocumentRow stand-ins — only ``id`` is read by the builder.
    # ``cast`` keeps mypy happy without weakening the production
    # function's typed contract.
    from typing import cast

    from aegis.storage import DocumentRow

    class _StubDoc:
        def __init__(self, doc_id: UUID) -> None:
            self.id = doc_id

    doc_a = _StubDoc(uuid4())
    doc_b = _StubDoc(uuid4())
    result = _make_stripe_result()

    section = build_processor_section(
        documents=cast(list[DocumentRow], [doc_a, doc_b]),
        stripe_results_by_doc={doc_a.id: result},
    )
    assert section is not None
    assert section["processor_type"] == "stripe"
    assert section["parse_method"] == "csv"
    assert section["document_id"] == str(doc_a.id)
    assert section["aggregates"] is result.aggregates
