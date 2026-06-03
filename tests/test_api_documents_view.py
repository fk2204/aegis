"""Tests for ``GET /api/documents/{document_id}/original`` (chunk C).

The view route is the only path that turns ciphertext back into a PDF
the operator can see. Coverage shape:

* Happy path: SSO present + ACL passes + storage_path populated +
  integrity OK → 200, application/pdf, audit row written, headers
  locked to private/no-store + nosniff + inline.
* 401: no SSO header.
* 403: SSO present but non-commerafunding.com → denied (acl_domain).
* 404: doc missing — no audit row (no subject).
* 404: doc exists but storage_path NULL → denied (no_storage_path).
* 500: ciphertext tampered (InvalidTag wrapped in
  CorruptCiphertextError) → integrity_failed (decrypt_invalid_tag).
* 500: SHA-256 mismatch with valid GCM tag → integrity_failed
  (sha256_mismatch). Separate audit action from the success path so
  an alert can fire on failure-only.
* Static invariant: the route source must not contain Supabase signed-
  URL or public-URL helpers (would let the browser cache the PDF and
  skip the audit on re-fetch).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aegis import storage_objects
from aegis.api.app import create_app
from aegis.api.deps import get_audit, get_repository, reset_dependency_caches
from aegis.audit import InMemoryAuditLog
from aegis.crypto import encrypt_pdf
from aegis.ops.operators import CF_ACCESS_EMAIL_HEADER
from aegis.storage import InMemoryDocumentRepository
from aegis.storage_objects import reset_backend_for_tests, upload

_PLAINTEXT_PDF = b"%PDF-1.7\n<<viewable bytes for the test>>\n%%EOF"
_VALID_EMAIL = "filip@commerafunding.com"
_OUTSIDE_EMAIL = "intruder@other-domain.com"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def docs() -> InMemoryDocumentRepository:
    return InMemoryDocumentRepository()


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def client(
    docs: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
) -> Iterator[TestClient]:
    """App wired with in-memory repo + audit + in-memory storage backend."""
    reset_dependency_caches()
    reset_backend_for_tests()
    app = create_app()
    app.dependency_overrides[get_repository] = lambda: docs
    app.dependency_overrides[get_audit] = lambda: audit
    with TestClient(app) as tc:
        yield tc
    app.dependency_overrides.clear()
    reset_dependency_caches()
    reset_backend_for_tests()


def _seed_stored_document(
    docs: InMemoryDocumentRepository,
    *,
    plaintext: bytes = _PLAINTEXT_PDF,
    storage_path: str | None = None,
    sha256_override: str | None = None,
    encryption_key_version: int = 1,
    original_filename: str = "stmt.pdf",
    merchant_id: UUID | None = None,
) -> tuple[UUID, str]:
    """Create a document row + upload its ciphertext.

    Returns ``(document_id, storage_path)``. The ciphertext at
    ``storage_path`` is a real encrypt_pdf output of ``plaintext`` under
    ``encryption_key_version``. ``sha256_override`` lets a caller seed
    a row whose ``sha256_original`` disagrees with the actual plaintext
    (drives the sha256_mismatch test).
    """
    file_hash = hashlib.sha256(plaintext).hexdigest()
    row = docs.create_document(
        file_hash=file_hash,
        byte_size=len(plaintext),
        original_filename=original_filename,
        uploaded_by="test",
        merchant_id=merchant_id,
    )
    if storage_path is None:
        storage_path = f"unassigned/documents/{row.id}.pdf.enc"
    ciphertext = encrypt_pdf(plaintext, key_version=encryption_key_version)
    upload(storage_path, ciphertext)
    docs.persist_storage_metadata(
        row.id,
        storage_path=storage_path,
        sha256_original=sha256_override if sha256_override is not None else file_hash,
        encryption_key_version=encryption_key_version,
        retention_until=datetime.now(UTC) + timedelta(days=7 * 365),
    )
    return row.id, storage_path


def _sso(email: str) -> dict[str, str]:
    return {CF_ACCESS_EMAIL_HEADER: email}


def _actions_for(audit: InMemoryAuditLog, document_id: UUID) -> list[str]:
    return [
        e["action"]
        for e in audit.entries
        if e["subject_type"] == "document" and e["subject_id"] == str(document_id)
    ]


def _last_entry_for(
    audit: InMemoryAuditLog, document_id: UUID, action: str
) -> dict[str, Any]:
    matches = [
        e
        for e in audit.entries
        if e["subject_type"] == "document"
        and e["subject_id"] == str(document_id)
        and e["action"] == action
    ]
    assert matches, f"no audit row found for {action}"
    return matches[-1]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_streams_plaintext_with_locked_headers(
    client: TestClient,
    docs: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
) -> None:
    """Valid SSO + ACL pass + integrity OK → 200, application/pdf, the
    exact plaintext bytes, audit row with the SSO email, and the
    private/no-store + nosniff + inline headers locked in."""
    merchant_id = uuid4()
    document_id, _ = _seed_stored_document(docs, merchant_id=merchant_id)

    response = client.get(
        f"/api/documents/{document_id}/original",
        headers={
            **_sso(_VALID_EMAIL),
            "user-agent": "TestClient/1.0",
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content == _PLAINTEXT_PDF
    # Headers — locked.
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    cd = response.headers["content-disposition"]
    assert cd.startswith("inline;"), cd
    assert 'filename="stmt.pdf"' in cd

    # Audit row written, with the SSO email + merchant_id + user_agent.
    success_entries = [
        e for e in audit.entries if e["action"] == "document.original_viewed"
    ]
    assert len(success_entries) == 1
    entry = success_entries[0]
    assert entry["actor"] == f"operator:{_VALID_EMAIL}"
    assert entry["actor_email"] == _VALID_EMAIL
    assert entry["subject_type"] == "document"
    assert entry["subject_id"] == str(document_id)
    assert entry["details"]["merchant_id"] == str(merchant_id)
    assert entry["details"]["user_agent"] == "TestClient/1.0"
    assert entry["details"]["encryption_key_version"] == 1


def test_happy_path_no_denied_or_integrity_failed_audit(
    client: TestClient,
    docs: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
) -> None:
    """The success path emits exactly ONE audit row (the
    document.original_viewed action) — no denied / integrity_failed
    sibling rows. Alerts that target the failure actions must not
    spuriously fire on a clean view."""
    document_id, _ = _seed_stored_document(docs)

    response = client.get(
        f"/api/documents/{document_id}/original",
        headers=_sso(_VALID_EMAIL),
    )

    assert response.status_code == 200
    actions = _actions_for(audit, document_id)
    assert actions == ["document.original_viewed"]


def test_happy_path_filename_sanitization_strips_injection_attempts(
    client: TestClient,
    docs: InMemoryDocumentRepository,
) -> None:
    """A malicious original_filename with CR/LF/quote/backslash MUST NOT
    let an attacker inject a second header. The route scrubs those
    bytes from the Content-Disposition filename slot."""
    document_id, _ = _seed_stored_document(
        docs,
        original_filename='evil"\r\nSet-Cookie: pwned=1\r\nx.pdf',
    )

    response = client.get(
        f"/api/documents/{document_id}/original",
        headers=_sso(_VALID_EMAIL),
    )

    assert response.status_code == 200
    cd = response.headers["content-disposition"]
    # CR/LF must not survive — that's how the header split would land.
    assert "\r" not in cd
    assert "\n" not in cd
    # The double-quote that closes the filename value must not be
    # forge-able via a payload-embedded quote.
    assert cd.count('"') == 2
    # And no Set-Cookie response header was injected.
    assert "set-cookie" not in {k.lower() for k in response.headers}


# ---------------------------------------------------------------------------
# Auth + ACL failure paths
# ---------------------------------------------------------------------------


def test_401_when_sso_header_missing(
    client: TestClient,
    docs: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
) -> None:
    """No cf-access-authenticated-user-email → 401, no audit row."""
    document_id, _ = _seed_stored_document(docs)

    response = client.get(f"/api/documents/{document_id}/original")

    assert response.status_code == 401
    # No subject-tied audit row should exist — the ACL never ran.
    assert _actions_for(audit, document_id) == []


def test_403_when_sso_email_outside_allowed_domain(
    client: TestClient,
    docs: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
) -> None:
    """SSO email outside @commerafunding.com → 403 + denied (acl_domain)."""
    document_id, _ = _seed_stored_document(docs)

    response = client.get(
        f"/api/documents/{document_id}/original",
        headers=_sso(_OUTSIDE_EMAIL),
    )

    assert response.status_code == 403
    entry = _last_entry_for(audit, document_id, "document.original_viewed_denied")
    assert entry["details"]["reason"] == "acl_domain"
    assert entry["actor"] == f"operator:{_OUTSIDE_EMAIL}"
    assert entry["actor_email"] == _OUTSIDE_EMAIL
    # No success row written.
    assert "document.original_viewed" not in _actions_for(audit, document_id)


# ---------------------------------------------------------------------------
# 404 paths
# ---------------------------------------------------------------------------


def test_404_when_document_missing(
    client: TestClient,
    audit: InMemoryAuditLog,
) -> None:
    """A random UUID with no row → 404, no audit row (no subject to
    attach the trace to)."""
    document_id = uuid4()

    response = client.get(
        f"/api/documents/{document_id}/original",
        headers=_sso(_VALID_EMAIL),
    )

    assert response.status_code == 404
    assert _actions_for(audit, document_id) == []


def test_404_when_storage_path_is_null(
    client: TestClient,
    docs: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
) -> None:
    """Document exists but never had its ciphertext persisted (legacy
    pre-chunk-B doc, or a worker quarantine outcome). Returns 404 +
    document.original_viewed_denied with reason=no_storage_path so the
    operator can see why the link didn't resolve."""
    row = docs.create_document(
        file_hash="a" * 64,
        byte_size=1024,
        original_filename="legacy.pdf",
        uploaded_by="test",
    )
    # NO persist_storage_metadata call → storage_path stays None.

    response = client.get(
        f"/api/documents/{row.id}/original",
        headers=_sso(_VALID_EMAIL),
    )

    assert response.status_code == 404
    entry = _last_entry_for(
        audit, row.id, "document.original_viewed_denied"
    )
    assert entry["details"]["reason"] == "no_storage_path"
    assert entry["actor_email"] == _VALID_EMAIL


# ---------------------------------------------------------------------------
# 500 integrity-failed paths
# ---------------------------------------------------------------------------


def test_500_when_ciphertext_tampered_decrypt_invalid_tag(
    client: TestClient,
    docs: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
) -> None:
    """Tampered ciphertext at the storage path → AES-GCM rejects the
    tag → CorruptCiphertextError → 500 + integrity_failed
    (reason=decrypt_invalid_tag)."""
    document_id, storage_path = _seed_stored_document(docs)
    # Overwrite ciphertext with garbage of the same shape (nonce+payload+tag).
    storage_objects.delete(storage_path)
    upload(storage_path, b"\x00" * 64)

    response = client.get(
        f"/api/documents/{document_id}/original",
        headers=_sso(_VALID_EMAIL),
    )

    assert response.status_code == 500
    entry = _last_entry_for(
        audit, document_id, "document.original_viewed_integrity_failed"
    )
    assert entry["details"]["reason"] == "decrypt_invalid_tag"
    assert entry["details"]["encryption_key_version"] == 1
    # Failure-only action; the success action is distinct.
    assert "document.original_viewed" not in _actions_for(audit, document_id)


def test_500_when_sha256_mismatch_with_valid_gcm_tag(
    client: TestClient,
    docs: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
) -> None:
    """Ciphertext decrypts cleanly (valid GCM tag) but the plaintext's
    SHA-256 disagrees with documents.sha256_original — typically a
    write-time bug. The route must refuse the response and audit
    integrity_failed with reason=sha256_mismatch — same action string
    as the InvalidTag case so the alert rule catches both."""
    bogus_sha = "f" * 64  # not the hash of _PLAINTEXT_PDF
    document_id, _ = _seed_stored_document(docs, sha256_override=bogus_sha)

    response = client.get(
        f"/api/documents/{document_id}/original",
        headers=_sso(_VALID_EMAIL),
    )

    assert response.status_code == 500
    entry = _last_entry_for(
        audit, document_id, "document.original_viewed_integrity_failed"
    )
    assert entry["details"]["reason"] == "sha256_mismatch"
    assert "document.original_viewed" not in _actions_for(audit, document_id)


# ---------------------------------------------------------------------------
# Static guard — chunk-A invariant must still cover this route's source
# ---------------------------------------------------------------------------


def test_route_source_contains_no_supabase_url_helpers() -> None:
    """``tests/test_security_invariants.py`` already scans all of
    ``src/aegis/`` for ``create_signed_url`` / ``get_public_url``. This
    test pins the same check to the new route file specifically so a
    chunk-C regression is obvious from the failure message."""
    route_path = (
        Path(__file__).resolve().parent.parent
        / "src" / "aegis" / "api" / "routes" / "documents.py"
    )
    text = route_path.read_text(encoding="utf-8")
    forbidden = ("create_signed_url", "createSignedUrl", "get_public_url", "getPublicUrl")
    hits = [term for term in forbidden if term in text]
    assert not hits, (
        f"chunk-C view route source must not call Supabase URL helpers; "
        f"found: {hits}"
    )
