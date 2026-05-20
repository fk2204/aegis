"""POST /upload route tests.

Use TestClient over the real FastAPI app, with a fresh in-memory repo
injected via dependency_overrides. We don't run the worker here — we
just assert the upload pipeline (validate, hash, dedupe, persist,
audit, enqueue) does its job.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import get_audit, get_repository, reset_dependency_caches
from aegis.audit import InMemoryAuditLog
from aegis.storage import InMemoryDocumentRepository

PDF_HEADER = b"%PDF-1.4\n%fake bytes\n"


@pytest.fixture
def repo() -> InMemoryDocumentRepository:
    return InMemoryDocumentRepository()


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def client(
    repo: InMemoryDocumentRepository, audit: InMemoryAuditLog
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_audit] = lambda: audit
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def _post_pdf(
    client: TestClient, body: bytes, filename: str = "stmt.pdf"
) -> Any:
    return client.post(
        "/upload",
        headers={"Authorization": "Bearer test-token-not-real"},
        files={"file": (filename, body, "application/pdf")},
    )


def test_upload_requires_bearer_token(
    repo: InMemoryDocumentRepository, audit: InMemoryAuditLog
) -> None:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_audit] = lambda: audit
    with TestClient(app) as c:
        resp = c.post(
            "/upload",
            files={"file": ("a.pdf", PDF_HEADER, "application/pdf")},
        )
    assert resp.status_code == 401


def test_upload_happy_path_returns_202(
    client: TestClient,
    repo: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
) -> None:
    resp = _post_pdf(client, PDF_HEADER + b"more bytes")
    assert resp.status_code == 202
    body = resp.json()
    assert body["parse_status"] == "pending"
    assert body["duplicate_of_existing"] is False

    # Repository now holds the row + hash dedupe works.
    assert len(repo._docs) == 1

    # Audit recorded the upload.
    actions = [e["action"] for e in audit.entries]
    assert "document.upload" in actions


def test_upload_records_actor_email_from_cf_access_header(
    client: TestClient,
    repo: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
) -> None:
    """CF-Access-Authenticated-User-Email -> audit_log.actor_email.

    The operator identity flowing through Cloudflare Access lands on
    every audit row from this request. Locks the wiring of
    `resolve_operator_email` into the upload handler (mp Phase 11 #8
    follow-up).
    """
    resp = client.post(
        "/upload",
        headers={
            "Authorization": "Bearer test-token-not-real",
            "CF-Access-Authenticated-User-Email": "fkozina92@gmail.com",
        },
        files={"file": ("stmt.pdf", PDF_HEADER + b"with-email", "application/pdf")},
    )
    assert resp.status_code == 202

    upload_rows = [e for e in audit.entries if e["action"] == "document.upload"]
    assert len(upload_rows) == 1
    assert upload_rows[0]["actor_email"] == "fkozina92@gmail.com"


def test_upload_actor_email_none_without_cf_access_header(
    client: TestClient,
    audit: InMemoryAuditLog,
) -> None:
    """Local/dev requests (no CF-Access header) -> actor_email=None.

    Backward compatibility: existing bearer-only tooling that doesn't
    traverse Access stays exactly as it was.
    """
    resp = _post_pdf(client, PDF_HEADER + b"no-cf-header")
    assert resp.status_code == 202

    upload_rows = [e for e in audit.entries if e["action"] == "document.upload"]
    assert len(upload_rows) == 1
    assert upload_rows[0]["actor_email"] is None


def test_upload_dedupes_identical_bytes(
    client: TestClient, repo: InMemoryDocumentRepository
) -> None:
    payload = PDF_HEADER + b"identical"
    first = _post_pdf(client, payload).json()
    second = _post_pdf(client, payload).json()
    assert first["document_id"] == second["document_id"]
    assert second["duplicate_of_existing"] is True
    assert len(repo._docs) == 1


def test_upload_rejects_non_pdf_magic(client: TestClient) -> None:
    resp = _post_pdf(client, b"not a pdf at all")
    assert resp.status_code == 415


def test_upload_rejects_empty_body(client: TestClient) -> None:
    resp = _post_pdf(client, b"")
    assert resp.status_code == 400


def test_upload_writes_uuid_filename_not_user_supplied(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Point upload_dir at a fresh tmp so we can inspect the written name.
    from aegis.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("AEGIS_UPLOAD_DIR", str(tmp_path))
    settings = get_settings()
    assert settings.aegis_upload_dir == tmp_path

    resp = _post_pdf(client, PDF_HEADER + b"abc", filename="../../etc/passwd.pdf")
    assert resp.status_code == 202

    written = list(tmp_path.iterdir())
    assert len(written) == 1
    name = written[0].name
    # 32 hex + ".pdf" — never includes "passwd" or path separators.
    assert name.endswith(".pdf")
    assert "passwd" not in name and "/" not in name and "\\" not in name


def test_upload_pending_jobs_recorded_when_no_pool(
    client: TestClient, repo: InMemoryDocumentRepository
) -> None:
    resp = _post_pdf(client, PDF_HEADER + b"abc")
    assert resp.status_code == 202
    pending = client.app.state.pending_jobs  # type: ignore[attr-defined]
    assert len(pending) == 1
    assert pending[0]["document_id"] == resp.json()["document_id"]
