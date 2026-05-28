"""Worker job tests — parse_document + process_close_attachments.

The worker delegates the heavy lifting to ``run_pipeline`` and to the
repository / audit. We inject all three so the test exercises the
worker's contract (audit start, audit complete, persist, cleanup) without
running the real LLM.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from aegis.audit import InMemoryAuditLog
from aegis.close.client import CloseAttachment, CloseError
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.parser.pipeline import PipelineResult
from aegis.storage import InMemoryDocumentRepository
from aegis.workers import parse_document, process_close_attachments
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
# Cost-tracking wrap (mp Phase 11 #2): when the worker receives a real
# BedrockClient (production), it gets wrapped in CostTrackingBedrockClient
# pinned to this job's document_id. Fake LLMs (other tests above) are NOT
# wrapped — the isinstance guard skips them.
# ---------------------------------------------------------------------------


async def test_parse_document_wraps_bedrock_client_with_cost_tracking(
    monkeypatch: pytest.MonkeyPatch, fake_pdf: Path
) -> None:
    """When the injected llm is a real BedrockClient, it gets wrapped so
    every Bedrock call inside the pipeline writes a bedrock.usage audit
    row tagged with this job's document_id."""
    from aegis.llm import BedrockClient
    from aegis.ops.cost_tracking import CostTrackingBedrockClient

    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()
    row = repo.create_document(
        file_hash="j" * 64, byte_size=fake_pdf.stat().st_size, original_filename="f.pdf"
    )

    # A minimal BedrockClient stand-in — production path is `isinstance(llm,
    # BedrockClient)`, so we sneak past it with BedrockClient.__new__ to
    # avoid the boto3 cred chain the real __init__ touches.
    fake_bedrock = BedrockClient.__new__(BedrockClient)

    captured_llm: list[object] = []

    def fake_run_pipeline(
        _path: str, llm: object, today: date | None = None
    ) -> PipelineResult:
        captured_llm.append(llm)
        return _make_pipeline_result()

    monkeypatch.setattr("aegis.workers.run_pipeline", fake_run_pipeline)

    await parse_document(
        {"repository": repo, "audit": audit, "llm": fake_bedrock},
        str(row.id),
        str(fake_pdf),
    )

    assert len(captured_llm) == 1
    wrapped = captured_llm[0]
    assert isinstance(wrapped, CostTrackingBedrockClient)
    # The wrap pins this job's document_id so bedrock.usage rows carry it.
    assert wrapped._document_id == row.id


async def test_parse_document_does_not_wrap_non_bedrock_llm(
    monkeypatch: pytest.MonkeyPatch, fake_pdf: Path
) -> None:
    """Fake LLMClients (object(), test doubles) MUST NOT be wrapped — the
    wrapper re-issues calls through `inner._client.messages` which fakes
    don't expose. The isinstance check is the guard."""
    from aegis.ops.cost_tracking import CostTrackingBedrockClient

    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()
    row = repo.create_document(
        file_hash="k" * 64, byte_size=fake_pdf.stat().st_size, original_filename="f.pdf"
    )

    captured_llm: list[object] = []

    def fake_run_pipeline(
        _path: str, llm: object, today: date | None = None
    ) -> PipelineResult:
        captured_llm.append(llm)
        return _make_pipeline_result()

    monkeypatch.setattr("aegis.workers.run_pipeline", fake_run_pipeline)

    fake_llm = object()
    await parse_document(
        {"repository": repo, "audit": audit, "llm": fake_llm},
        str(row.id),
        str(fake_pdf),
    )

    assert captured_llm[0] is fake_llm
    assert not isinstance(captured_llm[0], CostTrackingBedrockClient)


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


# =====================================================================
# process_close_attachments — Close attachment auto-flow orchestrator.
# =====================================================================


_MIN_PDF = b"%PDF-1.4\nfake-statement"


class _FakeCloseClient:
    """Records calls; returns canned attachments + per-attachment bytes."""

    def __init__(
        self,
        *,
        attachments: list[CloseAttachment] | None = None,
        list_error: Exception | None = None,
        per_attachment_bytes: dict[str, bytes] | None = None,
        per_attachment_filename: dict[str, str] | None = None,
        per_attachment_error: dict[str, Exception] | None = None,
    ) -> None:
        self._attachments = attachments or []
        self._list_error = list_error
        self._bytes = per_attachment_bytes or {}
        self._names = per_attachment_filename or {}
        self._errors = per_attachment_error or {}
        self.list_calls: list[str] = []
        self.download_calls: list[str] = []

    def list_lead_attachments(self, lead_id: str) -> list[CloseAttachment]:
        self.list_calls.append(lead_id)
        if self._list_error is not None:
            raise self._list_error
        return list(self._attachments)

    def download_attachment(
        self, attachment: CloseAttachment
    ) -> tuple[bytes, str]:
        self.download_calls.append(attachment.id)
        if attachment.id in self._errors:
            raise self._errors[attachment.id]
        body = self._bytes.get(
            attachment.id, _MIN_PDF + attachment.id.encode()
        )
        name = self._names.get(attachment.id, attachment.name)
        return body, name


def _make_merchant(close_lead_id: str = "lead_abc") -> MerchantRow:
    return MerchantRow(
        id=uuid4(),
        business_name="Test Merchant",
        owner_name="Test Owner",
        state="CA",
        close_lead_id=close_lead_id,
    )


def _attachment(id_: str, name: str) -> CloseAttachment:
    return CloseAttachment(id=id_, name=name)


def _orchestrator_ctx(
    *,
    merchants: InMemoryMerchantRepository,
    repo: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
    close: _FakeCloseClient,
    upload_dir: Path,
    enqueue_calls: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Build an arq ctx for the orchestrator with everything mocked."""
    calls = enqueue_calls if enqueue_calls is not None else []

    async def _capture(document_id: UUID, pdf_path: str) -> None:
        calls.append((str(document_id), pdf_path))

    return {
        "merchants": merchants,
        "repository": repo,
        "audit": audit,
        "close_client": close,
        "enqueue_parse": _capture,
        # persist_pdf_upload writes the temp PDF under
        # settings.aegis_upload_dir. The test fixture below sets that
        # env var; this ctx key is just for symmetry / future use.
        "upload_dir": str(upload_dir),
    }


@pytest.fixture
def upload_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point persist_pdf_upload at a per-test upload dir so the on-disk
    PDFs don't pollute the system temp."""
    d = tmp_path / "uploads"
    d.mkdir()
    monkeypatch.setenv("AEGIS_UPLOAD_DIR", str(d))
    from aegis.config import get_settings as _gs
    _gs.cache_clear()
    return d


async def test_orchestrator_happy_path_filters_and_persists(
    upload_dir: Path,
) -> None:
    merchant = _make_merchant("lead_abc")
    merchants = InMemoryMerchantRepository()
    merchants.upsert(merchant)
    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()

    close = _FakeCloseClient(
        attachments=[
            _attachment("file_1", "April_bank_statement.pdf"),
            _attachment("file_2", "drivers_license.jpg"),
            _attachment("file_3", "May_estmt_chase.pdf"),
        ],
    )
    enqueued: list[tuple[str, str]] = []
    ctx = _orchestrator_ctx(
        merchants=merchants, repo=repo, audit=audit, close=close,
        upload_dir=upload_dir, enqueue_calls=enqueued,
    )

    summary = await process_close_attachments(ctx, "lead_abc", "webhook")

    assert summary["total"] == 3
    assert summary["fetched"] == 2
    assert summary["skipped"] == 1
    assert summary["failed"] == 0
    assert summary["duplicates"] == 0
    assert summary["capped"] is False

    actions = [e["action"] for e in audit.entries]
    assert actions.count("close.attachment.fetched") == 2
    assert actions.count("close.attachment.skipped") == 1
    assert "close.orchestration.complete" in actions
    # Two parse jobs enqueued — one per non-duplicate statement.
    assert len(enqueued) == 2


async def test_orchestrator_no_merchant_audits_and_returns(
    upload_dir: Path,
) -> None:
    merchants = InMemoryMerchantRepository()  # empty
    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()
    close = _FakeCloseClient(attachments=[])
    ctx = _orchestrator_ctx(
        merchants=merchants, repo=repo, audit=audit, close=close,
        upload_dir=upload_dir,
    )

    summary = await process_close_attachments(ctx, "lead_missing", "webhook")

    assert summary["total"] == 0
    assert summary["fetched"] == 0
    assert close.list_calls == [], "list_lead_attachments must not run when no merchant"
    actions = [e["action"] for e in audit.entries]
    assert actions == ["close.orchestration.no_merchant"]


async def test_orchestrator_list_failed_raises_after_audit(
    upload_dir: Path,
) -> None:
    merchant = _make_merchant("lead_abc")
    merchants = InMemoryMerchantRepository()
    merchants.upsert(merchant)
    audit = InMemoryAuditLog()
    close = _FakeCloseClient(
        list_error=CloseError("close 502 transient", status_code=502),
    )
    ctx = _orchestrator_ctx(
        merchants=merchants,
        repo=InMemoryDocumentRepository(),
        audit=audit,
        close=close,
        upload_dir=upload_dir,
    )

    with pytest.raises(CloseError):
        await process_close_attachments(ctx, "lead_abc", "webhook")

    actions = [e["action"] for e in audit.entries]
    assert "close.orchestration.list_failed" in actions
    # complete audit row must NOT fire when listing fails
    assert "close.orchestration.complete" not in actions


async def test_orchestrator_per_attachment_fetch_failure_isolated(
    upload_dir: Path,
) -> None:
    """One CloseError on a download must not abort the batch."""
    merchant = _make_merchant("lead_abc")
    merchants = InMemoryMerchantRepository()
    merchants.upsert(merchant)
    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()

    close = _FakeCloseClient(
        attachments=[
            _attachment("good_1", "stmt_jan.pdf"),
            _attachment("bad_mid", "stmt_feb.pdf"),
            _attachment("good_2", "stmt_mar.pdf"),
        ],
        per_attachment_error={
            "bad_mid": CloseError("close 502 transient", status_code=502),
        },
    )
    ctx = _orchestrator_ctx(
        merchants=merchants, repo=repo, audit=audit, close=close,
        upload_dir=upload_dir,
    )

    summary = await process_close_attachments(ctx, "lead_abc", "webhook")

    assert summary["fetched"] == 2
    assert summary["failed"] == 1
    actions = [e["action"] for e in audit.entries]
    assert actions.count("close.attachment.fetched") == 2
    assert actions.count("close.attachment.fetch_failed") == 1
    assert "close.orchestration.complete" in actions


async def test_orchestrator_dedup_via_sha256(upload_dir: Path) -> None:
    """Same bytes on two attachments → second is duplicate, no second
    parse enqueue."""
    merchant = _make_merchant("lead_abc")
    merchants = InMemoryMerchantRepository()
    merchants.upsert(merchant)
    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()

    same = _MIN_PDF + b"-identical"
    close = _FakeCloseClient(
        attachments=[
            _attachment("file_a", "stmt_apr.pdf"),
            _attachment("file_b", "stmt_apr_resent.pdf"),
        ],
        per_attachment_bytes={"file_a": same, "file_b": same},
    )
    enqueued: list[tuple[str, str]] = []
    ctx = _orchestrator_ctx(
        merchants=merchants, repo=repo, audit=audit, close=close,
        upload_dir=upload_dir, enqueue_calls=enqueued,
    )

    summary = await process_close_attachments(ctx, "lead_abc", "webhook")

    assert summary["fetched"] == 2
    assert summary["duplicates"] == 1
    # The SECOND attachment's persist_pdf_upload returned
    # duplicate_of_existing=True — no parse enqueue.
    assert len(enqueued) == 1


async def test_orchestrator_caps_at_15_without_override(
    upload_dir: Path,
) -> None:
    merchant = _make_merchant("lead_abc")
    merchants = InMemoryMerchantRepository()
    merchants.upsert(merchant)
    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()

    atts = [_attachment(f"f_{i:02d}", f"stmt_{i:02d}.pdf") for i in range(17)]
    close = _FakeCloseClient(attachments=atts)
    ctx = _orchestrator_ctx(
        merchants=merchants, repo=repo, audit=audit, close=close,
        upload_dir=upload_dir,
    )

    summary = await process_close_attachments(ctx, "lead_abc", "webhook")

    assert summary["total"] == 17
    assert summary["fetched"] == 15
    assert summary["capped"] is True
    # Only 15 downloads happened — the deferred 2 never touched Close.
    assert len(close.download_calls) == 15
    actions = [e["action"] for e in audit.entries]
    assert "close.orchestration.capped" in actions
    assert "close.orchestration.warn_high_attachment_count" in actions
    capped_row = next(
        e for e in audit.entries if e["action"] == "close.orchestration.capped"
    )
    assert capped_row["details"]["deferred_count"] == 2


async def test_orchestrator_override_cap_processes_all(
    upload_dir: Path,
) -> None:
    merchant = _make_merchant("lead_abc")
    merchants = InMemoryMerchantRepository()
    merchants.upsert(merchant)
    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()

    atts = [_attachment(f"f_{i:02d}", f"stmt_{i:02d}.pdf") for i in range(17)]
    close = _FakeCloseClient(attachments=atts)
    ctx = _orchestrator_ctx(
        merchants=merchants, repo=repo, audit=audit, close=close,
        upload_dir=upload_dir,
    )

    summary = await process_close_attachments(
        ctx, "lead_abc", "rescan", override_cap=True,
    )

    assert summary["fetched"] == 17
    assert summary["capped"] is False
    assert summary["override_cap"] is True
    assert len(close.download_calls) == 17
    actions = [e["action"] for e in audit.entries]
    assert "close.orchestration.cap_overridden" in actions
    assert "close.orchestration.capped" not in actions


async def test_orchestrator_trigger_label_propagates(
    upload_dir: Path,
) -> None:
    merchant = _make_merchant("lead_abc")
    merchants = InMemoryMerchantRepository()
    merchants.upsert(merchant)
    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()
    close = _FakeCloseClient(
        attachments=[_attachment("f1", "stmt.pdf")],
    )
    ctx = _orchestrator_ctx(
        merchants=merchants, repo=repo, audit=audit, close=close,
        upload_dir=upload_dir,
    )

    summary = await process_close_attachments(
        ctx, "lead_abc", "rescan", actor_email="filip@commerafunding.com",
    )

    assert summary["trigger"] == "rescan"
    fetched = next(
        e for e in audit.entries if e["action"] == "close.attachment.fetched"
    )
    assert fetched["details"]["trigger"] == "rescan"
    assert fetched["actor_email"] == "filip@commerafunding.com"
