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

import logging
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest

from aegis import storage_objects
from aegis.storage_objects import (
    StorageBackendError,
    StorageError,
    _SupabaseStorageBackend,
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


# ---------------------------------------------------------------------------
# Bucket-private guard — five paths per operator's chunk-A refinement
# ---------------------------------------------------------------------------
#
# The supabase-backed bucket-private check has three "cannot determine"
# outcomes that MUST NOT brick the web tier (network/timeout, absent,
# auth). Verified-public is the only refuse-boot branch.
#
# Tests construct a _SupabaseStorageBackend with a stubbed ``_api()``
# that returns a fake storage client raising the desired exception. No
# real Supabase access — these are pure unit tests on the classification
# logic.


class _AuditCapture:
    """Test stub that records every audit row written.

    Implements the full AuditLog Protocol (``record``, ``list_recent``,
    ``list_for_subject``) so it passes mypy's structural check. The
    list_* methods return empty lists — these tests only care about
    what got written, not about reading rows back."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record(
        self,
        *,
        actor: str,
        action: str,
        subject_type: str | None = None,
        subject_id: UUID | None = None,
        details: dict[str, Any] | None = None,
        actor_email: str | None = None,
    ) -> None:
        self.records.append({
            "actor": actor,
            "action": action,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "details": details or {},
            "actor_email": actor_email,
        })

    def list_recent(self, *, limit: int = 20) -> list[dict[str, Any]]:
        del limit
        return []

    def list_for_subject(
        self,
        *,
        subject_type: str,
        subject_id: UUID,
        action: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        del subject_type, subject_id, action, limit
        return []


def _stub_backend_with_get_bucket(get_bucket_impl: Any) -> _SupabaseStorageBackend:
    """Build a _SupabaseStorageBackend whose ``_api().storage.get_bucket``
    routes through ``get_bucket_impl`` (callable taking the bucket
    name; returns an info dict OR raises)."""
    backend = _SupabaseStorageBackend()

    class _StorageStub:
        def get_bucket(self, name: str) -> Any:
            return get_bucket_impl(name)

    class _ApiStub:
        storage = _StorageStub()

    backend._api = lambda: _ApiStub()  # type: ignore[method-assign]
    return backend


# --- Path 1: verified-public → refuse boot --------------------------------


def test_verified_public_bucket_refuses_boot() -> None:
    """A bucket explicitly returning ``public=True`` from get_bucket()
    must fail the boot — encrypted-PDF storage requires a private
    bucket (service_role only)."""
    backend = _stub_backend_with_get_bucket(
        lambda name: {"name": name, "public": True}
    )
    audit = _AuditCapture()

    with pytest.raises(StorageBackendError) as exc:
        backend.assert_bucket_private("documents", audit=audit)

    assert "PUBLIC" in str(exc.value)
    # No audit row written on the refuse path — the StorageBackendError
    # surfaces to lifespan, which logs it; an audit row would be
    # redundant + would race with the abort.
    assert audit.records == []


# --- Path 2: verified-private → pass silently -----------------------------


def test_verified_private_bucket_passes_silently() -> None:
    """The healthy path. No log, no audit, no exception."""
    backend = _stub_backend_with_get_bucket(
        lambda name: {"name": name, "public": False}
    )
    audit = _AuditCapture()

    backend.assert_bucket_private("documents", audit=audit)  # no raise

    assert audit.records == []


# --- Path 3: unreachable (network/timeout) → WARN + proceed ---------------


def test_unreachable_supabase_warns_and_proceeds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Network exception from httpx → routine WARN + audit
    ``boot.bucket_check_unreachable`` + proceed. A Supabase outage
    during a kernel-update reboot must NOT take AEGIS down."""
    import httpx

    def _raise_connect_error(name: str) -> Any:
        raise httpx.ConnectError("DNS failure: no address for prod.supabase.co")

    backend = _stub_backend_with_get_bucket(_raise_connect_error)
    audit = _AuditCapture()

    with caplog.at_level(logging.WARNING):
        backend.assert_bucket_private("documents", audit=audit)  # no raise

    assert len(audit.records) == 1
    assert audit.records[0]["action"] == "boot.bucket_check_unreachable"
    assert audit.records[0]["actor"] == "boot"
    assert audit.records[0]["details"]["bucket"] == "documents"
    assert audit.records[0]["details"]["error_type"] == "ConnectError"
    assert any(
        "bucket_check_unreachable" in r.message and r.levelname == "WARNING"
        for r in caplog.records
    )


def test_timeout_warns_and_proceeds() -> None:
    """httpx.TimeoutException — same severity / action as ConnectError."""
    import httpx

    def _raise_timeout(name: str) -> Any:
        raise httpx.ReadTimeout("read timeout after 30s")

    backend = _stub_backend_with_get_bucket(_raise_timeout)
    audit = _AuditCapture()

    backend.assert_bucket_private("documents", audit=audit)  # no raise

    assert audit.records[0]["action"] == "boot.bucket_check_unreachable"


# --- Path 4: absent bucket (404) → ALERT-level WARN + proceed ------------


def test_absent_bucket_alerts_and_proceeds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """404 from get_bucket → ERROR log + audit ``boot.bucket_absent`` +
    proceed. Incomplete provisioning is not Supabase weather; chunk B
    will quarantine every upload until the bucket exists, so the
    journal-side alert must be louder than the unreachable case."""

    class _StorageApiError(Exception):
        def __init__(self, msg: str, code: int) -> None:
            super().__init__(msg)
            self.code = code

    def _raise_404(name: str) -> Any:
        raise _StorageApiError("Bucket not found", code=404)

    backend = _stub_backend_with_get_bucket(_raise_404)
    audit = _AuditCapture()

    with caplog.at_level(logging.ERROR):
        backend.assert_bucket_private("documents", audit=audit)  # no raise

    assert len(audit.records) == 1
    assert audit.records[0]["action"] == "boot.bucket_absent"
    assert audit.records[0]["details"]["status_code"] == 404
    assert any(
        "bucket_absent" in r.message and r.levelname == "ERROR"
        for r in caplog.records
    )


# --- Path 5: 401 / 403 (auth) → CRITICAL + proceed -----------------------


def test_auth_failure_401_escalates_and_proceeds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 401 from Supabase means the service_role credential is wrong —
    real fault, must escalate above routine WARN so journal-side
    alerting catches it. Still proceeds (don't brick the tier on a
    creds problem; the verified-public refuse-boot is the only
    hard gate)."""

    class _AuthError(Exception):
        def __init__(self, msg: str, status_code: int) -> None:
            super().__init__(msg)
            self.status_code = status_code

    def _raise_401(name: str) -> Any:
        raise _AuthError("invalid service_role token", status_code=401)

    backend = _stub_backend_with_get_bucket(_raise_401)
    audit = _AuditCapture()

    with caplog.at_level(logging.CRITICAL):
        backend.assert_bucket_private("documents", audit=audit)  # no raise

    assert len(audit.records) == 1
    assert audit.records[0]["action"] == "boot.bucket_auth_failed"
    assert audit.records[0]["details"]["status_code"] == 401
    assert any(
        "bucket_auth_failed" in r.message and r.levelname == "CRITICAL"
        for r in caplog.records
    )


def test_auth_failure_403_routes_to_auth_action() -> None:
    """403 (forbidden — service_role lacks the permission) routes to
    the same escalated action as 401 (unauthorized — token bad).
    Both are credential-class faults."""

    class _AuthError(Exception):
        def __init__(self, msg: str, status_code: int) -> None:
            super().__init__(msg)
            self.status_code = status_code

    def _raise_403(name: str) -> Any:
        raise _AuthError("forbidden", status_code=403)

    backend = _stub_backend_with_get_bucket(_raise_403)
    audit = _AuditCapture()

    backend.assert_bucket_private("documents", audit=audit)
    assert audit.records[0]["action"] == "boot.bucket_auth_failed"


# --- Audit-write failure tolerance --------------------------------------


def test_audit_write_failure_does_not_brick_boot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A DELIBERATE deviation from CLAUDE.md's "audit-write failures
    fail the operation" rule — narrowly scoped to this boot check. If
    Supabase Storage is down, Postgres might be too; failing boot
    because we can't audit defeats the don't-brick-the-tier
    requirement."""
    import httpx

    def _raise_connect(name: str) -> Any:
        raise httpx.ConnectError("supabase unreachable")

    backend = _stub_backend_with_get_bucket(_raise_connect)

    class _AuditBlowsUp:
        """Full Protocol surface so mypy passes; record() raises;
        list_* return empty (never invoked in this test)."""

        def record(
            self,
            *,
            actor: str,
            action: str,
            subject_type: str | None = None,
            subject_id: UUID | None = None,
            details: dict[str, Any] | None = None,
            actor_email: str | None = None,
        ) -> None:
            del (
                actor, action, subject_type, subject_id, details, actor_email,
            )
            raise RuntimeError("audit log is also down")

        def list_recent(self, *, limit: int = 20) -> list[dict[str, Any]]:
            del limit
            return []

        def list_for_subject(
            self,
            *,
            subject_type: str,
            subject_id: UUID,
            action: str | None = None,
            limit: int = 200,
        ) -> list[dict[str, Any]]:
            del subject_type, subject_id, action, limit
            return []

    with caplog.at_level(logging.CRITICAL):
        # MUST NOT raise — the boot proceeds even when both the bucket
        # check AND the audit write fail.
        backend.assert_bucket_private("documents", audit=_AuditBlowsUp())

    assert any(
        "audit_write_failed" in r.message for r in caplog.records
    )


def test_no_audit_passed_proceeds_without_emitting(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``audit=None`` is supported for callers that haven't wired the
    audit log (early-boot tests, scripts). The log emission still
    fires; only the audit row is skipped."""
    import httpx

    def _raise_connect(name: str) -> Any:
        raise httpx.ConnectError("network down")

    backend = _stub_backend_with_get_bucket(_raise_connect)
    with caplog.at_level(logging.WARNING):
        backend.assert_bucket_private("documents", audit=None)  # no raise

    assert any(
        "bucket_check_unreachable" in r.message for r in caplog.records
    )


# --- _extract_status_code edge cases ------------------------------------


def test_extract_status_code_from_int_attribute() -> None:
    from aegis.storage_objects import _extract_status_code

    class _StubError(Exception):
        code = 404

    assert _extract_status_code(_StubError()) == 404


def test_extract_status_code_from_string_attribute() -> None:
    from aegis.storage_objects import _extract_status_code

    class _StubError(Exception):
        code = "401"

    assert _extract_status_code(_StubError()) == 401


def test_extract_status_code_from_response_attribute() -> None:
    """httpx HTTPStatusError style: ``exc.response.status_code``."""
    from aegis.storage_objects import _extract_status_code

    class _Resp:
        status_code = 500

    class _StubError(Exception):
        response = _Resp()

    assert _extract_status_code(_StubError()) == 500


def test_extract_status_code_returns_none_for_shapes_without_status() -> None:
    """A bare exception with no recognizable status attribute returns
    None so the classifier falls through to network / unknown
    handling rather than guessing."""
    from aegis.storage_objects import _extract_status_code

    assert _extract_status_code(RuntimeError("???")) is None


def test_extract_status_code_rejects_out_of_range_values() -> None:
    """A ``code`` attribute carrying something that's clearly not an
    HTTP status (e.g. an internal error code) is rejected so it
    doesn't accidentally match a 4xx/5xx branch."""
    from aegis.storage_objects import _extract_status_code

    class _StubError(Exception):
        code = 99999

    assert _extract_status_code(_StubError()) is None


# --- _is_network_error edge cases ---------------------------------------


def test_is_network_error_true_for_httpx_connect_error() -> None:
    import httpx

    from aegis.storage_objects import _is_network_error

    assert _is_network_error(httpx.ConnectError("dns failure")) is True


def test_is_network_error_true_for_httpx_timeout() -> None:
    import httpx

    from aegis.storage_objects import _is_network_error

    assert _is_network_error(httpx.ReadTimeout("slow")) is True


def test_is_network_error_false_for_runtime_error() -> None:
    from aegis.storage_objects import _is_network_error

    assert _is_network_error(RuntimeError("unrelated")) is False
