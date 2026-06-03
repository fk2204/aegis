"""Unit tests for the row<->model mapping helpers in ``aegis.storage``.

These tests exercise the pure mapping functions DIRECTLY against
Supabase-shaped dicts. They run in the in-memory test backend like
every other test, but they bypass the backend entirely — feeding the
helper the exact dict shape PostgREST returns and asserting every
column round-trips onto the Pydantic model.

Why this file exists at all
---------------------------
On 2026-06-03 a chunk-C view-route 404 was traced to
``_row_to_document`` silently dropping four chunk-B retention columns
(``storage_path``, ``sha256_original``, ``encryption_key_version``,
``retention_until``). The bug had survived chunks B and C because
EVERY existing repository test runs through ``InMemoryDocumentRepository``,
which hands out the stored ``DocumentRow`` by reference and never
invokes ``_row_to_document``. The Supabase backend's mapping helper
had no direct coverage at all.

The fix is in storage.py:865; this test file is the gate that catches
a recurrence. Any future mapper (analyses, transactions, merchants,
etc.) should grow a sibling test here that feeds a realistic
PostgREST-shaped dict in and asserts every model field comes out
correctly. See ``project-in-memory-vs-supabase-test-gap`` memory.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from aegis.storage import _row_to_document

# ---------------------------------------------------------------------------
# Reference shapes
# ---------------------------------------------------------------------------


_DOC_ID = "195e8117-a75e-4ece-a6d3-96a901729052"
_FILE_HASH = "cddc19322aec497f79b109fca3991dd893b1525206cc3873b89125fe76d3ee15"
_STORAGE_PATH = f"unassigned/documents/{_DOC_ID}.pdf.enc"
_RETENTION_ISO = "2033-06-01T02:30:24.589955+00:00"
_UPLOADED_ISO = "2026-06-03T02:26:49.203751+00:00"
_PARSED_ISO = "2026-06-03T02:30:24.098563+00:00"


def _full_supabase_row() -> dict[str, object]:
    """All 18 columns PostgREST returns from ``SELECT * FROM documents``.

    Mirrors the actual row that exposed the bug — same id, hash, and
    storage_path the operator saw on 2026-06-03. Realistic values for
    every column so a regression that drops ANY field is visible in
    the assertion that follows.
    """
    return {
        "id": _DOC_ID,
        "file_hash": _FILE_HASH,
        "byte_size": 223762,
        "original_filename": "Business_Checking_Plus_x2414_Statement_May_2025.pdf",
        "merchant_id": None,
        "parse_status": "manual_review",
        "fraud_score": 12,
        "fraud_score_breakdown": {"metadata": 0, "math": 0, "patterns": 12},
        "all_flags": ["[META] some_flag", "[AGGREGATE] another"],
        "metadata_flags": ["clean"],
        "error_detail": None,
        "uploaded_at": _UPLOADED_ISO,
        "parsed_at": _PARSED_ISO,
        "uploaded_by": "token:abc12345",
        # Chunk B / migration 033 — the four columns the bug dropped.
        "storage_path": _STORAGE_PATH,
        "sha256_original": _FILE_HASH,
        "encryption_key_version": 1,
        "retention_until": _RETENTION_ISO,
    }


# ---------------------------------------------------------------------------
# The bug-gate tests
# ---------------------------------------------------------------------------


def test_row_to_document_hydrates_chunk_b_retention_columns() -> None:
    """The gate test for the 2026-06-03 chunk-C 404 incident.

    Before the fix, ``_row_to_document`` constructed ``DocumentRow``
    without passing ``storage_path``, ``sha256_original``,
    ``encryption_key_version``, or ``retention_until``, so the model's
    default of ``None`` won every time. The chunk-C view route's
    ``if doc.storage_path is None`` then 404'd on EVERY Supabase-backed
    document, even ones whose DB row had the path populated.

    This test asserts the four fields are present on the model. If any
    future repository mapper edit re-drops them, this assertion fires
    with a precise field name in the failure message.
    """
    doc = _row_to_document(_full_supabase_row())

    # The four fields the bug dropped.
    assert doc.storage_path == _STORAGE_PATH, (
        f"storage_path stripped by _row_to_document; "
        f"got {doc.storage_path!r}, expected {_STORAGE_PATH!r}"
    )
    assert doc.sha256_original == _FILE_HASH, (
        f"sha256_original stripped; got {doc.sha256_original!r}"
    )
    assert doc.encryption_key_version == 1, (
        f"encryption_key_version stripped; got {doc.encryption_key_version!r}"
    )
    assert doc.retention_until == datetime.fromisoformat(_RETENTION_ISO), (
        f"retention_until stripped or mis-parsed; got {doc.retention_until!r}"
    )


def test_row_to_document_round_trips_every_non_retention_field() -> None:
    """Belt + suspenders: every non-retention column round-trips too.

    If a future mapper edit drops a different column (say,
    ``error_detail`` or ``all_flags``) this test surfaces it with the
    field name, instead of waiting for a downstream consumer to hit
    the missing value at runtime."""
    row = _full_supabase_row()
    doc = _row_to_document(row)

    assert doc.id == UUID(_DOC_ID)
    assert doc.file_hash == _FILE_HASH
    assert doc.byte_size == 223762
    assert doc.original_filename == (
        "Business_Checking_Plus_x2414_Statement_May_2025.pdf"
    )
    assert doc.merchant_id is None
    assert doc.parse_status == "manual_review"
    assert doc.fraud_score == 12
    assert doc.fraud_score_breakdown == {"metadata": 0, "math": 0, "patterns": 12}
    assert doc.all_flags == ["[META] some_flag", "[AGGREGATE] another"]
    assert doc.metadata_flags == ["clean"]
    assert doc.error_detail is None
    assert doc.uploaded_at == datetime.fromisoformat(_UPLOADED_ISO)
    assert doc.parsed_at == datetime.fromisoformat(_PARSED_ISO)
    assert doc.uploaded_by == "token:abc12345"


def test_row_to_document_handles_legacy_row_without_chunk_b_columns() -> None:
    """Pre-migration-033 rows don't have the four chunk-B columns at
    all. PostgREST omits absent columns from the response dict (rather
    than returning ``null``), so the mapper must use ``.get(...)`` for
    the optional columns and not crash on KeyError.

    Specifically pins:
      - storage_path defaults to None (legacy row = no encrypted blob).
      - retention_until defaults to None (legacy row = no retention window).
      - The mapper does NOT raise.
    """
    legacy_row = {
        "id": _DOC_ID,
        "file_hash": _FILE_HASH,
        "byte_size": 100000,
        "original_filename": "legacy.pdf",
        "merchant_id": None,
        "parse_status": "proceed",
        "fraud_score": 0,
        "fraud_score_breakdown": {},
        "all_flags": [],
        "metadata_flags": [],
        "error_detail": None,
        "uploaded_at": _UPLOADED_ISO,
        "parsed_at": _PARSED_ISO,
        "uploaded_by": "system",
        # NO storage_path / sha256_original / encryption_key_version /
        # retention_until keys at all — pre-033 documents row shape.
    }

    doc = _row_to_document(legacy_row)
    assert doc.storage_path is None
    assert doc.sha256_original is None
    assert doc.encryption_key_version is None
    assert doc.retention_until is None
    # Sanity: the rest of the row still loaded cleanly.
    assert doc.id == UUID(_DOC_ID)
    assert doc.parse_status == "proceed"


def test_row_to_document_handles_explicit_null_retention_until() -> None:
    """Distinct from "key absent" (covered above): PostgREST CAN return
    the key with an explicit ``null`` value if the row has the column
    but it's NULL in the database. ``_parse_dt`` would raise on
    ``None``, so the mapper guards with ``.get(...)`` truthiness.
    This test pins that explicit-null path."""
    row = _full_supabase_row()
    row["retention_until"] = None
    # storage_path is set here so this isn't conflated with the
    # legacy-row test above — both shapes are legal in prod.
    row["storage_path"] = None
    row["sha256_original"] = None
    row["encryption_key_version"] = None

    doc = _row_to_document(row)
    assert doc.storage_path is None
    assert doc.sha256_original is None
    assert doc.encryption_key_version is None
    assert doc.retention_until is None


# ---------------------------------------------------------------------------
# Timezone sanity (Supabase returns ISO-8601 with offset; pin the parse)
# ---------------------------------------------------------------------------


def test_row_to_document_retention_until_preserves_timezone() -> None:
    """Supabase returns timestamps as ISO-8601 strings with the UTC
    offset (``+00:00``). The mapper must produce a tz-aware datetime;
    a naive datetime would compare-fail against ``datetime.now(UTC)``
    in the retention sweep cron and silently miss expired rows.
    """
    doc = _row_to_document(_full_supabase_row())
    assert doc.retention_until is not None
    assert doc.retention_until.tzinfo is not None
    assert doc.retention_until.utcoffset() == UTC.utcoffset(doc.retention_until)
