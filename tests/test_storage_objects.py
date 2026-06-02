"""Tests for ``aegis.storage_objects`` — Supabase Storage helper.

Chunk A of the PDF retention redesign. Focused on:

* ``upload`` / ``download`` / ``delete`` happy-path roundtrip (in-mem)
* ``delete`` idempotent on already-gone path
* ``confirm_absent`` semantics: True on 404, False on present
* error mapping: backend raises → ``StorageError`` for the worker /
  view route to catch uniformly
* bucket-private assertion at startup (no-op for in-mem; real backend
  raises on public bucket)
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from aegis import storage_objects
from aegis.storage_objects import (
    StorageError,
    confirm_absent,
    delete,
    download,
    reset_backend_for_tests,
    upload,
)


@pytest.fixture(autouse=True)
def _force_memory_backend(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force the in-memory backend for every test. Real Supabase
    backend tests would need a separate fixture with mocked HTTP."""
    reset_backend_for_tests()

    class _S:
        aegis_storage_backend = "memory"
        aegis_document_bucket = "documents-test"

    monkeypatch.setattr("aegis.storage_objects.get_settings", lambda: _S())
    yield
    reset_backend_for_tests()


def test_upload_download_roundtrip() -> None:
    """Trivial round-trip — the contract callers rely on."""
    upload("merchants/abc/documents/xyz.pdf.enc", b"opaque ciphertext bytes")
    assert download("merchants/abc/documents/xyz.pdf.enc") == (
        b"opaque ciphertext bytes"
    )


def test_download_missing_path_raises_storage_error() -> None:
    """A 404 from the backend → StorageError. The worker / view route
    catches this uniformly across backends."""
    with pytest.raises(StorageError):
        download("never/uploaded.pdf.enc")


def test_delete_then_download_404s() -> None:
    upload("a/b.pdf.enc", b"data")
    delete("a/b.pdf.enc")
    with pytest.raises(StorageError):
        download("a/b.pdf.enc")


def test_delete_idempotent_on_already_gone() -> None:
    """``delete`` MUST NOT raise on a path that's already 404. The
    retention sweep (chunk E) retries failed deletes; this idempotency
    is what makes retries safe."""
    delete("never/uploaded.pdf.enc")  # no raise
    delete("never/uploaded.pdf.enc")  # still no raise


def test_confirm_absent_returns_true_for_missing_path() -> None:
    assert confirm_absent("never/uploaded.pdf.enc") is True


def test_confirm_absent_returns_false_for_present_path() -> None:
    upload("a/b.pdf.enc", b"data")
    assert confirm_absent("a/b.pdf.enc") is False


def test_confirm_absent_returns_true_after_delete() -> None:
    """The chunk-E sweep relies on this exact sequence: upload, delete,
    confirm_absent → True. Without it, the sweep can't write the
    ``deletion_confirmed: true`` audit row honestly."""
    upload("a/b.pdf.enc", b"data")
    delete("a/b.pdf.enc")
    assert confirm_absent("a/b.pdf.enc") is True


def test_assert_bucket_private_at_startup_noop_for_memory_backend() -> None:
    """The in-memory backend doesn't actually enforce ACLs — but the
    boot guard call must not raise on a memory backend (otherwise
    tests + offline dev would refuse to start)."""
    storage_objects.assert_bucket_private_at_startup()


def test_different_paths_isolated() -> None:
    """Sanity that the backend stores paths verbatim and doesn't
    coalesce. The chunk-3-shipped pattern of per-merchant paths
    relies on this."""
    upload("merchants/m1/documents/d1.pdf.enc", b"first")
    upload("merchants/m2/documents/d2.pdf.enc", b"second")
    upload("unassigned/documents/d3.pdf.enc", b"orphan")

    assert download("merchants/m1/documents/d1.pdf.enc") == b"first"
    assert download("merchants/m2/documents/d2.pdf.enc") == b"second"
    assert download("unassigned/documents/d3.pdf.enc") == b"orphan"
