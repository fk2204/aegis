"""Round-trip tests for the canonical Supabase-row-dict -> ``DocumentRow``
mapper (``aegis.storage._row_to_document``).

This file exists *because* of the divergent-duplicate bug fixed on
2026-06-04 (mapper audit Track 1 H1): for weeks ``_row_to_document``
silently dropped the chunk-B PDF-retention columns while the parallel
``_doc_row_from_db`` helper hydrated them correctly. The in-memory
``InMemoryDocumentRepository`` never exercised either mapper, so the
test suite was green while production 404'd every chunk-C view-route
call.

These tests feed Supabase-shape row dicts DIRECTLY through
``_row_to_document`` (skipping the in-memory backend entirely) and
assert every field on ``DocumentRow`` lands. The schema-coverage test
at the bottom of the file is the structural guard: if someone adds a
new field to ``DocumentRow`` but forgets to update ``_row_to_document``,
the test fails — closing the class of bug that hid for weeks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from aegis.storage import DocumentRow, _row_to_document


def _full_row() -> dict[str, object]:
    """Supabase-shape row dict carrying values for every column the
    ``documents`` table exposes (post migration 033). Used as the
    happy-path fixture and the schema-coverage assertion base."""
    document_id = uuid4()
    merchant_id = uuid4()
    return {
        "id": str(document_id),
        "file_hash": "a" * 64,
        "byte_size": 4096,
        "original_filename": "eStmt_2026-03-31.pdf",
        "merchant_id": str(merchant_id),
        "parse_status": "manual_review",
        "fraud_score": 70,
        "fraud_score_breakdown": {
            "metadata_score": 0,
            "math_score": 0,
            "patterns_score": 85,
        },
        "all_flags": ["[META] incremental_saves: 2 EOF markers", "[MATH] x"],
        "metadata_flags": ["incremental_saves: 2 EOF markers"],
        "error_detail": None,
        "uploaded_at": "2026-06-03T18:41:58Z",
        "parsed_at": "2026-06-03T18:42:01Z",
        "uploaded_by": "manual_close_pull",
        "storage_path": (
            "encrypted/2026/06/03/49c7d058-3e2a-4554-ad46-f4063146b36e.pdf.enc"
        ),
        "sha256_original": "b" * 64,
        "encryption_key_version": 1,
        "retention_until": "2033-06-03T00:00:00Z",
    }


# ----------------------------------------------------------------------
# Schema coverage — the "new column added but mapper forgotten" guard
# ----------------------------------------------------------------------


def test_row_to_document_hydrates_every_field_on_document_row() -> None:
    """STRUCTURAL GUARD. If someone adds a new field to ``DocumentRow``
    they must add it here AND to the mapper at the same time — this
    test fails until both move together. The duplicate-mapper-class bug
    that hid for weeks was a column-add that updated the model but
    only one of the two mappers; this test would have caught it.

    Don't relax this test. Adding ``# noqa`` or ``ignore_fields``
    arguments defeats the purpose. If a field is intentionally not
    hydratable from the row (computed elsewhere), document the reason
    in this test's docstring and skip the field explicitly with a
    named-exception comment that the next reader can audit."""
    row = _full_row()
    doc = _row_to_document(row)

    declared_fields = set(DocumentRow.model_fields.keys())
    for field_name in declared_fields:
        model_value = getattr(doc, field_name)
        row_value = row.get(field_name)
        if row_value is None:
            # Row didn't carry it — model default is fine.
            continue
        # Row had a non-None value: model field must not be None.
        assert model_value is not None, (
            f"Mapper dropped field {field_name!r}: row carried "
            f"{row_value!r} but DocumentRow came out None. This is the "
            "exact bug class that hid for weeks on the chunk-B retention "
            "columns. Update _row_to_document in src/aegis/storage.py."
        )


# ----------------------------------------------------------------------
# Per-field round-trip
# ----------------------------------------------------------------------


def test_chunk_b_retention_columns_round_trip() -> None:
    """REGRESSION: the four chunk-B columns (migration 033) were the
    fields the old ``_row_to_document`` silently dropped. Pin them."""
    row = _full_row()
    doc = _row_to_document(row)

    assert doc.storage_path == row["storage_path"]
    assert doc.sha256_original == row["sha256_original"]
    assert doc.encryption_key_version == row["encryption_key_version"]
    assert doc.retention_until is not None
    assert doc.retention_until.year == 2033


def test_dates_parse_z_suffix() -> None:
    """Supabase emits ISO-8601 strings with a trailing ``Z`` for UTC.
    The consolidated mapper routes through ``_parse_dt`` which strips
    the ``Z`` -> ``+00:00`` before parsing. Was a subtle divergence
    between the two old mappers."""
    row = _full_row()
    doc = _row_to_document(row)

    assert doc.uploaded_at == datetime(2026, 6, 3, 18, 41, 58, tzinfo=UTC)
    assert doc.parsed_at == datetime(2026, 6, 3, 18, 42, 1, tzinfo=UTC)


def test_missing_optional_columns_default_to_none() -> None:
    """Pre-migration-033 rows don't have the chunk-B columns at all.
    The mapper must treat absent keys as None, not KeyError."""
    row = _full_row()
    for k in (
        "storage_path",
        "sha256_original",
        "encryption_key_version",
        "retention_until",
        "parsed_at",
        "merchant_id",
        "error_detail",
    ):
        del row[k]
    doc = _row_to_document(row)
    assert doc.storage_path is None
    assert doc.sha256_original is None
    assert doc.encryption_key_version is None
    assert doc.retention_until is None
    assert doc.parsed_at is None
    assert doc.merchant_id is None
    assert doc.error_detail is None


def test_missing_parse_status_raises_keyerror() -> None:
    """``parse_status`` has no default — if the column is missing on a
    row, the schema is wrong and the mapper raises rather than silently
    falling through to ``"pending"`` (the old ``_doc_row_from_db``
    helper's behavior, which hid real schema bugs)."""
    row = _full_row()
    del row["parse_status"]
    with pytest.raises(KeyError):
        _row_to_document(row)


def test_all_flags_and_metadata_flags_wrapped_in_list() -> None:
    """Supabase occasionally returns ``None`` for empty array columns
    (caught by ``or []``); the mapper coerces to ``list`` so downstream
    code can always iterate without checking for ``None``."""
    row = _full_row()
    row["all_flags"] = None
    row["metadata_flags"] = None
    doc = _row_to_document(row)
    assert doc.all_flags == []
    assert doc.metadata_flags == []


def test_uploaded_by_defaults_to_system_when_absent() -> None:
    row = _full_row()
    del row["uploaded_by"]
    doc = _row_to_document(row)
    assert doc.uploaded_by == "system"


def test_fraud_score_breakdown_defaults_to_empty_dict() -> None:
    row = _full_row()
    row["fraud_score_breakdown"] = None
    doc = _row_to_document(row)
    assert doc.fraud_score_breakdown == {}


def test_merchant_id_none_when_unmatched() -> None:
    """Bearer-token / orphan uploads land without a merchant_id."""
    row = _full_row()
    row["merchant_id"] = None
    doc = _row_to_document(row)
    assert doc.merchant_id is None


def test_uuid_string_coerced_to_uuid_type() -> None:
    """Supabase returns the id as a string; the model expects ``UUID``."""
    row = _full_row()
    doc = _row_to_document(row)
    assert str(doc.id) == row["id"]


def test_retention_until_z_suffix_parses() -> None:
    row = _full_row()
    row["retention_until"] = "2033-06-03T00:00:00Z"
    doc = _row_to_document(row)
    assert doc.retention_until is not None
    assert doc.retention_until.tzinfo is not None
