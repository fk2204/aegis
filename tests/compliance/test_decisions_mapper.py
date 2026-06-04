"""Round-trip tests for the ``decisions`` table mappers
(``aegis.compliance.snapshot._payload_to_row`` and
``aegis.compliance.snapshot._row_to_stored_decision``).

This file exists because of the 2026-06-04 mapper-divergence audit
(Track 3 / Finding 4 + 5): the ``decisions`` table is **immutable** at
the database layer (migration 015 installs ``block_decision_modification``
triggers that raise on UPDATE and DELETE), yet both mappers shipped
without a single round-trip test. A silently-dropped column on the
write path is unrecoverable — no UPDATE can repair it.

The audit-finding pattern matches the 2026-06-03 chunk-B
``_row_to_document`` regression: a column was added to the model, the
mapper was not updated in lockstep, and the in-memory backend bypassed
the mapper entirely so the test suite stayed green. These tests run the
mappers DIRECTLY against fully-populated Supabase-shape inputs.

What is tested:
- ``_payload_to_row``: every ``DecisionPayload`` field lands on the row;
  type coercions (Decimal, UUID, datetime → str/ISO; None preserved).
- ``_row_to_stored_decision``: every ``StoredDecision`` field hydrates
  from a fully-populated row; ISO + ``Z`` suffix parsing; None passthrough.
- Round trip (model → row → ``StoredDecision``) preserves the read-shape
  fields without loss. ``StoredDecision`` is intentionally narrow
  (``extra="ignore"``), so this round-trip only asserts that the
  read-shape subset survives — write-path completeness is asserted by
  the schema-coverage tests above.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from aegis.compliance.snapshot import (
    DecisionPayload,
    StoredDecision,
    _payload_to_row,
    _row_to_stored_decision,
)

AEGIS_VERSION = "2.0.0"
RULE_PACK_VERSION = "2026.05.18"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _full_payload(**overrides: Any) -> DecisionPayload:
    """Fully-populated ``DecisionPayload`` — every optional field set to
    a non-None value. Used to feed the schema-coverage tests so that any
    field the mapper drops fails the structural assertion.
    """
    defaults: dict[str, Any] = {
        "deal_id": uuid4(),
        "decided_by": "filip",
        "decision": "approve",
        "decision_reason_codes": ["CA_TIER1_OK", "OFAC_CLEAR"],
        "score": Decimal("72.50"),
        "score_factors": {"revenue": 25, "balance": 15, "nsf": -8},
        "analysis_id": uuid4(),
        "contributing_transaction_uuids": [uuid4(), uuid4(), uuid4()],
        "bank_statement_pdf_sha256": "a" * 64,
        "state_code": "CA",
        "cfdl_tier": 1,
        "disclosure_template_path": (
            "docs/compliance/states/CA/03_disclosure_template.j2"
        ),
        "disclosure_template_sha256": "b" * 64,
        "disclosure_pdf_sha256": "c" * 64,
        "apr_calculated": Decimal("32.4500"),
        "apr_method": "reg_z_1026_22",
        "ofac_cache_timestamp": datetime(2026, 5, 18, 9, 30, 0, tzinfo=UTC),
        "ofac_cache_sha256": "d" * 64,
        "aegis_version": AEGIS_VERSION,
        "rule_pack_version": RULE_PACK_VERSION,
        "backfill_quality": "partial",
        "decided_at": datetime(2026, 5, 18, 10, 0, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return DecisionPayload(**defaults)


def _full_row(**overrides: Any) -> dict[str, Any]:
    """Supabase-shape row dict with non-None values for every column the
    decisions table exposes (post migration 015). Mirrors the
    ``_payload_to_row`` output for a fully-populated payload."""
    row: dict[str, Any] = {
        "id": str(uuid4()),
        "deal_id": str(uuid4()),
        "decided_at": "2026-05-18T10:00:00+00:00",
        "decided_by": "filip",
        "decision": "approve",
        "decision_reason_codes": ["CA_TIER1_OK", "OFAC_CLEAR"],
        "score": "72.50",
        "score_factors": {"revenue": 25, "balance": 15, "nsf": -8},
        "analysis_id": str(uuid4()),
        "contributing_transaction_uuids": [str(uuid4()), str(uuid4())],
        "bank_statement_pdf_sha256": "a" * 64,
        "state_code": "CA",
        "cfdl_tier": 1,
        "disclosure_template_path": (
            "docs/compliance/states/CA/03_disclosure_template.j2"
        ),
        "disclosure_template_sha256": "b" * 64,
        "disclosure_pdf_sha256": "c" * 64,
        "apr_calculated": "32.4500",
        "apr_method": "reg_z_1026_22",
        "ofac_cache_timestamp": "2026-05-18T09:30:00+00:00",
        "ofac_cache_sha256": "d" * 64,
        "aegis_version": AEGIS_VERSION,
        "rule_pack_version": RULE_PACK_VERSION,
        "backfill_quality": "partial",
    }
    row.update(overrides)
    return row


# ===========================================================================
# _payload_to_row — model → DB row dict
# ===========================================================================


# ----------------------------------------------------------------------
# Schema coverage — the "new column added but mapper forgotten" guard
# ----------------------------------------------------------------------


def test_payload_to_row_writes_every_payload_field() -> None:
    """STRUCTURAL GUARD. If someone adds a new field to ``DecisionPayload``
    they MUST add it to ``_payload_to_row`` at the same time — this test
    fails until both move together.

    The decisions table is immutable at the DB layer (migration 015's
    ``block_decision_modification`` trigger raises on UPDATE/DELETE). A
    silently-dropped column on the write path is unrecoverable — no
    backfill UPDATE can repair the column, because UPDATE itself is
    blocked. Adding a new ``DecisionPayload`` field without updating
    ``_payload_to_row`` is therefore a permanent regulator-defense gap.

    Don't relax this test. If a field is genuinely not persistable
    (synthesized server-side, computed elsewhere), document the
    reasoning here and add the explicit skip with a named comment that
    the next reader can audit.
    """
    payload = _full_payload()
    row_id = uuid4()
    row = _payload_to_row(payload, row_id=row_id)

    declared_fields = set(DecisionPayload.model_fields.keys())
    for field_name in declared_fields:
        # Every payload field appears as a row key — even when its value
        # is None, the key must be present (decisions schema has
        # NOT-NULL on some columns; absence is a different bug than None).
        assert field_name in row, (
            f"Mapper dropped field {field_name!r}: it is declared on "
            f"DecisionPayload but not written by _payload_to_row. The "
            f"decisions table is IMMUTABLE — no UPDATE can repair a "
            f"dropped column. Update _payload_to_row in "
            f"src/aegis/compliance/snapshot.py."
        )

    # Synthetic columns owned by the mapper.
    assert "id" in row, "_payload_to_row must inject the row id"


def test_payload_to_row_row_id_is_serialized_uuid() -> None:
    """``row_id`` is injected by the mapper, not by the payload. It must
    arrive on the row as a string (Postgres uuid column accepts string)."""
    payload = _full_payload()
    row_id = uuid4()
    row = _payload_to_row(payload, row_id=row_id)

    assert row["id"] == str(row_id)
    assert isinstance(row["id"], str)


# ----------------------------------------------------------------------
# Per-field serialization
# ----------------------------------------------------------------------


def test_uuid_fields_become_strings() -> None:
    """UUIDs must be stringified — supabase-py's PostgREST JSON encoder
    handles strings natively. Pass a raw ``UUID`` and it will TypeError."""
    payload = _full_payload()
    row = _payload_to_row(payload, row_id=uuid4())

    assert isinstance(row["deal_id"], str)
    assert row["deal_id"] == str(payload.deal_id)
    assert isinstance(row["analysis_id"], str)
    assert row["analysis_id"] == str(payload.analysis_id)
    for tx_str in row["contributing_transaction_uuids"]:
        assert isinstance(tx_str, str)
    assert row["contributing_transaction_uuids"] == [
        str(u) for u in payload.contributing_transaction_uuids
    ]


def test_decimal_fields_become_strings() -> None:
    """Decimal must be stringified to avoid float-coercion through JSON.
    Postgres numeric accepts strings."""
    payload = _full_payload()
    row = _payload_to_row(payload, row_id=uuid4())

    assert isinstance(row["score"], str)
    assert row["score"] == "72.50"
    assert isinstance(row["apr_calculated"], str)
    assert row["apr_calculated"] == "32.4500"


def test_datetime_fields_become_iso_strings() -> None:
    """datetimes go to ISO-8601 (Postgres timestamptz parses ISO)."""
    payload = _full_payload()
    row = _payload_to_row(payload, row_id=uuid4())

    assert isinstance(row["decided_at"], str)
    assert row["decided_at"] == "2026-05-18T10:00:00+00:00"
    assert isinstance(row["ofac_cache_timestamp"], str)
    assert row["ofac_cache_timestamp"] == "2026-05-18T09:30:00+00:00"


def test_none_decimal_score_stays_none() -> None:
    """None score must NOT serialize to ``"None"`` (would become a non-
    null string and break the NUMERIC column on insert)."""
    payload = _full_payload(score=None)
    row = _payload_to_row(payload, row_id=uuid4())

    assert row["score"] is None


def test_none_decimal_apr_calculated_stays_none() -> None:
    payload = _full_payload(apr_calculated=None)
    row = _payload_to_row(payload, row_id=uuid4())

    assert row["apr_calculated"] is None


def test_none_uuid_analysis_id_stays_none() -> None:
    """``analysis_id`` is nullable on the column. None must passthrough,
    NOT serialize to ``"None"`` (would fail the UUID column type)."""
    payload = _full_payload(analysis_id=None)
    row = _payload_to_row(payload, row_id=uuid4())

    assert row["analysis_id"] is None


def test_none_datetime_decided_at_stays_none() -> None:
    """``decided_at=None`` means "let Postgres default to NOW()". The
    mapper must passthrough None (do NOT emit ``"None"`` or current time)."""
    payload = _full_payload(decided_at=None)
    row = _payload_to_row(payload, row_id=uuid4())

    assert row["decided_at"] is None


def test_none_datetime_ofac_cache_timestamp_stays_none() -> None:
    payload = _full_payload(ofac_cache_timestamp=None)
    row = _payload_to_row(payload, row_id=uuid4())

    assert row["ofac_cache_timestamp"] is None


def test_none_backfill_quality_stays_none() -> None:
    """``backfill_quality=None`` = live decision row. Mapper must NOT
    coerce to a string (DB CHECK constraint forbids non-enum strings)."""
    payload = _full_payload(backfill_quality=None)
    row = _payload_to_row(payload, row_id=uuid4())

    assert row["backfill_quality"] is None


def test_empty_contributing_transaction_uuids_is_empty_list() -> None:
    """The DB column defaults to ``'{}'`` (empty array). Mapper must
    emit an empty list, not None."""
    payload = _full_payload(contributing_transaction_uuids=[])
    row = _payload_to_row(payload, row_id=uuid4())

    assert row["contributing_transaction_uuids"] == []


def test_empty_decision_reason_codes_is_empty_list() -> None:
    """The DB column is NOT NULL; empty list is the only valid 'no
    reasons' representation."""
    payload = _full_payload(decision_reason_codes=[])
    row = _payload_to_row(payload, row_id=uuid4())

    assert row["decision_reason_codes"] == []


def test_score_factors_passes_through_as_dict() -> None:
    """``score_factors`` is JSONB on the DB. Mapper must NOT re-encode
    to a string — supabase-py serializes via ``json.dumps`` itself.

    Identity is NOT asserted because Pydantic's strict+frozen config
    defensively copies the input dict at construction time. The
    semantic contract is "passthrough as dict, not pre-serialized
    string"."""
    factors = {"revenue": 25, "balance": Decimal("15.5"), "nsf": -8}
    payload = _full_payload(score_factors=factors)
    row = _payload_to_row(payload, row_id=uuid4())

    assert isinstance(row["score_factors"], dict)
    assert row["score_factors"] == factors
    # NOT a stringified JSON blob.
    assert not isinstance(row["score_factors"], str)


def test_string_fields_unchanged() -> None:
    """No coercion on plain TEXT fields — verify the mapper does not
    accidentally re-cast / strip these."""
    payload = _full_payload()
    row = _payload_to_row(payload, row_id=uuid4())

    assert row["decided_by"] == payload.decided_by
    assert row["decision"] == payload.decision
    assert row["state_code"] == payload.state_code
    assert row["cfdl_tier"] == payload.cfdl_tier
    assert row["disclosure_template_path"] == payload.disclosure_template_path
    assert row["disclosure_template_sha256"] == payload.disclosure_template_sha256
    assert row["disclosure_pdf_sha256"] == payload.disclosure_pdf_sha256
    assert row["apr_method"] == payload.apr_method
    assert row["bank_statement_pdf_sha256"] == payload.bank_statement_pdf_sha256
    assert row["ofac_cache_sha256"] == payload.ofac_cache_sha256
    assert row["aegis_version"] == payload.aegis_version
    assert row["rule_pack_version"] == payload.rule_pack_version


# ===========================================================================
# _row_to_stored_decision — DB row dict → model
# ===========================================================================


# ----------------------------------------------------------------------
# Schema coverage — every read-shape field hydrates from a populated row
# ----------------------------------------------------------------------


def test_row_to_stored_decision_hydrates_every_field_on_stored_decision() -> None:
    """STRUCTURAL GUARD for the read path. If someone adds a new field
    to ``StoredDecision``, they must update ``_row_to_stored_decision``
    at the same time — this test fails until both move together.

    Mirrors the ``_row_to_document`` schema-coverage pattern that
    surfaced the chunk-B regression. ``StoredDecision`` uses
    ``extra="ignore"`` (read-shape is intentionally narrow), so model
    construction will silently tolerate a row that omits fields the
    model expects — the silent gap is on the MAPPER, which is exactly
    why this assertion has to be model-driven.
    """
    row = _full_row()
    decision = _row_to_stored_decision(row)

    declared_fields = set(StoredDecision.model_fields.keys())
    for field_name in declared_fields:
        model_value = getattr(decision, field_name)
        # The fully-populated row sets every field to a non-None value.
        # A None on the model side after this mapping means the mapper
        # dropped the field.
        assert model_value is not None, (
            f"Mapper dropped field {field_name!r}: row carried a non-None "
            f"value but StoredDecision came out None. Update "
            f"_row_to_stored_decision in "
            f"src/aegis/compliance/snapshot.py."
        )


# ----------------------------------------------------------------------
# Per-field coercion
# ----------------------------------------------------------------------


def test_id_string_coerced_to_uuid() -> None:
    row = _full_row()
    decision = _row_to_stored_decision(row)

    assert isinstance(decision.id, UUID)
    assert str(decision.id) == row["id"]


def test_deal_id_string_coerced_to_uuid() -> None:
    row = _full_row()
    decision = _row_to_stored_decision(row)

    assert isinstance(decision.deal_id, UUID)
    assert str(decision.deal_id) == row["deal_id"]


def test_score_string_coerced_to_decimal() -> None:
    """Numeric arrives from supabase-py as a string. Mapper must coerce
    to Decimal so downstream money math doesn't accidentally accept
    a float."""
    row = _full_row()
    decision = _row_to_stored_decision(row)

    assert isinstance(decision.score, Decimal)
    assert decision.score == Decimal("72.50")


def test_score_none_passes_through() -> None:
    """Nullable column — None must NOT coerce to Decimal("None")."""
    row = _full_row()
    row["score"] = None
    decision = _row_to_stored_decision(row)

    assert decision.score is None


def test_decided_at_iso_string_parses_to_datetime() -> None:
    row = _full_row()
    decision = _row_to_stored_decision(row)

    assert isinstance(decision.decided_at, datetime)
    assert decision.decided_at == datetime(2026, 5, 18, 10, 0, 0, tzinfo=UTC)


def test_decided_at_z_suffix_parses() -> None:
    """Supabase emits timestamptz with a trailing ``Z`` in some shapes;
    the mapper replaces ``Z`` with ``+00:00`` before parsing."""
    row = _full_row()
    row["decided_at"] = "2026-05-18T10:00:00Z"
    decision = _row_to_stored_decision(row)

    assert decision.decided_at == datetime(2026, 5, 18, 10, 0, 0, tzinfo=UTC)


def test_decided_at_none_passes_through() -> None:
    row = _full_row()
    row["decided_at"] = None
    decision = _row_to_stored_decision(row)

    assert decision.decided_at is None


def test_decided_at_native_datetime_passes_through() -> None:
    """If a future supabase-py upgrade returns datetime objects directly
    (instead of ISO strings), the mapper must accept them unchanged."""
    row = _full_row()
    native = datetime(2026, 5, 18, 10, 0, 0, tzinfo=UTC)
    row["decided_at"] = native
    decision = _row_to_stored_decision(row)

    assert decision.decided_at == native


def test_ofac_cache_timestamp_z_suffix_parses() -> None:
    row = _full_row()
    row["ofac_cache_timestamp"] = "2026-05-18T09:30:00Z"
    decision = _row_to_stored_decision(row)

    assert decision.ofac_cache_timestamp == datetime(
        2026, 5, 18, 9, 30, 0, tzinfo=UTC
    )


def test_ofac_cache_timestamp_none_passes_through() -> None:
    row = _full_row()
    row["ofac_cache_timestamp"] = None
    decision = _row_to_stored_decision(row)

    assert decision.ofac_cache_timestamp is None


def test_decision_reason_codes_wrapped_in_list_when_none() -> None:
    """Supabase occasionally returns ``None`` for empty array columns
    (caught by ``or []``); the mapper coerces to ``list`` so downstream
    code can always iterate without checking for ``None``."""
    row = _full_row()
    row["decision_reason_codes"] = None
    decision = _row_to_stored_decision(row)

    assert decision.decision_reason_codes == []


def test_decision_reason_codes_passthrough() -> None:
    row = _full_row()
    decision = _row_to_stored_decision(row)

    assert decision.decision_reason_codes == ["CA_TIER1_OK", "OFAC_CLEAR"]


def test_decision_literal_passthrough() -> None:
    row = _full_row(decision="decline")
    decision = _row_to_stored_decision(row)

    assert decision.decision == "decline"


def test_unknown_columns_silently_ignored() -> None:
    """``StoredDecision.model_config`` is ``extra="ignore"``. Verified
    in compliance/snapshot.py:109. A column the read-shape doesn't
    declare (e.g. ``aegis_version``, ``score_factors``) must NOT raise
    a ValidationError — this is intentional read-narrowness."""
    row = _full_row()
    # The full row contains many columns StoredDecision does NOT declare
    # (state_code, cfdl_tier, aegis_version, score_factors, ...).
    decision = _row_to_stored_decision(row)

    # Should not have grown any of those as attributes.
    assert not hasattr(decision, "state_code")
    assert not hasattr(decision, "aegis_version")
    assert not hasattr(decision, "score_factors")


def test_missing_required_id_raises() -> None:
    """``id`` has no .get() fallback — schema corruption (RLS strip,
    a SELECT that forgot ``id``) must FAIL FAST, not silently coerce to
    something else."""
    row = _full_row()
    del row["id"]
    with pytest.raises(KeyError):
        _row_to_stored_decision(row)


def test_missing_required_deal_id_raises() -> None:
    row = _full_row()
    del row["deal_id"]
    with pytest.raises(KeyError):
        _row_to_stored_decision(row)


def test_missing_required_decision_raises() -> None:
    row = _full_row()
    del row["decision"]
    with pytest.raises(KeyError):
        _row_to_stored_decision(row)


# ===========================================================================
# Round trip — model → row → model
# ===========================================================================


def test_round_trip_preserves_read_shape_fields() -> None:
    """Feed model → ``_payload_to_row`` → ``_row_to_stored_decision`` →
    assert the read-shape subset survives the immutable-table write.

    ``StoredDecision`` is intentionally narrow (``extra="ignore"``);
    the write path captures 22 fields and the read path projects 7.
    This round-trip verifies the 7 read-shape fields survive end-to-end
    with no type drift. Write-path completeness (all 22 fields) is the
    job of ``test_payload_to_row_writes_every_payload_field``.
    """
    payload = _full_payload()
    row_id = uuid4()
    row = _payload_to_row(payload, row_id=row_id)
    decision = _row_to_stored_decision(row)

    assert decision.id == row_id
    assert decision.deal_id == payload.deal_id
    assert decision.decision == payload.decision
    assert decision.score == payload.score
    assert decision.decision_reason_codes == payload.decision_reason_codes
    assert decision.ofac_cache_timestamp == payload.ofac_cache_timestamp
    assert decision.decided_at == payload.decided_at


def test_round_trip_with_all_optionals_none() -> None:
    """Sparse payload — every optional field set to None — must
    round-trip without raising ValidationError on either leg."""
    payload = _full_payload(
        score=None,
        analysis_id=None,
        contributing_transaction_uuids=[],
        bank_statement_pdf_sha256=None,
        disclosure_template_path=None,
        disclosure_template_sha256=None,
        disclosure_pdf_sha256=None,
        apr_calculated=None,
        apr_method=None,
        ofac_cache_timestamp=None,
        ofac_cache_sha256=None,
        backfill_quality=None,
        decided_at=None,
    )
    row_id = uuid4()
    row = _payload_to_row(payload, row_id=row_id)
    decision = _row_to_stored_decision(row)

    assert decision.id == row_id
    assert decision.deal_id == payload.deal_id
    assert decision.decision == payload.decision
    assert decision.score is None
    assert decision.ofac_cache_timestamp is None
    assert decision.decided_at is None


def test_round_trip_preserves_decimal_precision() -> None:
    """Round-tripping money math through string serialization must NOT
    lose precision. Use a value that would round if coerced to float."""
    score = Decimal("88.33")
    apr = Decimal("0.0001")
    payload = _full_payload(score=score, apr_calculated=apr)
    row = _payload_to_row(payload, row_id=uuid4())
    decision = _row_to_stored_decision(row)

    assert decision.score == score
    assert isinstance(decision.score, Decimal)


def test_round_trip_preserves_reason_codes_order() -> None:
    """The reason codes list is consumed in order downstream (rendered in
    the dossier, sent to Close in display order). Round trip must
    preserve sequence, not just set-equality."""
    payload = _full_payload(
        decision_reason_codes=["FIRST", "SECOND", "THIRD", "FOURTH"]
    )
    row = _payload_to_row(payload, row_id=uuid4())
    decision = _row_to_stored_decision(row)

    assert decision.decision_reason_codes == ["FIRST", "SECOND", "THIRD", "FOURTH"]


def test_round_trip_redisclosure_decision_literal() -> None:
    """Every ``DecisionLiteral`` value must round-trip. The full set is
    pinned by migration 015's CHECK constraint, so a typo in either
    mapper would split prod-DB and Python."""
    for decision_value in ("approve", "decline", "manual_review", "redisclosure"):
        payload = _full_payload(decision=decision_value)
        row = _payload_to_row(payload, row_id=uuid4())
        restored = _row_to_stored_decision(row)
        assert restored.decision == decision_value


# ===========================================================================
# Negative cases on the payload side — confirm strict-mode behaviour
# carries through the mapper unchanged
# ===========================================================================


def test_invalid_decision_value_rejected_at_payload_construction() -> None:
    """Mapper never sees an invalid decision — the model rejects it at
    construction. Pin the contract so a future loosening of the model is
    visible in the test diff."""
    with pytest.raises(ValidationError):
        _full_payload(decision="yolo")


def test_state_code_length_enforced_at_payload_construction() -> None:
    with pytest.raises(ValidationError):
        _full_payload(state_code="CAL")


def test_cfdl_tier_range_enforced_at_payload_construction() -> None:
    with pytest.raises(ValidationError):
        _full_payload(cfdl_tier=4)
