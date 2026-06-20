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

from aegis.merchants.repository import _merchant_to_payload, _row_to_merchant
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
    assert doc.original_filename == ("Business_Checking_Plus_x2414_Statement_May_2025.pdf")
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


# ===========================================================================
# Merchants — _row_to_merchant / _merchant_to_payload (migration 034)
# ===========================================================================
#
# Direct mapper round-trips for the new ``status`` column and the
# now-nullable business_name / owner_name / state columns.
#
# Why these tests exist: the chunk-C incident (storage.py:865 dropping
# four columns) was hidden by every test running through the in-memory
# backend. Adding a column to merchants would re-incident the same way
# if the mapper was the only updated code with no direct test.
# See ``project-in-memory-vs-supabase-test-gap``.


_MERCHANT_ID = "11111111-2222-3333-4444-555555555555"


def _finalized_merchant_row() -> dict[str, object]:
    """Full Supabase-shaped row for a normal finalized merchant.

    Matches what an existing curated merchant looks like post-migration:
    every legacy column populated, ``status='finalized'`` via the new
    column's DEFAULT.
    """
    return {
        "id": _MERCHANT_ID,
        "status": "finalized",
        "business_name": "Acme LLC",
        "dba": "Acme",
        "owner_name": "Jane Doe",
        "state": "CA",
        "industry_naics": "722511",
        "industry_risk_tier": "moderate",
        "time_in_business_months": 36,
        "credit_score": 720,
        "email": "jane@acme.example",
        "phone": "+15551234567",
        "entity_type": "llc",
        "ein": "12-3456789",
        "requested_amount": "50000.00",
        "requested_factor": "1.30",
        "requested_term_days": 180,
        "broker_source": "internal",
        "intake_date": "2026-05-01",
        "is_renewal": False,
        "preferred_funder_id": None,
        "close_lead_id": "lead_abc",
        "created_at": "2026-05-01T00:00:00+00:00",
        "updated_at": "2026-05-01T00:00:00+00:00",
    }


def test_row_to_merchant_hydrates_status_column() -> None:
    """The bug-gate test for migration 034.

    A provisional row carries the ``business_name`` placeholder set by
    ``create_provisional`` (kept non-null at the type level so the
    slugify / dossier / sort cascade doesn't need None-guards). The
    ``owner_name`` and ``state`` columns ARE nullable on provisional
    rows. The mapper must hydrate all three correctly.
    """
    from aegis.merchants.repository import PROVISIONAL_BUSINESS_NAME_PLACEHOLDER

    row = _finalized_merchant_row()
    row["status"] = "provisional"
    row["business_name"] = PROVISIONAL_BUSINESS_NAME_PLACEHOLDER
    row["owner_name"] = None
    row["state"] = None

    m = _row_to_merchant(row)

    assert m.status == "provisional"
    assert m.is_provisional is True
    assert m.needs_manual_naming is False
    assert m.is_finalized is False
    assert m.business_name == PROVISIONAL_BUSINESS_NAME_PLACEHOLDER
    assert m.owner_name is None
    assert m.state is None
    # Sanity: the rest of the row still loaded.
    assert m.id == UUID(_MERCHANT_ID)
    assert m.close_lead_id == "lead_abc"


def test_row_to_merchant_handles_legacy_row_without_status_column() -> None:
    """Pre-migration-034 rows don't have the ``status`` key at all.

    PostgREST omits absent columns from the response dict; the mapper
    must default ``status='finalized'`` (the same value the DB DEFAULT
    sets on post-034 rows). This protects reads against a replica /
    restored backup that hasn't been migrated yet.
    """
    row = _finalized_merchant_row()
    del row["status"]

    m = _row_to_merchant(row)

    assert m.status == "finalized"
    assert m.is_finalized is True
    # Sanity: the rest of the row still loaded.
    assert m.business_name == "Acme LLC"
    assert m.id == UUID(_MERCHANT_ID)


def test_row_to_merchant_finalized_with_full_fields_round_trips() -> None:
    """Existing curated merchants still load with every column.

    Belt + suspenders against a future mapper edit that drops one of
    the non-status fields. Asserts the columns we care about for the
    dashboard / scoring / Close linkage.
    """
    m = _row_to_merchant(_finalized_merchant_row())

    assert m.status == "finalized"
    assert m.business_name == "Acme LLC"
    assert m.dba == "Acme"
    assert m.owner_name == "Jane Doe"
    assert m.state == "CA"
    assert m.industry_naics == "722511"
    assert m.industry_risk_tier == "moderate"
    assert m.email == "jane@acme.example"
    assert m.close_lead_id == "lead_abc"
    assert m.entity_type == "llc"


def test_row_to_merchant_coerces_empty_owner_name_to_none() -> None:
    """ADG-Global-Express regression (2026-06-19): a Close webhook
    firing before fix `1966afa` had deployed wrote ``owner_name=""``
    to one row, and subsequent bulk ``list_all()`` reads crashed
    Pydantic ``string_too_short`` validation on every consumer. The
    mapper now coerces empty strings on read so one poisoned cell
    doesn't block the entire table from hydrating."""
    row = _finalized_merchant_row()
    row["owner_name"] = ""

    m = _row_to_merchant(row)

    assert m.owner_name is None
    assert m.business_name == "Acme LLC"  # other columns unaffected


def test_row_to_merchant_coerces_whitespace_only_owner_name_to_none() -> None:
    """A column whose value is whitespace-only ("\t   \n") is
    semantically empty too; coerce it the same way an empty string is
    handled so the strip / non-strip pair stays in lock-step."""
    row = _finalized_merchant_row()
    row["owner_name"] = "\t   \n"

    m = _row_to_merchant(row)

    assert m.owner_name is None


def test_row_to_merchant_coerces_empty_state_to_none() -> None:
    """State has both min_length=2 and max_length=2 — an empty string
    triggers ``string_too_short`` the same way owner_name does."""
    row = _finalized_merchant_row()
    row["state"] = ""

    m = _row_to_merchant(row)

    assert m.state is None


def test_row_to_merchant_preserves_valid_state_code() -> None:
    """The coercion only fires on empty inputs; a real 2-letter code
    must pass through verbatim or the round-trip is broken."""
    row = _finalized_merchant_row()
    row["state"] = "TX"

    m = _row_to_merchant(row)

    assert m.state == "TX"


def test_row_to_merchant_coerces_empty_strings_across_all_nullable_text_columns() -> None:
    """Every nullable ``str | None`` column on MerchantRow flows through
    the same coercion. This guards against a future column add that
    forgets to wrap a ``row.get`` in ``_none_if_empty`` — the regression
    is a Pydantic crash on bulk reads, same shape as the ADG incident."""
    row = _finalized_merchant_row()
    empty_value = ""
    columns_to_blank = [
        "dba",
        "owner_name",
        "state",
        "industry_naics",
        "email",
        "phone",
        "ein",
        "broker_source",
        "close_lead_id",
        "close_opportunity_id",
        "industry_choice",
        "notes",
        "deal_context",
        "close_lead_description",
        "close_notes_summary",
        "close_call_transcripts",
        "web_presence_summary",
    ]
    for col in columns_to_blank:
        row[col] = empty_value

    m = _row_to_merchant(row)

    for col in columns_to_blank:
        assert getattr(m, col) is None, f"column {col!r} not coerced to None"


def test_row_to_merchant_does_not_silence_business_name_corruption() -> None:
    """``business_name`` is NOT NULL with ``min_length=1``. An empty
    value here is real data corruption — the mapper must surface the
    Pydantic ``ValidationError`` rather than silently hydrate the row
    as ``None`` (which would also fail the NOT NULL type)."""
    import pytest as _pytest
    from pydantic import ValidationError

    row = _finalized_merchant_row()
    row["business_name"] = ""

    with _pytest.raises(ValidationError):
        _row_to_merchant(row)


def test_row_to_merchant_needs_manual_naming_status_round_trips() -> None:
    """``needs_manual_naming`` is the third valid status value. The
    placeholder ``business_name`` survives the worker's
    ``mark_needs_manual_naming`` call (which only changes status).
    Property surface differs from provisional (``needs_manual_naming``
    is True, ``is_provisional`` is False)."""
    from aegis.merchants.repository import PROVISIONAL_BUSINESS_NAME_PLACEHOLDER

    row = _finalized_merchant_row()
    row["status"] = "needs_manual_naming"
    row["business_name"] = PROVISIONAL_BUSINESS_NAME_PLACEHOLDER
    row["owner_name"] = None
    row["state"] = None

    m = _row_to_merchant(row)

    assert m.status == "needs_manual_naming"
    assert m.needs_manual_naming is True
    assert m.is_provisional is False
    assert m.is_finalized is False
    assert m.business_name == PROVISIONAL_BUSINESS_NAME_PLACEHOLDER


def test_merchant_to_payload_serializes_status() -> None:
    """The write-side mapper must include the new ``status`` field in
    the dict it produces for ``upsert``. A status drop would silently
    re-default rows to ``finalized`` on every upsert — a different bug
    shape than the chunk-C read drop, but the same class.

    Provisional shape: placeholder business_name, NULL owner/state.
    """
    from aegis.merchants.models import MerchantRow
    from aegis.merchants.repository import PROVISIONAL_BUSINESS_NAME_PLACEHOLDER

    m = MerchantRow(
        id=UUID(_MERCHANT_ID),
        status="provisional",
        business_name=PROVISIONAL_BUSINESS_NAME_PLACEHOLDER,
        owner_name=None,
        state=None,
    )

    payload = _merchant_to_payload(m)

    assert payload["status"] == "provisional"
    assert payload["business_name"] == PROVISIONAL_BUSINESS_NAME_PLACEHOLDER
    assert payload["owner_name"] is None
    assert payload["state"] is None
    # Sanity: id and the other always-present keys still serialize.
    assert payload["id"] == _MERCHANT_ID
    assert payload["is_renewal"] is False


def test_merchant_to_payload_serializes_state_none_without_crashing() -> None:
    """Pre-034 ``_merchant_to_payload`` called ``m.state.upper()``
    unconditionally — would have crashed on a None state. Post-034
    finalized merchants legitimately have NULL state until operator
    edits, so the write-side must tolerate None too."""
    from aegis.merchants.models import MerchantRow

    m = MerchantRow(
        id=UUID(_MERCHANT_ID),
        status="finalized",
        business_name="Acme LLC",
        owner_name=None,
        state=None,  # the load-bearing case
    )

    payload = _merchant_to_payload(m)
    assert payload["state"] is None  # NOT a crash; NOT an empty string
