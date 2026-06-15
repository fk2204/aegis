"""PdfStoreRow — Pydantic projection of one ``pdf_store`` row.

Mirrors ``migrations/060_pdf_store.sql``. Pydantic-strict so a Supabase
column drift trips at parse time rather than corrupting the view-route
download downstream.

All four crypto columns are required: ciphertext, nonce, key_version,
sha256_plaintext. The nonce is also embedded in the leading 12 bytes of
``ciphertext`` (see ``aegis.crypto.encrypt_pdf``); the separate column
exists for forensic audit queries (rotation drift, nonce-collision
sweeps) that should not need to parse the blob.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )


class PdfStoreRow(_StrictModel):
    """One ciphertext blob keyed by ``document_id``.

    Field shapes are gated to match the DB CHECK constraints in migration
    060 so a Pydantic build can never produce a payload that would fail
    on insert.
    """

    document_id: UUID

    # AES-GCM sealed bytes from ``aegis.crypto.encrypt_pdf``. Layout:
    # ``nonce(12) || ciphertext || tag(16)``. Minimum length is 28
    # bytes (empty plaintext would be ``nonce + tag`` alone), but the
    # DB rejects ``byte_size_plaintext = 0`` so a real row always has
    # at least one plaintext byte (≥ 29 bytes of ciphertext).
    ciphertext: bytes = Field(min_length=28)

    # 12-byte AES-GCM nonce. Embedded in the leading bytes of
    # ``ciphertext`` too — duplicated here for audit queries.
    nonce: Annotated[bytes, Field(min_length=12, max_length=12)]

    # Which PDF_ENCRYPTION_KEY_V{n} sealed this row. The decrypt path
    # looks up the matching key in ``/etc/aegis/aegis.env``.
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


__all__ = ["PdfStoreRow"]
