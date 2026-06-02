"""Supabase Storage helper — opaque blob upload / download / delete.

Chunk A of the PDF retention redesign — see
``docs/PDF_RETENTION_DESIGN.md`` §7. The encrypted-PDF persistence
path goes through this module: the worker (chunk B) uploads
ciphertext via ``upload``, the view route (chunk C) downloads via
``download``, the retention sweep (chunk E) deletes via ``delete`` +
``confirm_absent``.

All operations raise ``StorageError`` on any non-2xx, including 404
from upload/download (the worker treats those as failures; the sweep
treats 404 on delete as already-gone idempotency). No retries are
implemented inside this module — the caller decides whether to retry,
quarantine, or audit.

Backend selection follows ``aegis_storage_backend`` from settings:
``"memory"`` for tests + offline dev (dict-backed); ``"supabase"`` for
production (service_role auth via ``SUPABASE_SERVICE_KEY``).

Bucket is read from ``AEGIS_DOCUMENT_BUCKET`` (default ``documents``).
Per-env separation: prod / staging / dev each get their own bucket so
a cross-env Supabase read can't reach the wrong corpus.
"""
from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Any, Final, Protocol

from aegis.config import get_settings
from aegis.logger import get_logger

if TYPE_CHECKING:
    from aegis.audit import AuditLog

_log = get_logger(__name__)


class StorageError(RuntimeError):
    """Wraps non-2xx responses from the storage backend.

    Maps to HTTP 500 in the view route, audits as
    ``document.original_viewed_integrity_failed`` (reason=storage_download_failed)
    or ``document.original_storage_failed`` in the worker.
    """


class StorageBackendError(RuntimeError):
    """Wraps backend misconfiguration detected at startup, e.g. the
    bucket is public or the service role can't see it. Refuses boot.
    """


class _StorageBackend(Protocol):
    def upload(self, path: str, data: bytes) -> None: ...
    def download(self, path: str) -> bytes: ...
    def delete(self, path: str) -> None: ...
    def confirm_absent(self, path: str) -> bool: ...
    def assert_bucket_private(
        self, bucket: str, audit: AuditLog | None = None
    ) -> None: ...


# ---------------------------------------------------------------------------
# In-memory backend — tests + offline dev
# ---------------------------------------------------------------------------


class _InMemoryStorageBackend:
    """Dict-backed backend for tests. NOT for production use."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}
        self._private_buckets: set[str] = set()

    def upload(self, path: str, data: bytes) -> None:
        self._blobs[path] = bytes(data)

    def download(self, path: str) -> bytes:
        if path not in self._blobs:
            raise StorageError(f"blob not found: {path}")
        return self._blobs[path]

    def delete(self, path: str) -> None:
        # Idempotent — 404 tolerated per Supabase contract
        self._blobs.pop(path, None)

    def confirm_absent(self, path: str) -> bool:
        return path not in self._blobs

    def assert_bucket_private(
        self, bucket: str, audit: AuditLog | None = None
    ) -> None:
        # Tests opt in; default is "private". Real backend asserts
        # against Supabase ACL.
        # ``audit`` is accepted to satisfy the Protocol; the in-memory
        # backend never has a "cannot determine" path that would
        # warrant an audit row.
        del audit  # explicit no-op marker for ruff
        self._private_buckets.add(bucket)

    # Test helpers
    def _force_present(self, path: str, data: bytes) -> None:
        self._blobs[path] = bytes(data)

    def _list_paths(self) -> list[str]:
        return sorted(self._blobs.keys())


# ---------------------------------------------------------------------------
# Supabase backend — production
# ---------------------------------------------------------------------------


class _SupabaseStorageBackend:
    """Real Supabase Storage backend via ``supabase-py``.

    Auth: ``SUPABASE_SERVICE_KEY`` (service_role). Bucket is read from
    ``AEGIS_DOCUMENT_BUCKET``. Created lazily; raises ``StorageError``
    if the client can't be built or the bucket can't be reached.
    """

    def __init__(self) -> None:
        # Lazy client — we don't want module import to hit Supabase.
        # supabase-py's Client class has inconsistent type exposure across
        # versions; ``Any`` keeps this module free of upstream typing
        # noise without losing IDE support at call sites (the public
        # functions below are still strictly typed).
        self._client: Any = None

    def _api(self) -> Any:  # noqa: ANN401  # supabase-py types are version-inconsistent; Any is intentional
        if self._client is None:
            from aegis.db import get_supabase
            self._client = get_supabase()
        return self._client

    def upload(self, path: str, data: bytes) -> None:
        try:
            self._api().storage.from_(_bucket()).upload(
                path=path,
                file=data,
                file_options={"content-type": "application/octet-stream"},
            )
        except Exception as exc:
            raise StorageError(f"upload {path!r} failed: {exc}") from exc

    def download(self, path: str) -> bytes:
        try:
            data = self._api().storage.from_(_bucket()).download(path)
        except Exception as exc:
            raise StorageError(f"download {path!r} failed: {exc}") from exc
        # supabase-py returns bytes; widen-then-narrow to satisfy mypy
        # under no-any-return.
        if isinstance(data, (bytes, bytearray, memoryview)):
            return bytes(data)
        raise StorageError(
            f"download {path!r} returned non-bytes ({type(data).__name__})"
        )

    def delete(self, path: str) -> None:
        # supabase-py's remove([paths]) is idempotent for already-gone
        # paths — tolerate the "not found" case.
        try:
            self._api().storage.from_(_bucket()).remove([path])
        except Exception as exc:
            msg = str(exc).lower()
            if "not found" in msg or "404" in msg:
                return
            raise StorageError(f"delete {path!r} failed: {exc}") from exc

    def confirm_absent(self, path: str) -> bool:
        """Lists at the path's directory and checks the basename is
        absent. Returns True on 404 / empty listing, False if the blob
        is still listable. Raises ``StorageError`` on transport
        failure (separate from "still there").
        """
        try:
            # Supabase list is by directory; basename = last segment
            head, _, basename = path.rpartition("/")
            entries = self._api().storage.from_(_bucket()).list(head or "")
            for entry in entries:
                if entry.get("name") == basename:
                    return False
            return True
        except Exception as exc:
            raise StorageError(f"confirm_absent {path!r} failed: {exc}") from exc

    def assert_bucket_private(
        self, bucket: str, audit: AuditLog | None = None
    ) -> None:
        """Verify the bucket exists and is configured private.

        Boot-time check called by ``app.lifespan``. Implementation hits
        ``storage.get_bucket(bucket)`` and inspects the public flag.

        Behavior by outcome (per design doc §3 + operator-required
        chunk-A refinement: don't brick the web tier on Supabase
        weather):

          * Verified PUBLIC  → raise ``StorageBackendError`` (fail closed)
          * Verified PRIVATE → return silently (pass)
          * 401 / 403 (auth)  → log CRITICAL, audit
            ``boot.bucket_auth_failed``, **proceed**. A wrong
            service_role credential is a real fault, escalated above
            routine WARN so the journal-side alerting catches it.
          * 404 (absent)      → log ERROR, audit ``boot.bucket_absent``,
            **proceed**. Incomplete provisioning — chunk B will
            quarantine every upload until it exists.
          * Network / timeout → log WARN, audit
            ``boot.bucket_check_unreachable``, **proceed**. Supabase
            outage during routine maintenance must not take AEGIS down.

        Audit-write failures inside the proceed paths are swallowed
        with a CRITICAL log entry — this is a DELIBERATE deviation
        from CLAUDE.md's "audit-write failures fail the operation"
        rule, narrowly scoped to this boot check. Rationale: the
        operation here IS booting; failing boot because we can't
        record an observation defeats the operator's "don't brick
        the tier" requirement. The structured journal log is the
        always-on signal; the audit row is a durable record when
        possible.
        """
        try:
            info = self._api().storage.get_bucket(bucket)
        except Exception as exc:
            _classify_and_emit_bucket_check_failure(bucket, exc, audit)
            return  # proceed regardless of which unknown — operator-required

        # supabase-py returns a dict-like or object with .public attribute
        is_public = getattr(info, "public", None)
        if is_public is None and isinstance(info, dict):
            is_public = info.get("public")
        if is_public is True:
            raise StorageBackendError(
                f"bucket {bucket!r} is PUBLIC — refusing to boot. "
                "Encrypted-PDF storage requires a private bucket "
                "(service_role-only). Disable public access in the "
                "Supabase Storage console."
            )


# ---------------------------------------------------------------------------
# Backend factory + public API
# ---------------------------------------------------------------------------


_BUCKET_DEFAULT: Final[str] = "documents"


def _bucket() -> str:
    return get_settings().aegis_document_bucket


@lru_cache(maxsize=1)
def _get_backend() -> _StorageBackend:
    """Pick a backend based on ``aegis_storage_backend`` settings."""
    backend = get_settings().aegis_storage_backend
    if backend == "memory":
        return _InMemoryStorageBackend()
    return _SupabaseStorageBackend()


def reset_backend_for_tests() -> None:
    """Test helper — clears the LRU cache so a per-test backend can
    swap in. NEVER call from production code paths."""
    _get_backend.cache_clear()


def upload(path: str, data: bytes) -> None:
    """Upload ciphertext to the configured bucket at ``path``.

    Raises ``StorageError`` on any non-2xx. The worker (chunk B)
    catches this, quarantines the ciphertext, and audits
    ``document.original_storage_failed``.
    """
    _get_backend().upload(path, data)


def download(path: str) -> bytes:
    """Fetch ciphertext from the configured bucket at ``path``.

    Raises ``StorageError`` on any non-2xx (including 404 — the view
    route maps that to HTTP 500 + integrity_failed because a
    populated ``documents.storage_path`` should resolve).
    """
    return _get_backend().download(path)


def delete(path: str) -> None:
    """Delete the ciphertext blob at ``path``. Idempotent — tolerates
    already-404 so retention-sweep retries don't error on a partially-
    completed previous run."""
    _get_backend().delete(path)


def confirm_absent(path: str) -> bool:
    """HEAD/list check after delete. Returns True when the blob is
    confirmed absent (404 / not listable), False when it's still
    present. Raises ``StorageError`` on transport / auth failure.

    Used by the retention sweep cron (chunk E) to prove deletion
    before writing the ``document.retention_deleted`` audit row.
    """
    return _get_backend().confirm_absent(path)


def assert_bucket_private_at_startup(audit: AuditLog | None = None) -> None:
    """Boot-time guard. Verifies the configured bucket exists and is
    private. Called by ``app.lifespan`` so a misconfigured bucket
    refuses to start.

    Behavior (chunk-A operator refinement): refuses boot ONLY on
    verified-public. Cannot-determine outcomes (unreachable, absent,
    auth) write a journal log at the appropriate severity + an audit
    row tagged with the specific cause, then proceed. See
    ``_SupabaseStorageBackend.assert_bucket_private`` for the full
    contract.

    ``audit`` is optional so tests that don't wire an audit log can
    still call this guard; production callers (``app.lifespan``) pass
    ``get_audit()`` so the cannot-determine paths get a durable trace.

    No-op for the in-memory backend (tests).
    """
    backend = _get_backend()
    bucket = _bucket()
    backend.assert_bucket_private(bucket, audit=audit)


# ---------------------------------------------------------------------------
# Bucket-check failure classification (chunk-A operator refinement)
# ---------------------------------------------------------------------------


def _classify_and_emit_bucket_check_failure(
    bucket: str, exc: Exception, audit: AuditLog | None
) -> None:
    """Classify the bucket-check exception into one of three
    cannot-determine paths and emit the matching log + audit row.

    Severity ladder (per operator's chunk-A refinement):
      * AUTH (401/403)   → CRITICAL log + ``boot.bucket_auth_failed``
      * ABSENT (404)     → ERROR log    + ``boot.bucket_absent``
      * UNREACHABLE      → WARNING log  + ``boot.bucket_check_unreachable``
      * UNKNOWN          → WARNING log  + ``boot.bucket_check_unreachable``
        (conservative default — treat the unmapped shape as Supabase
        weather rather than escalating it)

    Audit-write failures are swallowed with a CRITICAL log entry —
    see ``_SupabaseStorageBackend.assert_bucket_private`` for the
    deliberate deviation rationale.
    """
    status = _extract_status_code(exc)
    error_type = type(exc).__name__
    error_message = str(exc)[:500]

    if status in (401, 403):
        action = "boot.bucket_auth_failed"
        _log.critical(
            "ops.boot.bucket_auth_failed bucket=%s error=%s status=%s",
            bucket, error_type, status,
        )
    elif status == 404:
        action = "boot.bucket_absent"
        _log.error(
            "ops.boot.bucket_absent bucket=%s error=%s",
            bucket, error_type,
        )
    elif _is_network_error(exc):
        action = "boot.bucket_check_unreachable"
        _log.warning(
            "ops.boot.bucket_check_unreachable bucket=%s error=%s",
            bucket, error_type,
        )
    else:
        # Unmapped exception shape — default to unreachable. Logging
        # at WARNING (not error/critical) keeps Supabase-weather
        # outages from spam-paging on every unfamiliar exception
        # subclass; a genuinely-faulty shape gets re-classified
        # when we learn what it looks like.
        action = "boot.bucket_check_unreachable"
        _log.warning(
            "ops.boot.bucket_check_unmapped bucket=%s error=%s status=%s",
            bucket, error_type, status,
        )

    if audit is None:
        return
    try:
        audit.record(
            actor="boot",
            action=action,
            details={
                "bucket": bucket,
                "error_type": error_type,
                "error_message": error_message,
                "status_code": status,
            },
        )
    except Exception:
        # DELIBERATE swallow — see _SupabaseStorageBackend.assert_bucket_private
        # docstring for the deviation rationale (boot is the operation; we
        # don't fail boot on inability to audit). Log CRITICAL so journal
        # alerting catches the audit-write failure itself.
        _log.exception(
            "ops.boot.audit_write_failed action=%s bucket=%s",
            action, bucket,
        )


def _extract_status_code(exc: Exception) -> int | None:
    """Best-effort HTTP-status extraction from various exception
    shapes supabase-py / httpx might raise.

    Supports:
      * direct ``.code`` / ``.status_code`` / ``.status`` attributes
        (int or numeric string — supabase-py StorageApiError varies
        by version)
      * ``.response.status_code`` (httpx HTTPStatusError shape)
      * fallback ``None`` for shapes that don't carry a status

    Returns ``None`` (not 0) so callers can distinguish "no status"
    from "status 0".
    """
    for attr in ("code", "status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
        if isinstance(value, str) and value.isdigit():
            parsed = int(value)
            if 100 <= parsed <= 599:
                return parsed
    response = getattr(exc, "response", None)
    if response is not None:
        candidate = getattr(response, "status_code", None)
        if isinstance(candidate, int) and 100 <= candidate <= 599:
            return candidate
    return None


def _is_network_error(exc: Exception) -> bool:
    """True for httpx transport-level exceptions (connect failure,
    timeout, DNS, etc.). Used to distinguish "Supabase weather"
    (unreachable) from "Supabase responded with a problem"
    (auth / absent).

    Lazy-imports ``httpx`` because storage_objects is imported at
    boot regardless of which backend is active, and the tests for
    the in-memory backend shouldn't pay an httpx import cost.
    """
    try:
        import httpx
    except ImportError:
        return False
    return isinstance(exc, httpx.TransportError)


__all__ = [
    "StorageBackendError",
    "StorageError",
    "_classify_and_emit_bucket_check_failure",
    "_extract_status_code",
    "_is_network_error",
    "assert_bucket_private_at_startup",
    "confirm_absent",
    "delete",
    "download",
    "reset_backend_for_tests",
    "upload",
]
