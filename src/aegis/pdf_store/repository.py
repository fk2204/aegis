"""PdfStoreRepository — Protocol + InMemory + Supabase impls.

Mirrors the two-impl pattern of ``aegis.funder_note_submissions.repository``
and ``aegis.merchants.repository``. The Protocol surface is intentionally
narrow:

* :meth:`store`           — seal plaintext, persist the ciphertext blob.
* :meth:`fetch_plaintext` — load the row, decrypt, verify integrity.

There is NO update or status mutation API — pdf_store is append-only
from the operator's perspective. Re-parses overwrite the row (latest
ciphertext wins) so the worker can re-run a parse without leaking stale
ciphertext for a document the operator re-uploaded.

Error semantics:

* :class:`PdfStoreNotFoundError` — no row for the document_id.
* :class:`PdfStoreIntegrityError` — decrypt succeeded but the SHA-256
  of the plaintext disagrees with ``sha256_plaintext``. The view route
  maps this to HTTP 500 + a ``document.pdf_streamed_integrity_failed``
  audit row.
* :class:`PdfStoreWriteError` — Supabase write path failed (transient or
  permanent). The worker preserves the local plaintext for retry / ops
  inspection rather than unlinking after a failed store.

CLAUDE.md PDF retention rule: integrity is checked on EVERY read in
addition to the AES-GCM auth tag check inside ``decrypt_pdf``. A
mismatch is a typed error, not a logged-and-served warning.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import UUID

from aegis import storage_objects
from aegis.crypto import (
    CorruptCiphertextError,
    current_key_version,
    decrypt_pdf,
    encrypt_pdf,
)
from aegis.db import get_supabase
from aegis.logger import get_logger
from aegis.pdf_store.models import PdfStoreRow
from aegis.storage_objects import StorageError

_log = get_logger(__name__)


# AES-GCM nonce length is 12 bytes — the same constant ``aegis.crypto``
# uses internally. Duplicated here so the splitter below doesn't depend
# on the private name.
_NONCE_BYTES = 12


class PdfStoreNotFoundError(KeyError):
    """Raised when ``fetch_plaintext`` finds no row for the document_id."""


class PdfStoreIntegrityError(RuntimeError):
    """Raised when the SHA-256 of decrypted plaintext disagrees with the
    stored ``sha256_plaintext`` column. AES-GCM's auth tag would already
    catch most tampering — this check is belt-and-suspenders against
    silent corruption (Postgres disk, network), and matches the CLAUDE.md
    contract "Integrity: SHA-256 of plaintext at documents.sha256_original,
    checked on every read."
    """


class PdfStoreWriteError(RuntimeError):
    """Raised when a pdf_store row could not be persisted."""


class PdfStoreRepository(Protocol):
    def store(
        self,
        *,
        document_id: UUID,
        plaintext: bytes,
    ) -> PdfStoreRow:
        """Encrypt ``plaintext`` under the current key version and
        persist a row keyed by ``document_id``. Idempotent: if a row
        already exists for ``document_id`` it is overwritten (latest
        ciphertext wins so a re-parse cleanly replaces the prior seal).
        """

    def fetch_plaintext(self, document_id: UUID) -> bytes:
        """Return the original plaintext PDF bytes for ``document_id``.

        Pipeline:
          1. Read the row (404 path: ``PdfStoreNotFoundError``).
          2. Call ``aegis.crypto.decrypt_pdf`` with the stored
             ``key_version``. Tag failure raises ``CorruptCiphertextError``
             (forwarded to the caller as-is — already a typed error).
          3. Compute SHA-256 of the decrypted bytes and compare against
             ``sha256_plaintext``. Mismatch raises
             ``PdfStoreIntegrityError``.
        """


# ---------------------------------------------------------------------------
# In-memory implementation — tests + offline runs.
# ---------------------------------------------------------------------------


class InMemoryPdfStoreRepository:
    """Dict-backed pdf_store. Tests + offline use."""

    def __init__(self) -> None:
        self._by_id: dict[UUID, PdfStoreRow] = {}

    def store(
        self,
        *,
        document_id: UUID,
        plaintext: bytes,
    ) -> PdfStoreRow:
        row = _seal_plaintext(document_id=document_id, plaintext=plaintext)
        # Overwrite-on-conflict: re-parses replace the prior seal.
        self._by_id[document_id] = row
        return row

    def fetch_plaintext(self, document_id: UUID) -> bytes:
        try:
            row = self._by_id[document_id]
        except KeyError as exc:
            raise PdfStoreNotFoundError(str(document_id)) from exc
        return _unseal_row(row)


# ---------------------------------------------------------------------------
# Supabase implementation
# ---------------------------------------------------------------------------


class SupabasePdfStoreRepository:
    """Persistence backed by Postgres ``pdf_store`` (migration 060) +
    Supabase Storage (migration 062 onwards).

    Migration 062 relocated the ciphertext blob out of the BYTEA column
    and into Supabase Storage to avoid PostgREST's ~1 MB request-body
    ceiling on the supabase-py wire (BYTEA inflates ~33 % as base64
    on the JSON path, so any plaintext > ~750 KB blew the limit and
    blocked the recovery script's prod run on 2026-06-16). The row in
    ``pdf_store`` now carries only metadata + the bucket path; the
    blob travels via service-role direct upload / download.

    Reads tolerate BOTH legacy BYTEA rows AND new storage-path rows so
    a backfill sweep can migrate at its own cadence.
    """

    def store(
        self,
        *,
        document_id: UUID,
        plaintext: bytes,
    ) -> PdfStoreRow:
        if not plaintext:
            raise PdfStoreWriteError(f"cannot store empty plaintext for document_id={document_id}")

        # Encrypt + compute integrity hash. Both happen client-side so
        # neither the Storage bucket nor the metadata row ever see
        # plaintext; if anything raises before the upload we're still
        # holding the bytes locally and the caller decides what to do.
        key_version = current_key_version()
        blob = encrypt_pdf(plaintext, key_version=key_version)
        sha256_hex = hashlib.sha256(plaintext).hexdigest()
        storage_path = _build_pdf_store_path(document_id)

        # Storage upload BEFORE metadata insert. If the upload raises,
        # nothing in Postgres references the (nonexistent) blob — a
        # retry can re-upload + re-insert cleanly. If the metadata
        # insert raises after a successful upload, we leave the blob
        # in Storage rather than rolling back; the cheap follow-up is
        # the same retry path landing the row on the second attempt.
        try:
            storage_objects.upload(storage_path, blob)
        except StorageError as exc:
            _log.error(
                "pdf_store.storage_upload_failed document_id=%s byte_size=%d",
                document_id,
                len(plaintext),
            )
            raise PdfStoreWriteError(
                f"failed to upload pdf_store ciphertext for document_id={document_id}: {exc}"
            ) from exc

        # Metadata row — no BYTEA columns set on the insert. The
        # storage_path column is the lookup key the read path
        # downloads from. Upsert on document_id PK matches the
        # re-parse semantic the original migration documented.
        payload: dict[str, Any] = {
            "document_id": str(document_id),
            "storage_path": storage_path,
            "key_version": key_version,
            "sha256_plaintext": sha256_hex,
            "byte_size_plaintext": len(plaintext),
        }
        try:
            result = (
                get_supabase()
                .table("pdf_store")
                .upsert(payload, on_conflict="document_id")
                .execute()
            )
        except Exception as exc:
            _log.error(
                "pdf_store.metadata_write_failed document_id=%s byte_size=%d storage_path=%s",
                document_id,
                len(plaintext),
                storage_path,
            )
            raise PdfStoreWriteError(
                f"failed to write pdf_store metadata row for document_id={document_id}"
            ) from exc
        rows = cast(list[dict[str, Any]], result.data or [])
        if not rows:
            raise PdfStoreWriteError(
                f"supabase upsert returned no row for pdf_store document_id={document_id}"
            )
        return _row_from_dict(rows[0])

    def fetch_plaintext(self, document_id: UUID) -> bytes:
        try:
            result = (
                get_supabase()
                .table("pdf_store")
                .select("*")
                .eq("document_id", str(document_id))
                .limit(1)
                .execute()
            )
        except Exception as exc:
            _log.error(
                "pdf_store.read_failed document_id=%s",
                document_id,
            )
            raise PdfStoreWriteError(
                f"failed to read pdf_store row for document_id={document_id}"
            ) from exc
        rows = cast(list[dict[str, Any]], result.data or [])
        if not rows:
            raise PdfStoreNotFoundError(str(document_id))

        row = _row_from_dict(rows[0])

        # Two-mode read: storage_path wins when present, else fall back
        # to the legacy inline BYTEA. The model validator guarantees
        # at least one mode is populated — the cast inside the else
        # branch is safe.
        if row.storage_path is not None:
            try:
                blob = storage_objects.download(row.storage_path)
            except StorageError as exc:
                _log.error(
                    "pdf_store.storage_download_failed document_id=%s storage_path=%s",
                    document_id,
                    row.storage_path,
                )
                raise PdfStoreWriteError(
                    f"failed to download pdf_store ciphertext for "
                    f"document_id={document_id} at {row.storage_path!r}: {exc}"
                ) from exc
        else:
            # PdfStoreRow._at_least_one_storage_mode guarantees we get
            # here only when ciphertext is populated; the local
            # variable copy is just to satisfy mypy's narrowing pass
            # without ``assert`` (ruff S101).
            inline_blob = row.ciphertext
            if inline_blob is None:
                raise PdfStoreWriteError(
                    f"pdf_store row for document_id={document_id} has neither "
                    f"storage_path nor inline ciphertext (CHECK constraint "
                    f"pdf_store_blob_or_path should have caught this)"
                )
            blob = inline_blob

        return _unseal_blob(
            document_id=row.document_id,
            blob=blob,
            key_version=row.key_version,
            expected_sha256=row.sha256_plaintext,
        )


def _build_pdf_store_path(document_id: UUID) -> str:
    """Stable Supabase Storage path for the pdf_store ciphertext blob.

    Distinct prefix from chunk-B's
    ``merchants/{merchant_id}/documents/{document_id}.pdf.enc`` so the
    two ciphertext copies are independently auditable; consolidating
    them is a future plumbing change.
    """
    return f"pdf_store/{document_id}.pdf.enc"


# ---------------------------------------------------------------------------
# Crypto helpers — shared by both backends.
# ---------------------------------------------------------------------------


def _seal_plaintext(*, document_id: UUID, plaintext: bytes) -> PdfStoreRow:
    """Encrypt ``plaintext`` and pack the resulting PdfStoreRow.

    The SHA-256 is computed on the plaintext input (not on the
    ciphertext) so the integrity check on read is meaningful against
    bytes-in-bytes-out semantics.
    """
    if not plaintext:
        # A zero-byte PDF would also fail the DB CHECK
        # (byte_size_plaintext > 0) but raising here gives a clearer
        # error than waiting for the round-trip.
        raise PdfStoreWriteError(f"cannot store empty plaintext for document_id={document_id}")
    key_version = current_key_version()
    blob = encrypt_pdf(plaintext, key_version=key_version)
    # ``encrypt_pdf`` returns ``nonce(12) || ciphertext || tag(16)``.
    # Split out the nonce so the dedicated column matches the leading
    # 12 bytes of the blob; the view of the blob carried by the row is
    # ``ciphertext = nonce || ct || tag`` unchanged.
    nonce = blob[:_NONCE_BYTES]
    sha256_hex = hashlib.sha256(plaintext).hexdigest()
    return PdfStoreRow(
        document_id=document_id,
        ciphertext=blob,
        nonce=nonce,
        key_version=key_version,
        sha256_plaintext=sha256_hex,
        byte_size_plaintext=len(plaintext),
    )


def _unseal_row(row: PdfStoreRow) -> bytes:
    """Decrypt an inline-BYTEA row and verify the plaintext SHA-256.

    Used by ``InMemoryPdfStoreRepository`` (whose blobs always live in
    the row) and as the legacy-row code path inside
    ``SupabasePdfStoreRepository.fetch_plaintext``. Mig-062-onwards
    Supabase rows route through ``_unseal_blob`` instead because the
    bytes arrive from Storage, not from the row dict.
    """
    if row.ciphertext is None:
        raise PdfStoreWriteError(
            f"_unseal_row called on row with no inline ciphertext "
            f"for document_id={row.document_id}; use _unseal_blob with the "
            f"Storage-downloaded bytes"
        )
    return _unseal_blob(
        document_id=row.document_id,
        blob=row.ciphertext,
        key_version=row.key_version,
        expected_sha256=row.sha256_plaintext,
    )


def _unseal_blob(
    *,
    document_id: UUID,
    blob: bytes,
    key_version: int,
    expected_sha256: str,
) -> bytes:
    """Decrypt arbitrary ciphertext bytes and verify the plaintext SHA-256.

    ``decrypt_pdf`` raises ``CorruptCiphertextError`` on AES-GCM tag
    failure — that wraps tampering, wrong key, AND truncation. We
    forward it as-is (callers want to know the bytes are bad regardless
    of the precise cause) and add a separate ``PdfStoreIntegrityError``
    for the hash-mismatch case. Both surface as HTTP 500 at the route.
    """
    plaintext = decrypt_pdf(blob, key_version=key_version)
    actual_sha = hashlib.sha256(plaintext).hexdigest()
    if actual_sha != expected_sha256:
        # Length-comparison in the message lets the operator distinguish
        # truncation from corruption without ever dumping bytes to a log.
        raise PdfStoreIntegrityError(
            f"sha256 mismatch for document_id={document_id}: "
            f"stored={expected_sha256[:16]}... "
            f"actual={actual_sha[:16]}... "
            f"plaintext_bytes={len(plaintext)}"
        )
    return plaintext


# ---------------------------------------------------------------------------
# Row encoders / decoders
# ---------------------------------------------------------------------------


def _row_to_payload(r: PdfStoreRow) -> dict[str, Any]:
    """Encode a PdfStoreRow for the ``pdf_store`` insert / upsert path.

    Either inline (legacy InMemory backend + pre-mig-062 Supabase rows)
    or storage-path mode (mig-062 onwards). Postgres BYTEA columns
    accept Python ``bytes`` over supabase-py; no manual hex encoding
    required.
    """
    payload: dict[str, Any] = {
        "document_id": str(r.document_id),
        "key_version": r.key_version,
        "sha256_plaintext": r.sha256_plaintext,
        "byte_size_plaintext": r.byte_size_plaintext,
    }
    if r.storage_path is not None:
        payload["storage_path"] = r.storage_path
    if r.ciphertext is not None:
        payload["ciphertext"] = r.ciphertext
    if r.nonce is not None:
        payload["nonce"] = r.nonce
    return payload


def _row_from_dict(row: dict[str, Any]) -> PdfStoreRow:
    """Decode a Postgres row dict into a PdfStoreRow.

    Handles both storage modes — ``storage_path`` populated (mig-062
    onwards) or inline ``ciphertext`` + ``nonce`` (legacy). The
    PdfStoreRow validator enforces at least one mode is present, so a
    pathological row with neither raises at construction time rather
    than mysteriously decrypting nothing.

    BYTEA columns come back from supabase-py as either ``bytes`` (raw
    BYTEA) or hex-encoded strings ('\\x...' / hex digits) depending on
    the PostgREST encoding mode. Tolerate both so a backend swap or
    encoding update can't silently break reads.
    """
    storage_path_raw = row.get("storage_path")
    storage_path: str | None = str(storage_path_raw) if isinstance(storage_path_raw, str) else None
    ciphertext = _decode_bytea(row.get("ciphertext"))
    nonce = _decode_bytea(row.get("nonce"))
    stored_at = _parse_dt(row.get("stored_at"))
    return PdfStoreRow(
        document_id=UUID(str(row["document_id"])),
        storage_path=storage_path,
        ciphertext=ciphertext,
        nonce=nonce,
        key_version=int(row["key_version"]),
        sha256_plaintext=str(row["sha256_plaintext"]),
        byte_size_plaintext=int(row["byte_size_plaintext"]),
        stored_at=stored_at,
    )


def _decode_bytea(value: object) -> bytes | None:
    """Accept either raw bytes (Supabase BYTEA Python path) or a hex
    string of the form '\\xDEADBEEF' (PostgREST text encoding).
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        s = value
        if s.startswith("\\x") or s.startswith("\\X"):
            s = s[2:]
        try:
            return bytes.fromhex(s)
        except ValueError as exc:
            raise PdfStoreWriteError(f"pdf_store row has non-hex BYTEA value: {value!r}") from exc
    raise PdfStoreWriteError(f"pdf_store row has unsupported BYTEA type: {type(value).__name__}")


def _parse_dt(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


__all__ = [
    # Re-export from aegis.crypto so the view route can catch both
    # AES-GCM rejection (``CorruptCiphertextError``) and SHA-256 mismatch
    # (``PdfStoreIntegrityError``) by importing from the same module.
    "CorruptCiphertextError",
    "InMemoryPdfStoreRepository",
    "PdfStoreIntegrityError",
    "PdfStoreNotFoundError",
    "PdfStoreRepository",
    "PdfStoreWriteError",
    "SupabasePdfStoreRepository",
]
