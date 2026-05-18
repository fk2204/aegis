"""Worker job tests — parse_document.

The worker delegates the heavy lifting to ``run_pipeline`` and to the
repository / audit. We inject all three so the test exercises the
worker's contract (audit start, audit complete, persist, cleanup) without
running the real LLM.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from aegis.audit import InMemoryAuditLog
from aegis.parser.pipeline import PipelineResult
from aegis.storage import InMemoryDocumentRepository
from aegis.workers import parse_document
from tests.test_storage import _make_pipeline_result


@pytest.fixture
def fake_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "f.pdf"
    p.write_bytes(b"%PDF-1.4\nfake")
    return p


async def test_parse_document_persists_and_audits(
    monkeypatch: pytest.MonkeyPatch, fake_pdf: Path
) -> None:
    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()
    row = repo.create_document(
        file_hash="h" * 64, byte_size=fake_pdf.stat().st_size, original_filename="f.pdf"
    )

    fake_result = _make_pipeline_result()

    def fake_run_pipeline(
        _path: str, _llm: object, today: date | None = None
    ) -> PipelineResult:
        return fake_result

    monkeypatch.setattr("aegis.workers.run_pipeline", fake_run_pipeline)

    out = await parse_document(
        {"repository": repo, "audit": audit, "llm": object()},
        str(row.id),
        str(fake_pdf),
    )

    assert out["parse_status"] == "proceed"
    assert out["fraud_score"] == fake_result.fraud_score
    assert not fake_pdf.exists(), "PDF must be deleted after parse"

    actions = [e["action"] for e in audit.entries]
    assert "document.parse.start" in actions
    assert "document.parse.complete" in actions

    persisted = repo.get_document(row.id)
    assert persisted.parse_status == "proceed"
    assert repo.get_analysis(row.id) is not None


async def test_parse_document_failure_records_error_and_unlinks(
    monkeypatch: pytest.MonkeyPatch, fake_pdf: Path
) -> None:
    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()
    row = repo.create_document(
        file_hash="i" * 64, byte_size=10, original_filename="f.pdf"
    )

    def boom(
        _path: str, _llm: object, today: date | None = None
    ) -> PipelineResult:
        raise RuntimeError("parser blew up")

    monkeypatch.setattr("aegis.workers.run_pipeline", boom)

    with pytest.raises(RuntimeError, match="parser blew up"):
        await parse_document(
            {"repository": repo, "audit": audit, "llm": object()},
            str(row.id),
            str(fake_pdf),
        )

    assert not fake_pdf.exists(), "PDF must be deleted even on parser failure"

    actions = [e["action"] for e in audit.entries]
    assert "document.parse.start" in actions
    assert "document.parse.error" in actions

    after = repo.get_document(row.id)
    assert after.parse_status == "error"
    assert after.error_detail and "parser blew up" in after.error_detail


async def test_parse_document_unknown_id_raises_and_unlinks(
    monkeypatch: pytest.MonkeyPatch, fake_pdf: Path
) -> None:
    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()

    from aegis.storage import DocumentNotFoundError

    with pytest.raises(DocumentNotFoundError):
        await parse_document(
            {"repository": repo, "audit": audit, "llm": object()},
            str(uuid4()),
            str(fake_pdf),
        )

    assert not fake_pdf.exists()


# ---------------------------------------------------------------------------
# Processor branch (mp Phase 6.6 / Stage 2C): detection routes Stripe and
# Square PDFs to ``run_processor_pipeline`` instead of ``run_pipeline``.
# Worker-side dispatch is what makes upload → parse a single coherent flow.
# ---------------------------------------------------------------------------


async def test_parse_document_routes_processor_pdf_to_processor_pipeline(
    monkeypatch: pytest.MonkeyPatch, fake_pdf: Path
) -> None:
    """Detection says 'stripe' → run_pipeline is NEVER called; the
    processor pipeline runs instead and the worker audits
    ``document.parse.processor_complete`` with the aggregates."""
    from dataclasses import dataclass, field
    from decimal import Decimal

    from aegis.parser.processor.detect import ProcessorDetection
    from aegis.parser.processor.validate import ProcessorValidationResult

    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()
    row = repo.create_document(
        file_hash="p" * 64, byte_size=fake_pdf.stat().st_size, original_filename="s.pdf"
    )

    # Force detection to "stripe" regardless of the PDF contents.
    monkeypatch.setattr(
        "aegis.workers.detect_processor",
        lambda _path: ProcessorDetection(
            brand="stripe", stripe_hits=3, square_hits=0
        ),
    )

    @dataclass
    class _FakeSourcedMoney:
        value: Decimal

    @dataclass
    class _FakeSourcedInt:
        value: int

    @dataclass
    class _FakeAggregates:
        gross_volume: _FakeSourcedMoney
        refunds_total: _FakeSourcedMoney
        chargebacks_total: _FakeSourcedMoney
        fees_total: _FakeSourcedMoney
        payouts_total: _FakeSourcedMoney
        net_revenue: _FakeSourcedMoney
        transaction_count: _FakeSourcedInt
        chargeback_ratio: Decimal

    @dataclass
    class _FakeProcessorPipelineResult:
        parse_status: str
        brand: str
        extraction: object | None
        validation: ProcessorValidationResult
        aggregates: _FakeAggregates | None
        flags: list[str] = field(default_factory=list)

    def fake_processor_pipeline(
        _path: object, _bytes: bytes, _llm: object, *, brand: str
    ) -> _FakeProcessorPipelineResult:
        return _FakeProcessorPipelineResult(
            parse_status="proceed",
            brand=brand,
            extraction=None,
            validation=ProcessorValidationResult(passed=True),
            aggregates=_FakeAggregates(
                gross_volume=_FakeSourcedMoney(value=Decimal("10000.00")),
                refunds_total=_FakeSourcedMoney(value=Decimal("0.00")),
                chargebacks_total=_FakeSourcedMoney(value=Decimal("0.00")),
                fees_total=_FakeSourcedMoney(value=Decimal("290.00")),
                payouts_total=_FakeSourcedMoney(value=Decimal("9710.00")),
                net_revenue=_FakeSourcedMoney(value=Decimal("9710.00")),
                transaction_count=_FakeSourcedInt(value=30),
                chargeback_ratio=Decimal("0"),
            ),
        )

    # Sentinel: if the bank pipeline runs we want a clear failure.
    def must_not_be_called(*_args: object, **_kw: object) -> object:
        raise AssertionError("bank run_pipeline was called on a processor PDF")

    monkeypatch.setattr("aegis.workers.run_processor_pipeline", fake_processor_pipeline)
    monkeypatch.setattr("aegis.workers.run_pipeline", must_not_be_called)

    out = await parse_document(
        {"repository": repo, "audit": audit, "llm": object()},
        str(row.id),
        str(fake_pdf),
    )

    assert out["parse_status"] == "proceed"
    assert not fake_pdf.exists(), "PDF must be deleted after parse"

    actions = [e["action"] for e in audit.entries]
    assert "document.parse.start" in actions
    assert "document.parse.processor_complete" in actions
    processor_event = next(
        e for e in audit.entries if e["action"] == "document.parse.processor_complete"
    )
    assert processor_event["details"]["brand"] == "stripe"
    # PII masking keeps numeric strings; the gross_volume field name is not PII.
    assert processor_event["details"]["gross_volume"] == "10000.00"


async def test_parse_document_ambiguous_processor_routes_to_manual_review(
    monkeypatch: pytest.MonkeyPatch, fake_pdf: Path
) -> None:
    """Both brands detected → manual_review, error audited, PDF deleted.
    The parser must NOT guess between Stripe and Square."""
    from aegis.parser.processor.detect import ProcessorDetection

    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()
    row = repo.create_document(
        file_hash="a" * 64, byte_size=fake_pdf.stat().st_size, original_filename="x.pdf"
    )

    monkeypatch.setattr(
        "aegis.workers.detect_processor",
        lambda _path: ProcessorDetection(
            brand="ambiguous", stripe_hits=3, square_hits=3
        ),
    )

    out = await parse_document(
        {"repository": repo, "audit": audit, "llm": object()},
        str(row.id),
        str(fake_pdf),
    )

    assert out["parse_status"] == "manual_review"
    assert not fake_pdf.exists()
    actions = [e["action"] for e in audit.entries]
    assert "document.parse.error" in actions
    err_event = next(
        e for e in audit.entries if e["action"] == "document.parse.error"
    )
    assert err_event["details"]["error"] == "AmbiguousProcessor"
