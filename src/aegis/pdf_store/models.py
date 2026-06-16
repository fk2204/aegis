"""PdfStoreRow — Pydantic projection of one ``pdf_store`` row.

Mirrors ``migrations/060_pdf_store.sql`` + ``062_pdf_store_storage_path.sql``.
Pydantic-strict so a Supabase column drift trips at parse time rather
than corrupting the view-route download downstream.

Two storage modes coexist for backward compatibility:

  * **Legacy (pre-mig-062)** — ``ciphertext`` + ``nonce`` populated,
    ``storage_path`` NULL. Plaintext is sealed inline as BYTEA in the
    Postgres row. Reads decrypt the BYTEA blob directly.
  * **Current (mig-062 onwards)** — ``storage_path`` populated,
    ``ciphertext`` + ``nonce`` NULL. The AES-GCM ciphertext lives in
    Supabase Storage at ``storage_path``; the row holds only metadata.
    Reads download the blob from Storage, then decrypt.

``key_version`` and ``sha256_plaintext`` are required in both modes —
the decrypt path needs the former to pick the key, and the read path
verifies integrity against the latter regardless of where the blob
came from.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )


class PdfStoreRow(_StrictModel):
    """One ciphertext blob keyed by ``document_id``.

    Either ``storage_path`` is set (current mode — blob lives in
    Supabase Storage) or both ``ciphertext`` + ``nonce`` are set
    (legacy mode — blob lives inline as BYTEA). A row with neither
    storage mode populated has nothing to decrypt and is rejected at
    construction time by ``_at_least_one_storage_mode``.
    """

    document_id: UUID

    # Path in Supabase Storage at which the AES-GCM sealed ciphertext
    # lives. ``aegis.storage_objects`` is the I/O surface — service-
    # role direct download. NEVER served back to a client as a signed
    # / public URL (see tests/test_security_invariants.py for the
    # grep-ban on the matching supabase-py helpers).
    storage_path: str | None = None

    # AES-GCM sealed bytes from ``aegis.crypto.encrypt_pdf``. Layout:
    # ``nonce(12) || ciphertext || tag(16)``. Minimum length when set
    # is 28 bytes (empty plaintext would be ``nonce + tag`` alone), but
    # the DB rejects ``byte_size_plaintext = 0`` so a real legacy row
    # always carries ≥ 29 bytes. ``None`` for mig-062-onwards rows
    # whose blob lives in Storage instead.
    ciphertext: bytes | None = None

    # 12-byte AES-GCM nonce. Embedded in the leading bytes of
    # ``ciphertext`` too — duplicated here for audit queries that
    # inspect nonces without parsing the blob. ``None`` for
    # mig-062-onwards rows.
    nonce: bytes | None = None

    # Which PDF_ENCRYPTION_KEY_V{n} sealed this row. Always required —
    # the decrypt path looks up the matching key in
    # ``/etc/aegis/aegis.env`` regardless of where the ciphertext lives.
    key_version: Annotated[int, Field(ge=1)]

    # Plaintext SHA-256 as a 64-character hex digest. Verified after
    # every decrypt per CLAUDE.md PDF retention rule (integrity check
    # on every read; mismatch is a hard 500 + audit row).
    sha256_plaintext: Annotated[str, Field(min_length=64, max_length=64)]

    # Plaintext length in bytes. Surfaces in audit rows so the operator
    # can confirm "we streamed 1.2 MB at 14:03Z" without the bytes
    # themselves ever landing in a log.
    byte_size_plaintext: Annotated[int, Field(gt=0)]

    # When the row landed. Postgres default NOW(); None on Pydantic
    # constructions that have not yet round-tripped through the DB.
    stored_at: datetime | None = None

    @field_validator("ciphertext")
    @classmethod
    def _ciphertext_min_length(cls, v: bytes | None) -> bytes | None:
        if v is not None and len(v) < 28:
            raise ValueError(
                "PdfStoreRow.ciphertext, when set, must be ≥ 28 bytes "
                "(nonce(12) + tag(16) is the floor for empty plaintext)"
            )
        return v

    @field_validator("nonce")
    @classmethod
    def _nonce_length(cls, v: bytes | None) -> bytes | None:
        if v is not None and len(v) != 12:
            raise ValueError(
                f"PdfStoreRow.nonce, when set, must be exactly 12 bytes (AES-GCM); got {len(v)}"
            )
        return v

    @model_validator(mode="after")
    def _at_least_one_storage_mode(self) -> PdfStoreRow:
        """Enforce ``CHECK (storage_path IS NOT NULL OR (ciphertext IS
        NOT NULL AND nonce IS NOT NULL))`` from migration 062 at the
        Pydantic boundary so a malformed construct fails fast rather
        than at DB write time. The two modes are not mutually
        exclusive — a transient state where both columns carry data
        is allowed for forward-migrating legacy rows in place.
        """
        has_storage = self.storage_path is not None
        has_inline = self.ciphertext is not None and self.nonce is not None
        if not has_storage and not has_inline:
            raise ValueError(
                "PdfStoreRow needs storage_path OR (ciphertext AND nonce); got neither"
            )
        return self


__all__ = ["PdfStoreRow"]
