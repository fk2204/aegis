"""InMemoryPdfStoreRepository tests.

Covers the contract both backends must honour:

* ``store`` round-trips: bytes in -> Pydantic row out with the right
  ``key_version`` + ``sha256_plaintext`` + ``byte_size_plaintext``.
* ``fetch_plaintext`` returns the original bytes verbatim.
* ``fetch_plaintext`` raises ``PdfStoreNotFoundError`` for an unknown
  document_id.
* ``fetch_plaintext`` raises ``PdfStoreIntegrityError`` when the
  stored ``sha256_plaintext`` disagrees with the decrypted plaintext.
* Idempotent overwrite: a second ``store`` call on the same
  ``document_id`` replaces the prior ciphertext.
* Storing empty plaintext is rejected.
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest

from aegis.pdf_store.repository import (
    InMemoryPdfStoreRepository,
    PdfStoreIntegrityError,
    PdfStoreNotFoundError,
    PdfStoreWriteError,
)


def _make_pdf_bytes(seed: int = 1) -> bytes:
    """Build a deterministic byte string. Not a real PDF — the repo
    treats the bytes as opaque, so any non-empty payload works.
    """
    body = f"%PDF-1.7 fake-{seed:04d}\n".encode("ascii")
    # Pad so we exercise non-trivial-size ciphertext.
    return body + b"\x00" * 512


def test_store_round_trips_plaintext_and_populates_row_metadata() -> None:
    repo = InMemoryPdfStoreRepository()
    document_id = uuid4()
    plaintext = _make_pdf_bytes(seed=42)

    row = repo.store(document_id=document_id, plaintext=plaintext)

    assert row.document_id == document_id
    assert row.byte_size_plaintext == len(plaintext)
    assert row.sha256_plaintext == hashlib.sha256(plaintext).hexdigest()
    assert row.key_version == 1  # tests/conftest.py pins PDF_ENCRYPTION_KEYS_CURRENT=1
    # InMemoryPdfStoreRepository keeps the blob inline as BYTEA — both
    # ciphertext and nonce must be populated for legacy-mode reads.
    # mig-062 split the SupabasePdfStoreRepository to Storage-mode
    # (storage_path), but InMemory stays inline.
    assert row.nonce is not None and len(row.nonce) == 12
    # ciphertext = nonce(12) || ct || tag(16) — minimum 28 bytes, plus
    # at least one plaintext byte.
    assert row.ciphertext is not None and len(row.ciphertext) >= 28 + len(plaintext)


def test_fetch_plaintext_returns_original_bytes() -> None:
    repo = InMemoryPdfStoreRepository()
    document_id = uuid4()
    plaintext = _make_pdf_bytes(seed=7)
    repo.store(document_id=document_id, plaintext=plaintext)

    fetched = repo.fetch_plaintext(document_id)

    assert fetched == plaintext


def test_fetch_plaintext_raises_for_unknown_document_id() -> None:
    repo = InMemoryPdfStoreRepository()
    with pytest.raises(PdfStoreNotFoundError):
        repo.fetch_plaintext(uuid4())


def test_fetch_plaintext_raises_integrity_error_on_sha_mismatch() -> None:
    """Belt-and-suspenders SHA-256 verification — see CLAUDE.md PDF
    retention rule "Integrity: SHA-256 of plaintext... checked on every
    read". Manually tamper the stored row's hash and confirm the
    repository surfaces an integrity error rather than serving the
    decrypted bytes silently.
    """
    repo = InMemoryPdfStoreRepository()
    document_id = uuid4()
    row = repo.store(document_id=document_id, plaintext=_make_pdf_bytes(seed=3))

    # Mutate the row in place to simulate Postgres-side corruption of
    # the ``sha256_plaintext`` column without breaking the AES-GCM
    # ciphertext. Pydantic ``validate_assignment=True`` enforces the
    # 64-char shape; replacing with a different valid hex digest
    # produces a hash that won't match the real plaintext.
    bogus_hex = hashlib.sha256(b"different bytes").hexdigest()
    row.sha256_plaintext = bogus_hex

    with pytest.raises(PdfStoreIntegrityError) as ei:
        repo.fetch_plaintext(document_id)
    assert "sha256 mismatch" in str(ei.value)


def test_store_is_idempotent_overwrites_prior_ciphertext() -> None:
    """A re-parse of the same document_id (or a worker retry) must
    cleanly replace the prior seal — the latest plaintext wins so a
    document the operator re-uploaded never silently serves the stale
    bytes.
    """
    repo = InMemoryPdfStoreRepository()
    document_id = uuid4()
    first = _make_pdf_bytes(seed=1)
    second = _make_pdf_bytes(seed=2)

    repo.store(document_id=document_id, plaintext=first)
    repo.store(document_id=document_id, plaintext=second)

    assert repo.fetch_plaintext(document_id) == second


def test_store_rejects_empty_plaintext() -> None:
    """A zero-byte PDF would also fail the migration-060 CHECK
    (``byte_size_plaintext > 0``). Surface the failure as a typed
    Python error before the round-trip to give the worker a cleaner
    fault to handle.
    """
    repo = InMemoryPdfStoreRepository()
    with pytest.raises(PdfStoreWriteError):
        repo.store(document_id=uuid4(), plaintext=b"")
