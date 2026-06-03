"""Worker job tests — parse_document + process_close_attachments.

The worker delegates the heavy lifting to ``run_pipeline`` and to the
repository / audit. We inject all three so the test exercises the
worker's contract (audit start, audit complete, persist, cleanup) without
running the real LLM.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from aegis import storage_objects
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


def _real_file_hash(path: Path) -> str:
    """SHA-256 hex of the file bytes — what production code records
    in ``documents.file_hash`` at upload time. Used in success-path
    tests so the chunk-B sha256-divergence check doesn't silently
    route them through the dead-letter path (which would still
    delete the plaintext, passing ``assert not fake_pdf.exists()`` —
    but for the wrong reason)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def chunk_b_storage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[Path]:
    """Point ``aegis_upload_dir`` at ``tmp_path`` so chunk-B's
    ``quarantine/`` and ``quarantine/dead-letter/`` writes land where
    the test can inspect them. Resets the storage_objects backend
    between tests so a previous test's in-memory blobs don't bleed
    over."""
    monkeypatch.setenv("AEGIS_UPLOAD_DIR", str(tmp_path))
    from aegis.config import get_settings
    get_settings.cache_clear()
    storage_objects.reset_backend_for_tests()
    yield tmp_path
    storage_objects.reset_backend_for_tests()
    get_settings.cache_clear()


async def test_parse_document_persists_and_audits(
    monkeypatch: pytest.MonkeyPatch, fake_pdf: Path
) -> None:
    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()
    row = repo.create_document(
        file_hash=_real_file_hash(fake_pdf),
        byte_size=fake_pdf.stat().st_size,
        original_filename="f.pdf",
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


async def test_parse_document_cancelled_marks_error_unlinks_and_reraises(
    monkeypatch: pytest.MonkeyPatch, fake_pdf: Path
) -> None:
    """``arq``'s job-timeout path raises ``CancelledError`` into the
    worker via ``asyncio.wait_for``. On Python 3.12+ ``CancelledError``
    inherits from ``BaseException``, not ``Exception``, so the bare
    ``except Exception`` does NOT catch it — without an explicit
    handler the row stayed at ``parse_status="pending"`` AND the
    plaintext PDF survived on disk forever, breaking the day-one
    plaintext-at-rest invariant (verified 2026-06-03 against doc
    3df15a58 and the lingering uploads in
    ``/var/lib/aegis/uploads/``).

    Contract this test pins:
      1. ``CancelledError`` is RE-RAISED (arq must still see the
         cancellation).
      2. ``parse_status`` lands on ``"error"`` with a non-empty
         ``error_detail`` mentioning the timeout reason.
      3. The plaintext PDF is unlinked.
      4. A ``document.parse.error`` audit row exists with
         ``reason="timeout"`` and ``error="CancelledError"``.
    """
    import asyncio as _asyncio

    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()
    row = repo.create_document(
        file_hash="c" * 64, byte_size=10, original_filename="cancel.pdf"
    )

    def cancel_during_pipeline(
        _path: str, _llm: object, today: date | None = None
    ) -> PipelineResult:
        raise _asyncio.CancelledError()

    monkeypatch.setattr("aegis.workers.run_pipeline", cancel_during_pipeline)

    with pytest.raises(_asyncio.CancelledError):
        await parse_document(
            {"repository": repo, "audit": audit, "llm": object()},
            str(row.id),
            str(fake_pdf),
        )

    assert not fake_pdf.exists(), "PDF must be deleted on cancellation"

    after = repo.get_document(row.id)
    assert after.parse_status == "error"
    assert after.error_detail is not None
    assert "CancelledError" in after.error_detail
    assert "timeout" in after.error_detail.lower()

    error_rows = [e for e in audit.entries if e["action"] == "document.parse.error"]
    assert len(error_rows) == 1
    details = error_rows[0]["details"]
    assert details["error"] == "CancelledError"
    assert details["reason"] == "timeout"
    # The success-side audit was NEVER written.
    actions = [e["action"] for e in audit.entries]
    assert "document.parse.complete" not in actions
    # parse.start fired BEFORE the cancellation — preserved for the trace.
    assert "document.parse.start" in actions


async def test_run_processor_branch_cancelled_marks_error_unlinks_and_reraises(
    monkeypatch: pytest.MonkeyPatch, fake_pdf: Path
) -> None:
    """Same CancelledError-handling contract as the bank-statement path,
    but for the Stripe/Square branch (``_run_processor_branch``). The
    processor pipeline is also LLM-bound and was equally exposed to the
    Python-3.12 BaseException semantics. Both branches must lose the
    plaintext on cancellation, mark the row, audit, and re-raise."""
    import asyncio as _asyncio

    from aegis.parser.processor.detect import ProcessorDetection

    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()
    row = repo.create_document(
        file_hash=_real_file_hash(fake_pdf),
        byte_size=fake_pdf.stat().st_size,
        original_filename="stripe_cancel.pdf",
    )

    # Route to the processor branch by faking detection.
    monkeypatch.setattr(
        "aegis.workers.detect_processor",
        lambda _path: ProcessorDetection(
            brand="stripe", stripe_hits=3, square_hits=0
        ),
    )

    def cancel_during_processor_pipeline(
        _path: object, _bytes: bytes, _llm: object, *, brand: str
    ) -> object:
        raise _asyncio.CancelledError()

    monkeypatch.setattr(
        "aegis.workers.run_processor_pipeline", cancel_during_processor_pipeline
    )

    with pytest.raises(_asyncio.CancelledError):
        await parse_document(
            {"repository": repo, "audit": audit, "llm": object()},
            str(row.id),
            str(fake_pdf),
        )

    assert not fake_pdf.exists(), "PDF must be deleted on processor cancellation"

    after = repo.get_document(row.id)
    assert after.parse_status == "error"
    assert after.error_detail is not None
    assert "CancelledError" in after.error_detail

    error_rows = [e for e in audit.entries if e["action"] == "document.parse.error"]
    assert len(error_rows) == 1
    details = error_rows[0]["details"]
    assert details["error"] == "CancelledError"
    assert details["reason"] == "timeout"
    assert details["brand"] == "stripe"
    actions = [e["action"] for e in audit.entries]
    assert "document.parse.processor_complete" not in actions


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
        file_hash=_real_file_hash(fake_pdf),
        byte_size=fake_pdf.stat().st_size,
        original_filename="f.pdf",
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
        file_hash=_real_file_hash(fake_pdf),
        byte_size=fake_pdf.stat().st_size,
        original_filename="f.pdf",
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
        file_hash=_real_file_hash(fake_pdf),
        byte_size=fake_pdf.stat().st_size,
        original_filename="s.pdf",
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
# PDF retention chunk B — encrypted-storage step (worker)
# =====================================================================
#
# Tests the new control-flow after parse-complete:
#   plaintext → sha256(...) → encrypt → upload → persist → audit → unlink.
#
# Three outcome classes covered:
#   SUCCESS    — happy path; storage_path populated, audit
#                document.original_stored, plaintext unlinked.
#   TRANSIENT  — upload raises (network/5xx); ciphertext quarantined
#                (NOT plaintext); audit document.original_storage_failed
#                reason=upload_failed outcome=quarantine; plaintext
#                unlinked; NO exception propagates.
#   TERMINAL   — sha256(plaintext) != documents.file_hash; dead-letter
#                instead of quarantine (so reconcile NEVER picks it
#                up); audit reason=sha256_divergence outcome=dead_letter.
#
# Plus: persist_storage_metadata atomicity (single call writes all
# four storage columns together) and the existing 4 PDF-deleted
# assertions in this file confirm the SUCCESS-path local-cleanup
# contract still holds after chunk B.


async def test_chunk_b_success_populates_storage_columns(
    monkeypatch: pytest.MonkeyPatch,
    fake_pdf: Path,
    chunk_b_storage: Path,
) -> None:
    """Success path locks down all four contract details:
      * documents.storage_path is populated
      * documents.sha256_original == sha256(plaintext_bytes)
      * documents.encryption_key_version == 1 (the conftest test key)
      * documents.retention_until ≈ NOW() + 7yr (±2s tolerance)
      * audit document.original_stored row written
      * plaintext at pdf_path is deleted
    """
    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()
    row = repo.create_document(
        file_hash=_real_file_hash(fake_pdf),
        byte_size=fake_pdf.stat().st_size,
        original_filename="f.pdf",
    )
    plaintext_bytes = fake_pdf.read_bytes()
    expected_sha = hashlib.sha256(plaintext_bytes).hexdigest()

    fake_result = _make_pipeline_result()
    monkeypatch.setattr(
        "aegis.workers.run_pipeline",
        lambda *_a, **_kw: fake_result,
    )

    await parse_document(
        {"repository": repo, "audit": audit, "llm": object()},
        str(row.id),
        str(fake_pdf),
    )

    persisted = repo.get_document(row.id)
    assert persisted.storage_path is not None
    assert persisted.storage_path.endswith(f"{row.id}.pdf.enc")
    assert persisted.sha256_original == expected_sha
    assert persisted.encryption_key_version == 1
    assert persisted.retention_until is not None
    expected_retention = datetime.now(UTC) + timedelta(days=365 * 7)
    delta = abs(
        (persisted.retention_until - expected_retention).total_seconds()
    )
    assert delta < 2.0, f"retention_until off by {delta}s"

    actions = [e["action"] for e in audit.entries]
    assert "document.original_stored" in actions
    success_event = next(
        e for e in audit.entries
        if e["action"] == "document.original_stored"
    )
    assert success_event["details"]["encryption_key_version"] == 1
    assert success_event["details"]["byte_size"] == len(plaintext_bytes)

    # Local plaintext deleted on success — day-one cleanup rule
    # preserved when the storage step succeeds.
    assert not fake_pdf.exists()


async def test_chunk_b_upload_failure_quarantines_ciphertext(
    monkeypatch: pytest.MonkeyPatch,
    fake_pdf: Path,
    chunk_b_storage: Path,
) -> None:
    """Transient upload failure (storage_objects.upload raises) →
    ciphertext written to quarantine/{doc_id}.pdf.enc + .meta
    sidecar; plaintext at pdf_path is DELETED; documents.storage_path
    stays NULL; audit document.original_storage_failed row written
    with reason=upload_failed and outcome=quarantine; NO exception
    propagates to the worker's caller (parse already succeeded)."""
    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()
    row = repo.create_document(
        file_hash=_real_file_hash(fake_pdf),
        byte_size=fake_pdf.stat().st_size,
        original_filename="f.pdf",
    )
    plaintext_bytes = fake_pdf.read_bytes()

    fake_result = _make_pipeline_result()
    monkeypatch.setattr(
        "aegis.workers.run_pipeline",
        lambda *_a, **_kw: fake_result,
    )

    def boom_upload(path: str, data: bytes) -> None:
        raise storage_objects.StorageError("simulated transient failure")

    monkeypatch.setattr("aegis.storage_objects.upload", boom_upload)

    # MUST NOT raise — storage failure is best-effort
    out = await parse_document(
        {"repository": repo, "audit": audit, "llm": object()},
        str(row.id),
        str(fake_pdf),
    )
    assert out["parse_status"] == "proceed"

    # Storage NOT recorded on documents row
    persisted = repo.get_document(row.id)
    assert persisted.storage_path is None
    assert persisted.sha256_original is None
    assert persisted.encryption_key_version is None
    assert persisted.retention_until is None

    # Audit row written with the expected reason + outcome
    storage_failed_rows = [
        e for e in audit.entries
        if e["action"] == "document.original_storage_failed"
    ]
    assert len(storage_failed_rows) == 1
    failure = storage_failed_rows[0]
    assert failure["details"]["reason"] == "upload_failed"
    assert failure["details"]["outcome"] == "quarantine"
    assert failure["details"]["error_type"] == "StorageError"

    # Plaintext at pdf_path is DELETED — no plaintext at rest past parse
    assert not fake_pdf.exists()

    # Quarantine holds the recovery artifacts: ciphertext blob + meta
    qdir = chunk_b_storage / "quarantine"
    blob_path = qdir / f"{row.id}.pdf.enc"
    meta_path = qdir / f"{row.id}.meta.json"
    assert blob_path.exists(), "quarantine ciphertext must be present"
    assert meta_path.exists(), "quarantine meta sidecar must be present"

    # The quarantined bytes are CIPHERTEXT, not plaintext — assertion
    # locks down the load-bearing contract that we never reintroduce
    # plaintext-at-rest in the failure path.
    quarantined = blob_path.read_bytes()
    assert quarantined != plaintext_bytes, (
        "quarantine MUST hold ciphertext, never plaintext (the whole "
        "project exists because plaintext-at-rest is the failure mode "
        "we're closing — see docs/PDF_RETENTION_DESIGN.md §1)"
    )
    assert len(quarantined) >= 28  # AES-GCM nonce(12) + tag(16) min

    # Meta sidecar carries everything reconcile needs to retry
    # without re-reading plaintext or re-encrypting
    import json as _json
    meta = _json.loads(meta_path.read_text())
    assert meta["reason"] == "upload_failed"
    assert meta["sha256_original"] == hashlib.sha256(plaintext_bytes).hexdigest()
    assert meta["encryption_key_version"] == 1
    assert meta["storage_path"].endswith(f"{row.id}.pdf.enc")
    assert "retention_until" in meta


async def test_chunk_b_sha256_divergence_dead_letters(
    monkeypatch: pytest.MonkeyPatch,
    fake_pdf: Path,
    chunk_b_storage: Path,
) -> None:
    """sha256(plaintext) != documents.file_hash → TERMINAL.

    Artifacts go to quarantine/dead-letter/ — NOT to quarantine/
    where the reconcile cron retries. Without this separation,
    reconcile would infinite-loop: re-read divergent plaintext,
    re-hash, re-detect divergence, re-quarantine, repeat.

    This test explicitly asserts the negative: no file lands in
    quarantine/{doc_id}.pdf.enc — reconcile's non-recursive scan
    of quarantine/ won't pick it up.
    """
    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()
    # Create the doc with a WRONG file_hash so the post-parse
    # sha256 check detects divergence
    row = repo.create_document(
        file_hash="0" * 64,  # not the real hash of fake_pdf
        byte_size=fake_pdf.stat().st_size,
        original_filename="f.pdf",
    )

    fake_result = _make_pipeline_result()
    monkeypatch.setattr(
        "aegis.workers.run_pipeline",
        lambda *_a, **_kw: fake_result,
    )

    await parse_document(
        {"repository": repo, "audit": audit, "llm": object()},
        str(row.id),
        str(fake_pdf),
    )

    # Audit reason + outcome
    storage_failed_rows = [
        e for e in audit.entries
        if e["action"] == "document.original_storage_failed"
    ]
    assert len(storage_failed_rows) == 1
    failure = storage_failed_rows[0]
    assert failure["details"]["reason"] == "sha256_divergence"
    assert failure["details"]["outcome"] == "dead_letter"

    # Dead-letter has the .meta sidecar (forensic record)
    dlq_dir = chunk_b_storage / "quarantine" / "dead-letter"
    dlq_meta = dlq_dir / f"{row.id}.meta.json"
    assert dlq_meta.exists()

    # CRITICAL: nothing in quarantine/ that reconcile would pick up
    qdir = chunk_b_storage / "quarantine"
    retry_blob = qdir / f"{row.id}.pdf.enc"
    retry_meta = qdir / f"{row.id}.meta.json"
    assert not retry_blob.exists(), (
        "sha256_divergence is TERMINAL — must NOT land in quarantine/ "
        "where reconcile would loop forever"
    )
    assert not retry_meta.exists()

    # storage_path stays NULL
    persisted = repo.get_document(row.id)
    assert persisted.storage_path is None

    # Plaintext deleted
    assert not fake_pdf.exists()


async def test_chunk_b_quarantine_dir_is_mode_0700(
    monkeypatch: pytest.MonkeyPatch,
    fake_pdf: Path,
    chunk_b_storage: Path,
) -> None:
    """Q4(a) — quarantine/ and quarantine/dead-letter/ must be mode
    0700 (owner-only). The ``.meta`` sidecars carry storage-layout
    metadata (storage_path, sha256_original, key_version) — must NOT
    be world-readable. Skipped on Windows where chmod semantics
    differ; the prod box is Linux which is what matters.
    """
    import sys
    if sys.platform.startswith("win"):
        pytest.skip("POSIX-mode test; Windows chmod is permissive")

    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()
    row = repo.create_document(
        file_hash=_real_file_hash(fake_pdf),
        byte_size=fake_pdf.stat().st_size,
        original_filename="f.pdf",
    )
    fake_result = _make_pipeline_result()
    monkeypatch.setattr(
        "aegis.workers.run_pipeline",
        lambda *_a, **_kw: fake_result,
    )

    def boom_upload(path: str, data: bytes) -> None:
        raise storage_objects.StorageError("force quarantine path")

    monkeypatch.setattr("aegis.storage_objects.upload", boom_upload)

    await parse_document(
        {"repository": repo, "audit": audit, "llm": object()},
        str(row.id),
        str(fake_pdf),
    )

    qdir = chunk_b_storage / "quarantine"
    qmode = qdir.stat().st_mode & 0o777
    assert qmode == 0o700, (
        f"quarantine/ must be mode 0700 (got 0o{qmode:o}) — .meta "
        "sidecars carry storage-layout metadata that must not be "
        "world-readable"
    )


async def test_chunk_b_dead_letter_dir_is_mode_0700(
    monkeypatch: pytest.MonkeyPatch,
    fake_pdf: Path,
    chunk_b_storage: Path,
) -> None:
    """Q4(a) — quarantine/dead-letter/ must also be mode 0700.
    Triggered via sha256 divergence (the canonical terminal path)."""
    import sys
    if sys.platform.startswith("win"):
        pytest.skip("POSIX-mode test; Windows chmod is permissive")

    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()
    row = repo.create_document(
        file_hash="0" * 64,  # forces divergence → dead-letter
        byte_size=fake_pdf.stat().st_size,
        original_filename="f.pdf",
    )
    fake_result = _make_pipeline_result()
    monkeypatch.setattr(
        "aegis.workers.run_pipeline",
        lambda *_a, **_kw: fake_result,
    )

    await parse_document(
        {"repository": repo, "audit": audit, "llm": object()},
        str(row.id),
        str(fake_pdf),
    )

    dlq = chunk_b_storage / "quarantine" / "dead-letter"
    dmode = dlq.stat().st_mode & 0o777
    assert dmode == 0o700, (
        f"quarantine/dead-letter/ must be mode 0700 (got 0o{dmode:o})"
    )
    # And the parent quarantine/ which dead-letter sits under
    qdir = chunk_b_storage / "quarantine"
    qmode = qdir.stat().st_mode & 0o777
    assert qmode == 0o700, (
        f"parent quarantine/ must also be 0700 (got 0o{qmode:o}); "
        "0700 on dead-letter/ alone is moot if traversal of the parent "
        "is permissive"
    )


async def test_chunk_b_fifth_path_unmapped_exception_unlinks_and_audits(
    monkeypatch: pytest.MonkeyPatch,
    fake_pdf: Path,
    chunk_b_storage: Path,
) -> None:
    """Q1 — the 5th-path catch-all must:
      * log CRITICAL (journal-priority surfaces it via -p err / -p crit)
      * write audit ``document.original_storage_failed`` with
        ``outcome=best_effort_cleanup`` (NOT ``no_cleanup`` — the old
        name was wrong; we DO attempt cleanup)
      * best-effort delete the plaintext (preserve disk-hygiene rule)

    Forced by patching ``encrypt_pdf`` to raise something OTHER than
    ``CryptoConfigError`` (which has its own explicit handler) —
    here a ``RuntimeError`` that the explicit branches above don't
    catch, falling through to the 5th-path catch-all.
    """
    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()
    row = repo.create_document(
        file_hash=_real_file_hash(fake_pdf),
        byte_size=fake_pdf.stat().st_size,
        original_filename="f.pdf",
    )
    fake_result = _make_pipeline_result()
    monkeypatch.setattr(
        "aegis.workers.run_pipeline",
        lambda *_a, **_kw: fake_result,
    )

    def boom_encrypt(plaintext: bytes, *, key_version: int) -> bytes:
        # Not CryptoConfigError — falls through to the 5th-path catch
        raise RuntimeError("unmapped encryption failure")

    monkeypatch.setattr("aegis.workers.encrypt_pdf", boom_encrypt)

    out = await parse_document(
        {"repository": repo, "audit": audit, "llm": object()},
        str(row.id),
        str(fake_pdf),
    )

    # Parse already persisted before storage step; result still returned
    assert out["parse_status"] == "proceed"

    # Audit row written with reason=unknown outcome=best_effort_cleanup
    storage_failed_rows = [
        e for e in audit.entries
        if e["action"] == "document.original_storage_failed"
    ]
    assert len(storage_failed_rows) == 1
    failure = storage_failed_rows[0]
    assert failure["details"]["reason"] == "unknown"
    assert failure["details"]["outcome"] == "best_effort_cleanup", (
        "outcome must be best_effort_cleanup — the 5th path attempts "
        "_safe_unlink at the end. The old 'no_cleanup' label promised "
        "plaintext-at-rest, which is the failure mode we're closing."
    )
    assert failure["details"]["error_type"] == "RuntimeError"

    # Plaintext deleted — disk-hygiene rule preserved on the 5th path
    assert not fake_pdf.exists()

    # storage_path stays NULL (no successful upload happened)
    persisted = repo.get_document(row.id)
    assert persisted.storage_path is None


async def test_chunk_b_write_failure_preserves_plaintext(
    monkeypatch: pytest.MonkeyPatch,
    fake_pdf: Path,
    chunk_b_storage: Path,
) -> None:
    """Q1 refinement — if the recovery-artifact write fails (disk
    full, IO error, permissions), the plaintext on disk is THE LAST
    COPY of this document's bytes. Best-effort unlink in this case
    would be data loss: we couldn't write the encrypted recovery
    artifact AND we deleted the only remaining copy.

    The 5th path discriminates by ``isinstance(exc, OSError)``:
    OSError → preserve plaintext; anything else → best-effort
    unlink. Operator frees space / fixes permissions, then
    ``scripts/_reparse_one.py`` retries from the preserved plaintext.

    Test forces the path by:
      1. Triggering the upload-failure branch (so
         ``_write_quarantine`` gets called)
      2. Patching ``_write_quarantine`` itself to raise OSError
         (ENOSPC — "No space left on device")
      3. The OSError escapes the upload-fail inner except,
         propagates to the outer 5th-path catch
    """
    import errno

    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()
    row = repo.create_document(
        file_hash=_real_file_hash(fake_pdf),
        byte_size=fake_pdf.stat().st_size,
        original_filename="f.pdf",
    )
    fake_result = _make_pipeline_result()
    monkeypatch.setattr(
        "aegis.workers.run_pipeline",
        lambda *_a, **_kw: fake_result,
    )

    # Trigger the upload-fail branch (which calls _write_quarantine)
    def boom_upload(path: str, data: bytes) -> None:
        raise storage_objects.StorageError("transient upload failure")
    monkeypatch.setattr("aegis.storage_objects.upload", boom_upload)

    # Force _write_quarantine to ENOSPC — escapes the inner except
    def enospc_write(
        document_id: UUID, *, ciphertext: bytes, meta: dict[str, Any]
    ) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr("aegis.workers._write_quarantine", enospc_write)

    out = await parse_document(
        {"repository": repo, "audit": audit, "llm": object()},
        str(row.id),
        str(fake_pdf),
    )
    # Parse already completed before the storage step
    assert out["parse_status"] == "proceed"

    # CRITICAL: plaintext is PRESERVED — operator-recovery copy
    assert fake_pdf.exists(), (
        "OSError during recovery-artifact write must NOT delete the "
        "plaintext — disk-full + delete = data loss. The plaintext on "
        "disk is the LAST copy after the quarantine write failed; "
        "preserving it lets the operator free space + reprocess."
    )

    # Audit signals the discrimination — write_failure_preserved
    storage_failed_rows = [
        e for e in audit.entries
        if e["action"] == "document.original_storage_failed"
    ]
    assert len(storage_failed_rows) == 1
    failure = storage_failed_rows[0]
    assert failure["details"]["reason"] == "unknown"
    assert failure["details"]["outcome"] == "write_failure_preserved", (
        "outcome must signal write-failure preservation, NOT "
        "best_effort_cleanup (which would have deleted the plaintext)"
    )
    assert failure["details"]["error_type"] == "OSError"

    # storage_path stays NULL (upload + quarantine both failed)
    persisted = repo.get_document(row.id)
    assert persisted.storage_path is None


async def test_chunk_b_persist_storage_metadata_is_atomic(
    monkeypatch: pytest.MonkeyPatch,
    fake_pdf: Path,
    chunk_b_storage: Path,
) -> None:
    """persist_storage_metadata is the single atomic UPDATE writing
    all four storage columns together. The partial index
    ``idx_documents_retention_until WHERE storage_path IS NOT NULL``
    + the ``no-retained-forever-anomaly`` db_check depend on the
    four fields moving as one.

    This test spies on the repo method and asserts:
      * exactly ONE call (not three separate UPDATEs)
      * all four columns are arguments to that one call
    """
    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()
    row = repo.create_document(
        file_hash=_real_file_hash(fake_pdf),
        byte_size=fake_pdf.stat().st_size,
        original_filename="f.pdf",
    )

    original = repo.persist_storage_metadata
    call_log: list[dict[str, Any]] = []

    def spy_persist(document_id: UUID, **kwargs: Any) -> None:
        call_log.append({"document_id": document_id, **kwargs})
        original(document_id, **kwargs)

    monkeypatch.setattr(repo, "persist_storage_metadata", spy_persist)

    fake_result = _make_pipeline_result()
    monkeypatch.setattr(
        "aegis.workers.run_pipeline",
        lambda *_a, **_kw: fake_result,
    )

    await parse_document(
        {"repository": repo, "audit": audit, "llm": object()},
        str(row.id),
        str(fake_pdf),
    )

    # Single call — atomicity contract
    assert len(call_log) == 1, (
        f"persist_storage_metadata MUST be a single atomic call; "
        f"observed {len(call_log)} invocations"
    )
    call = call_log[0]
    # All four columns argued together
    assert "storage_path" in call
    assert "sha256_original" in call
    assert "encryption_key_version" in call
    assert "retention_until" in call
    # Sanity on values
    assert call["storage_path"].endswith(f"{row.id}.pdf.enc")
    assert call["encryption_key_version"] == 1


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

    def download_attachment(self, attachment_id: str) -> tuple[bytes, str]:
        self.download_calls.append(attachment_id)
        if attachment_id in self._errors:
            raise self._errors[attachment_id]
        body = self._bytes.get(attachment_id, _MIN_PDF + attachment_id.encode())
        name = self._names.get(attachment_id, f"{attachment_id}.pdf")
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
