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
