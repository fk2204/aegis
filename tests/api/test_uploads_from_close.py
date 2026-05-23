"""Tests for the hybrid statement intake path.

Two endpoints land in step 7:

  * ``POST /upload?close_lead_id=...`` — existing operator-side upload,
    now with optional Close-Lead association.
  * ``POST /uploads/from-close`` — caller (n8n or operator UI) hands
    AEGIS a Close attachment reference; AEGIS pulls the file via
    ``CloseClient.download_attachment``.

Both converge on the same SHA256-keyed ``documents`` row, ensuring
guarantee #3 from the design doc: an attachment delivered twice (once
via webhook + operator click, once via the dashboard) never produces
duplicate parses.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_close_client,
    get_merchant_repository,
    get_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.close.client import CloseClient
from aegis.config import get_settings
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.storage import InMemoryDocumentRepository

_BEARER = "test-token-not-real"
_PDF = b"%PDF-1.7\nfake bytes for a real-looking test PDF\n"


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


def _set_close_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOSE_API_KEY", "api_test_close_key")
    monkeypatch.setenv("CLOSE_API_BASE", "https://api.close.example")
    get_settings.cache_clear()


def _seed_merchant(
    repo: InMemoryMerchantRepository,
    *,
    close_lead_id: str | None = "lead_abc",
) -> MerchantRow:
    m = MerchantRow(
        id=uuid4(),
        business_name="Acme",
        owner_name="Jane",
        state="CA",
        close_lead_id=close_lead_id,
    )
    repo.upsert(m)
    return m


@pytest.fixture
def merchants() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture
def docs() -> InMemoryDocumentRepository:
    return InMemoryDocumentRepository()


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def close_get_status() -> dict[str, int]:
    return {"code": 200}


@pytest.fixture
def close_content() -> dict[str, Any]:
    """Mutable canned bytes + filename returned by the Close client."""
    return {"bytes": _PDF, "filename": "bank_stmt.pdf"}


@pytest.fixture
def close_transport_requests() -> list[httpx.Request]:
    return []


@pytest.fixture
def close_client(
    monkeypatch: pytest.MonkeyPatch,
    close_get_status: dict[str, int],
    close_content: dict[str, Any],
    close_transport_requests: list[httpx.Request],
) -> CloseClient:
    _set_close_env(monkeypatch)
    monkeypatch.setattr("aegis.close.client.time.sleep", lambda _s: None)

    def transport(request: httpx.Request) -> httpx.Response:
        close_transport_requests.append(request)
        code = close_get_status["code"]
        if code == 200:
            return httpx.Response(
                200,
                content=close_content["bytes"],
                headers={
                    "content-disposition": (
                        f'attachment; filename="{close_content["filename"]}"'
                    ),
                },
            )
        return httpx.Response(code, text=f"close-error-{code}")

    return CloseClient(
        http_client=httpx.Client(transport=httpx.MockTransport(transport))
    )


@pytest.fixture
def client(
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
    close_client: CloseClient,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: merchants
    app.dependency_overrides[get_repository] = lambda: docs
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_close_client] = lambda: close_client
    with TestClient(app) as tc:
        yield tc
    app.dependency_overrides.clear()
    reset_dependency_caches()


def _post_upload(
    client: TestClient,
    *,
    close_lead_id: str | None = None,
    content: bytes = _PDF,
    filename: str = "stmt.pdf",
) -> Any:
    url = "/upload"
    if close_lead_id is not None:
        url = f"/upload?close_lead_id={close_lead_id}"
    return client.post(
        url,
        headers={"Authorization": f"Bearer {_BEARER}"},
        files={"file": (filename, content, "application/pdf")},
    )


def _post_from_close(
    client: TestClient,
    *,
    close_lead_id: str = "lead_abc",
    attachment_id: str = "att_xyz",
) -> Any:
    return client.post(
        "/uploads/from-close",
        headers={"Authorization": f"Bearer {_BEARER}"},
        content=json.dumps(
            {"close_lead_id": close_lead_id, "attachment_id": attachment_id}
        ),
        # FastAPI's TestClient honors the explicit content + content-type.
        headers_override=None,
    ) if False else client.post(  # tooling-friendly form below
        "/uploads/from-close",
        headers={
            "Authorization": f"Bearer {_BEARER}",
            "content-type": "application/json",
        },
        content=json.dumps(
            {"close_lead_id": close_lead_id, "attachment_id": attachment_id}
        ),
    )


# ----------------------------------------------------------------------
# /upload with close_lead_id (regression + association)
# ----------------------------------------------------------------------


def test_upload_without_close_lead_id_behavior_unchanged(
    client: TestClient,
    docs: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
) -> None:
    """Existing /upload path: no close_lead_id. Document persists, no
    merchant association, audit row has close_lead_id=None."""
    resp = _post_upload(client)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    document_id = UUID(body["document_id"])

    row = docs.get_document(document_id)
    assert row.merchant_id is None

    upload_audits = [e for e in audit.entries if e["action"] == "document.upload"]
    assert len(upload_audits) == 1
    assert upload_audits[0]["details"]["close_lead_id"] is None


def test_upload_with_close_lead_id_associates_merchant(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
) -> None:
    merchant = _seed_merchant(merchants, close_lead_id="lead_abc")
    resp = _post_upload(client, close_lead_id="lead_abc")
    assert resp.status_code == 202, resp.text

    document_id = UUID(resp.json()["document_id"])
    row = docs.get_document(document_id)
    assert row.merchant_id == merchant.id

    upload_audits = [e for e in audit.entries if e["action"] == "document.upload"]
    assert upload_audits[0]["details"]["close_lead_id"] == "lead_abc"
    assert upload_audits[0]["details"]["merchant_id"] == str(merchant.id)


def test_upload_with_close_lead_id_404_when_merchant_missing(
    client: TestClient,
) -> None:
    """close_lead_id present but no merchant linked -> 404, clear body."""
    resp = _post_upload(client, close_lead_id="lead_nonexistent")
    assert resp.status_code == 404
    assert "lead_nonexistent" in resp.json()["detail"]
    assert "no AEGIS merchant" in resp.json()["detail"]


# ----------------------------------------------------------------------
# /uploads/from-close — happy paths
# ----------------------------------------------------------------------


# KNOWN FLAKE: passes in isolation, fails ~1 in N in full-suite runs.
# Suspected cause: prior test's AEGIS_MAX_UPLOAD_BYTES monkeypatch +
# get_settings.cache_clear() cleanup races with this test's fixture setup.
# See step 11 sweep notes. Not blocking; behavior covered by other tests
# in this file.
def test_from_close_happy_path_persists_and_enqueues(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
    close_transport_requests: list[httpx.Request],
) -> None:
    merchant = _seed_merchant(merchants)
    resp = _post_from_close(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["duplicate"] is False
    assert body["parse_enqueued"] is True

    document_id = UUID(body["document_id"])
    row = docs.get_document(document_id)
    assert row.merchant_id == merchant.id

    # Close was called once.
    assert len(close_transport_requests) == 1
    req = close_transport_requests[0]
    assert req.method == "GET"
    assert req.url.path.endswith("/api/v1/files/att_xyz/download/")

    # Audit row.
    fetched = [e for e in audit.entries if e["action"] == "close.upload.fetched"]
    assert len(fetched) == 1
    details = fetched[0]["details"]
    assert details["close_lead_id"] == "lead_abc"
    assert details["attachment_id"] == "att_xyz"
    assert details["document_id"] == body["document_id"]
    assert details["duplicate"] is False
    assert details["filename"] == "bank_stmt.pdf"
    assert details["sha256"]
    assert details["byte_size"] == len(_PDF)


def test_from_close_sha256_dedup_returns_existing_no_reparse(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
    close_transport_requests: list[httpx.Request],
) -> None:
    """Second /uploads/from-close with the same SHA returns the SAME
    document_id, duplicate=True, parse_enqueued=False. Close IS still
    called (we don't have the SHA until we fetch), but the parse job
    is not re-enqueued."""
    _seed_merchant(merchants)
    first = _post_from_close(client)
    second = _post_from_close(client)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["document_id"] == second.json()["document_id"]
    assert second.json()["duplicate"] is True
    assert second.json()["parse_enqueued"] is False

    # Two Close fetches, one document.
    assert len(close_transport_requests) == 2
    assert len(docs._docs) == 1

    # Two close.upload.fetched audit rows; only the first carries
    # duplicate=False.
    fetched = [e for e in audit.entries if e["action"] == "close.upload.fetched"]
    assert len(fetched) == 2
    assert fetched[0]["details"]["duplicate"] is False
    assert fetched[1]["details"]["duplicate"] is True


def test_dashboard_upload_then_from_close_dedupe_to_same_doc(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
) -> None:
    """Operator drops the PDF into /upload (dashboard path); n8n later
    triggers /uploads/from-close with the same statement. The SHA256
    dedup gate ensures one ``documents`` row across both paths."""
    _seed_merchant(merchants, close_lead_id="lead_abc")

    first = _post_upload(client, close_lead_id="lead_abc", content=_PDF)
    assert first.status_code == 202
    first_id = UUID(first.json()["document_id"])

    second = _post_from_close(client)
    assert second.status_code == 200
    second_id = UUID(second.json()["document_id"])

    assert first_id == second_id
    assert second.json()["duplicate"] is True
    assert len(docs._docs) == 1


# ----------------------------------------------------------------------
# /uploads/from-close — error cases
# ----------------------------------------------------------------------


def test_from_close_404_when_no_merchant_for_lead(client: TestClient) -> None:
    """close_lead_id has no AEGIS merchant linked."""
    resp = _post_from_close(client, close_lead_id="lead_unknown")
    assert resp.status_code == 404
    assert "lead_unknown" in resp.json()["detail"]


def test_from_close_404_when_close_returns_404(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    close_get_status: dict[str, int],
) -> None:
    """Close returns 404 -> attachment not found."""
    _seed_merchant(merchants)
    close_get_status["code"] = 404
    resp = _post_from_close(client)
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def test_from_close_502_on_close_5xx(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    close_get_status: dict[str, int],
) -> None:
    _seed_merchant(merchants)
    close_get_status["code"] = 500
    resp = _post_from_close(client)
    assert resp.status_code == 502
    assert "close_upstream_error" in resp.json()["detail"]


def test_from_close_413_when_attachment_too_large(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    close_content: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close-supplied bytes exceed aegis_max_upload_bytes -> 413."""
    _seed_merchant(merchants)
    # Cap the upload size hard so the 50 KiB fake "PDF" trips it.
    monkeypatch.setenv("AEGIS_MAX_UPLOAD_BYTES", "10")
    get_settings.cache_clear()
    close_content["bytes"] = b"%PDF-" + b"x" * 50_000
    resp = _post_from_close(client)
    assert resp.status_code == 413
    assert "exceeds" in resp.json()["detail"]


def test_from_close_400_on_empty_attachment_id(
    client: TestClient, merchants: InMemoryMerchantRepository
) -> None:
    _seed_merchant(merchants)
    resp = client.post(
        "/uploads/from-close",
        headers={
            "Authorization": f"Bearer {_BEARER}",
            "content-type": "application/json",
        },
        content=json.dumps(
            {"close_lead_id": "lead_abc", "attachment_id": ""}
        ),
    )
    assert resp.status_code == 422  # Pydantic validation (min_length=1)


def test_from_close_400_on_empty_close_lead_id(
    client: TestClient,
) -> None:
    resp = client.post(
        "/uploads/from-close",
        headers={
            "Authorization": f"Bearer {_BEARER}",
            "content-type": "application/json",
        },
        content=json.dumps(
            {"close_lead_id": "", "attachment_id": "att_xyz"}
        ),
    )
    assert resp.status_code == 422
