"""pdf_store package — in-Postgres ciphertext blob for original PDFs.

Backs migration 060. The 2026-06-15 operator directive simplifies the
chunk-B retention design (Supabase Storage in ``docs/PDF_RETENTION_DESIGN.md``)
to a Postgres ``pdf_store`` table containing the AES-GCM sealed bytes,
the 12-byte nonce, the key version used to seal, and the plaintext
SHA-256 hash for integrity verification on every read.

The worker writes one row per successful parse via
:meth:`PdfStoreRepository.store` after the parse + analyses persistence
step and before the local plaintext PDF is unlinked. The view route
(``GET /ui/merchants/{merchant_id}/documents/{document_id}/pdf``) reads
via :meth:`PdfStoreRepository.fetch_plaintext` which decrypts, verifies
the SHA-256 against the stored hash, and returns the bytes.

Distinct from the legacy Supabase Storage chunk-B path in
``aegis.workers._try_encrypted_storage_step`` — that path stays in place
for now; this module is additive. The view route in chunk C reads from
the new Postgres table only.
"""

from aegis.crypto import CorruptCiphertextError
from aegis.pdf_store.models import PdfStoreRow
from aegis.pdf_store.repository import (
    InMemoryPdfStoreRepository,
    PdfStoreIntegrityError,
    PdfStoreNotFoundError,
    PdfStoreRepository,
    PdfStoreWriteError,
    SupabasePdfStoreRepository,
)

__all__ = [
    "CorruptCiphertextError",
    "InMemoryPdfStoreRepository",
    "PdfStoreIntegrityError",
    "PdfStoreNotFoundError",
    "PdfStoreRepository",
    "PdfStoreRow",
    "PdfStoreWriteError",
    "SupabasePdfStoreRepository",
]
