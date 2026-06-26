"""Round-trip tests for the canonical ``analyses`` table mappers
(``aegis.storage._analysis_to_db_row`` + ``aegis.storage._db_row_to_analysis``).

These two helpers DO already have happy-path round-trip coverage in
``tests/test_storage.py`` (focused on the migration-032 ``pattern_analysis``
jsonb column). What's missing — and what this file adds — is the
**structural schema-coverage guard** modelled on the H1 fix at
``tests/storage/test_row_to_document_mapper.py``: introspect
``AnalysisRow.model_fields``, feed a fully-populated Supabase-shape row,
and assert every field with a non-None row value lands non-None on the
constructed model. This closes the "operator added a new column to the
model but forgot the mapper" bug class — the exact class that silently
dropped the chunk-B PDF-retention columns from ``_row_to_document`` for
weeks.

Plus per-field round-trip pins for the five ``_source_ids`` arrays,
``monthly_breakdown``, ``account_last4``, and ``bank_name`` — none of
which are covered by the existing ``tests/test_storage.py`` tests, which
focus narrowly on ``pattern_analysis``.

NOTE — scope: these tests exercise the helpers directly (no Supabase
client, no in-memory backend). They run offline; the bug class they
catch only manifests when the Supabase backend is the active binding,
which is never exercised in CI today (per the audit at
``docs/audit-mapper-divergence.md`` §"Test coverage gap").
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from aegis.parser.patterns import (
    CounterpartySignalsDTO,
    McaPositionDTO,
    PatternAnalysisDTO,
    PatternDTO,
)
from aegis.storage import (
    AnalysisRow,
    _analysis_to_db_row,
    _db_row_to_analysis,
)


def _full_analysis_row() -> AnalysisRow:
    """An ``AnalysisRow`` with EVERY field populated to a distinguishable
    non-default value. Used as the round-trip source — every read-side
    field must equal the corresponding write-side field after a
    ``_analysis_to_db_row`` -> ``_db_row_to_analysis`` cycle.
    """
    adb_src = [uuid4(), uuid4()]
    rev_src = [uuid4()]
    nsf_src = [uuid4()]
    neg_src = [uuid4(), uuid4(), uuid4()]
    mca_src = [uuid4()]
    return AnalysisRow(
        id=uuid4(),
        document_id=uuid4(),
        merchant_id=uuid4(),
        statement_period_start=date(2026, 3, 1),
        statement_period_end=date(2026, 3, 31),
        statement_days=31,
        beginning_balance=Decimal("1234.56"),
        ending_balance=Decimal("4321.00"),
        avg_daily_balance=Decimal("2500.25"),
        true_revenue=Decimal("9876.54"),
        monthly_revenue=Decimal("9876.54"),
        lowest_balance=Decimal("-100.99"),
        num_nsf=4,
        days_negative=2,
        mca_positions=3,
        mca_daily_total=Decimal("789.10"),
        debt_to_revenue=Decimal("0.42"),
        payroll_detected=True,
        returned_ach_count=7,
        avg_daily_balance_source_ids=adb_src,
        true_revenue_source_ids=rev_src,
        num_nsf_source_ids=nsf_src,
        days_negative_source_ids=neg_src,
        mca_daily_total_source_ids=mca_src,
        monthly_breakdown=[
            {
                "month": "2026-03",
                "deposits": "9876.54",
                "withdrawals": "5555.55",
                "avg_balance": "2500.25",
            }
        ],
        bank_name="Chase",
        account_last4="1234",
        account_holder="Acme LLC",
        pattern_analysis=PatternAnalysisDTO(
            schema_version=1,
            patterns=[
                PatternDTO(
                    code="mca_stacking",
                    severity=30,
                    detail="3 MCA position(s) detected",
                    source_ids=mca_src,
                )
            ],
            mca_positions=[
                McaPositionDTO(
                    funder_label="OnDeck",
                    daily_equivalent=Decimal("123.45"),
                    occurrences=10,
                    source_ids=mca_src,
                )
            ],
            has_kiting=False,
            paydown_suspected=False,
            counterparty_signals=CounterpartySignalsDTO(),
            payroll_present=True,
            acceleration_clause_triggered=False,
            unauthorized_withdrawal_dispute=False,
            ai_generated_score=11,
        ),
        narrator_summary={
            "deal_summary": "Test merchant averaging $9,876/month with clean cashflow.",
            "flag_explanations": [],
            "recommended_action": {
                "action": "submit_now",
                "next_step": "Submit the package.",
                "top_funder_match": "TestFunder",
                "estimated_terms": "1.30 factor, 6 months, $50k advance",
            },
            "model_id": "us.anthropic.claude-sonnet-4-6",
            "generated_at": "2026-06-26T17:00:00+00:00",
            "version": 1,
        },
    )


# ----------------------------------------------------------------------
# Structural guard — the "new column added but mapper forgotten" guard
# ----------------------------------------------------------------------


def test_analysis_to_db_row_writes_every_field_on_analysis_row() -> None:
    """STRUCTURAL GUARD (write side). For every field declared on
    ``AnalysisRow`` that the source instance set to a non-default value,
    the resulting db dict must carry a non-None entry under the same key.

    This catches the "operator added a new field to AnalysisRow but
    forgot to extend ``_analysis_to_db_row``" regression — the same bug
    class that hid in ``_row_to_document`` for weeks (per
    docs/audit-mapper-divergence.md §HIGH 1). The `analyses` table is
    less dramatic than the immutable `decisions` table, but a silent
    drop on the write path means the column will read back as None on
    every subsequent fetch — and the in-memory backend will mask it.
    """
    src = _full_analysis_row()
    db_row = _analysis_to_db_row(src)

    declared_fields = set(AnalysisRow.model_fields.keys())
    missing: list[str] = []
    for field_name in declared_fields:
        if field_name not in db_row:
            missing.append(field_name)
            continue
        # The mapper is allowed to write `None` for nullable fields, but
        # only when the source carried None. Every field in our full row
        # is populated; nothing should land as None.
        if db_row[field_name] is None:
            missing.append(field_name)

    assert not missing, (
        f"Mapper _analysis_to_db_row dropped fields: {missing!r}. "
        "Every populated AnalysisRow field must land in the db dict — "
        "extend the mapper in src/aegis/storage.py."
    )


def test_db_row_to_analysis_hydrates_every_field_on_analysis_row() -> None:
    """STRUCTURAL GUARD (read side). Round-trip the full row through
    write -> read; every field on ``AnalysisRow`` whose write-side value
    was non-None must come back non-None on the rebuilt model.

    Mirrors ``test_row_to_document_hydrates_every_field_on_document_row``
    in ``test_row_to_document_mapper.py`` — same bug class, different
    mapper pair.
    """
    src = _full_analysis_row()
    db_row = _analysis_to_db_row(src)
    restored = _db_row_to_analysis(db_row)

    declared_fields = set(AnalysisRow.model_fields.keys())
    for field_name in declared_fields:
        write_value = db_row.get(field_name)
        if write_value is None:
            continue
        read_value = getattr(restored, field_name)
        assert read_value is not None, (
            f"Mapper _db_row_to_analysis dropped field {field_name!r}: "
            f"db row carried {write_value!r} but rebuilt AnalysisRow "
            "came out None. This is the exact bug class that hid for "
            "weeks on the chunk-B retention columns. Update "
            "_db_row_to_analysis in src/aegis/storage.py."
        )


# ----------------------------------------------------------------------
# Per-field round-trip — scalar Decimal money columns
# ----------------------------------------------------------------------


def test_money_columns_round_trip_as_exact_decimal() -> None:
    """Decimal money values survive the str-serialize / Decimal-parse
    cycle without precision loss. Pinning EXACT equality — not
    ``money_eq`` tolerance — because we want any future change to the
    serializer (e.g. ``mode="json"`` accidentally numeric'ing the value)
    to fail this test loudly."""
    src = _full_analysis_row()
    db_row = _analysis_to_db_row(src)
    restored = _db_row_to_analysis(db_row)

    assert restored.beginning_balance == Decimal("1234.56")
    assert restored.ending_balance == Decimal("4321.00")
    assert restored.avg_daily_balance == Decimal("2500.25")
    assert restored.true_revenue == Decimal("9876.54")
    assert restored.monthly_revenue == Decimal("9876.54")
    assert restored.lowest_balance == Decimal("-100.99")
    assert restored.mca_daily_total == Decimal("789.10")
    assert restored.debt_to_revenue == Decimal("0.42")


def test_lowest_balance_handles_negative_decimal() -> None:
    """Negative balances are common (NSF, overdrawn). Make sure the
    str-Decimal coercion doesn't swallow the minus sign."""
    src = _full_analysis_row()
    db_row = _analysis_to_db_row(src)
    assert db_row["lowest_balance"] == "-100.99"
    restored = _db_row_to_analysis(db_row)
    assert restored.lowest_balance < Decimal("0")


# ----------------------------------------------------------------------
# Per-field round-trip — UUID + integer + bool scalars
# ----------------------------------------------------------------------


def test_uuids_round_trip_as_uuid_type() -> None:
    """Mapper stringifies UUIDs on write; deserializer wraps them in
    ``UUID(...)`` on read. Both halves must hold."""
    src = _full_analysis_row()
    db_row = _analysis_to_db_row(src)

    assert isinstance(db_row["id"], str)
    assert isinstance(db_row["document_id"], str)
    assert isinstance(db_row["merchant_id"], str)

    restored = _db_row_to_analysis(db_row)
    assert isinstance(restored.id, UUID)
    assert isinstance(restored.document_id, UUID)
    assert restored.merchant_id is not None
    assert isinstance(restored.merchant_id, UUID)
    assert restored.id == src.id
    assert restored.document_id == src.document_id
    assert restored.merchant_id == src.merchant_id


def test_merchant_id_none_writes_and_reads_as_none() -> None:
    """Orphan analyses (no merchant matched yet) write merchant_id as
    None, and read it back as None — not as the literal string
    ``"None"`` (which would happen if the mapper used a naive
    ``str(...)`` wrap without the None guard)."""
    src = _full_analysis_row()
    src.merchant_id = None
    db_row = _analysis_to_db_row(src)
    assert db_row["merchant_id"] is None

    restored = _db_row_to_analysis(db_row)
    assert restored.merchant_id is None


def test_integer_and_bool_counts_round_trip() -> None:
    src = _full_analysis_row()
    db_row = _analysis_to_db_row(src)
    restored = _db_row_to_analysis(db_row)

    assert restored.statement_days == 31
    assert restored.num_nsf == 4
    assert restored.days_negative == 2
    assert restored.mca_positions == 3
    assert restored.returned_ach_count == 7
    assert restored.payroll_detected is True


def test_returned_ach_count_defaults_to_zero_when_absent() -> None:
    """Pre-migration rows lack the ``returned_ach_count`` column. The
    deserializer uses ``row.get("returned_ach_count", 0)`` — pin the
    default behavior."""
    src = _full_analysis_row()
    db_row = _analysis_to_db_row(src)
    del db_row["returned_ach_count"]
    restored = _db_row_to_analysis(db_row)
    assert restored.returned_ach_count == 0


# ----------------------------------------------------------------------
# Per-field round-trip — date scalars
# ----------------------------------------------------------------------


def test_dates_round_trip_through_isoformat() -> None:
    src = _full_analysis_row()
    db_row = _analysis_to_db_row(src)
    assert db_row["statement_period_start"] == "2026-03-01"
    assert db_row["statement_period_end"] == "2026-03-31"

    restored = _db_row_to_analysis(db_row)
    assert restored.statement_period_start == date(2026, 3, 1)
    assert restored.statement_period_end == date(2026, 3, 31)


# ----------------------------------------------------------------------
# Per-field round-trip — the five _source_ids arrays
# ----------------------------------------------------------------------


def test_all_source_id_arrays_round_trip() -> None:
    """Every aggregate metric carries a list of contributing transaction
    UUIDs (the auditability requirement in CLAUDE.md). All five arrays
    must round-trip — UUID -> str on write, str -> UUID on read — with
    order preserved."""
    src = _full_analysis_row()
    db_row = _analysis_to_db_row(src)
    restored = _db_row_to_analysis(db_row)

    assert restored.avg_daily_balance_source_ids == src.avg_daily_balance_source_ids
    assert restored.true_revenue_source_ids == src.true_revenue_source_ids
    assert restored.num_nsf_source_ids == src.num_nsf_source_ids
    assert restored.days_negative_source_ids == src.days_negative_source_ids
    assert restored.mca_daily_total_source_ids == src.mca_daily_total_source_ids


def test_source_id_arrays_default_to_empty_list_when_absent() -> None:
    """Pre-aggregate-arrays rows have these keys absent or null.
    Deserializer uses ``or []`` — confirm an empty list, not None."""
    src = _full_analysis_row()
    db_row = _analysis_to_db_row(src)
    for key in (
        "avg_daily_balance_source_ids",
        "true_revenue_source_ids",
        "num_nsf_source_ids",
        "days_negative_source_ids",
        "mca_daily_total_source_ids",
    ):
        db_row[key] = None

    restored = _db_row_to_analysis(db_row)
    assert restored.avg_daily_balance_source_ids == []
    assert restored.true_revenue_source_ids == []
    assert restored.num_nsf_source_ids == []
    assert restored.days_negative_source_ids == []
    assert restored.mca_daily_total_source_ids == []


# ----------------------------------------------------------------------
# Per-field round-trip — monthly_breakdown + bank identity
# ----------------------------------------------------------------------


def test_monthly_breakdown_jsonb_round_trips_as_list_of_dicts() -> None:
    src = _full_analysis_row()
    db_row = _analysis_to_db_row(src)
    restored = _db_row_to_analysis(db_row)

    assert restored.monthly_breakdown == src.monthly_breakdown
    assert restored.monthly_breakdown[0]["month"] == "2026-03"
    assert restored.monthly_breakdown[0]["deposits"] == "9876.54"


def test_monthly_breakdown_defaults_to_empty_list_when_absent() -> None:
    src = _full_analysis_row()
    db_row = _analysis_to_db_row(src)
    db_row["monthly_breakdown"] = None
    restored = _db_row_to_analysis(db_row)
    assert restored.monthly_breakdown == []


def test_bank_name_and_account_last4_round_trip() -> None:
    src = _full_analysis_row()
    db_row = _analysis_to_db_row(src)
    restored = _db_row_to_analysis(db_row)

    assert restored.bank_name == "Chase"
    assert restored.account_last4 == "1234"


def test_bank_identity_defaults_to_none_when_absent() -> None:
    src = _full_analysis_row()
    src.bank_name = None
    src.account_last4 = None
    db_row = _analysis_to_db_row(src)
    restored = _db_row_to_analysis(db_row)
    assert restored.bank_name is None
    assert restored.account_last4 is None


# ----------------------------------------------------------------------
# Per-field round-trip — pattern_analysis jsonb (migration 032)
# ----------------------------------------------------------------------


def test_pattern_analysis_round_trips_as_dto() -> None:
    """Already covered narrowly in ``tests/test_storage.py``; pin here
    too so this file is self-contained and the structural guard above
    has a concrete positive case to lean on."""
    src = _full_analysis_row()
    db_row = _analysis_to_db_row(src)

    # Write side: pattern_analysis must be a dict (mode="json"
    # serialization), not a Pydantic model instance.
    assert isinstance(db_row["pattern_analysis"], dict)
    assert db_row["pattern_analysis"]["schema_version"] == 1

    restored = _db_row_to_analysis(db_row)
    assert restored.pattern_analysis is not None
    assert restored.pattern_analysis.schema_version == 1
    assert restored.pattern_analysis.ai_generated_score == 11
    assert len(restored.pattern_analysis.patterns) == 1
    assert restored.pattern_analysis.patterns[0].code == "mca_stacking"


def test_pattern_analysis_null_round_trips_as_none() -> None:
    src = _full_analysis_row()
    src.pattern_analysis = None
    db_row = _analysis_to_db_row(src)
    assert db_row["pattern_analysis"] is None

    restored = _db_row_to_analysis(db_row)
    assert restored.pattern_analysis is None


# ----------------------------------------------------------------------
# Required-column behavior
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "required_key",
    [
        "id",
        "document_id",
        "statement_period_start",
        "statement_period_end",
        "statement_days",
        "beginning_balance",
        "ending_balance",
        "avg_daily_balance",
        "true_revenue",
        "monthly_revenue",
        "lowest_balance",
        "num_nsf",
        "days_negative",
        "mca_positions",
        "mca_daily_total",
        "debt_to_revenue",
        "payroll_detected",
    ],
)
def test_missing_required_column_raises(required_key: str) -> None:
    """Truly-required columns (no default in mapper body) must raise
    rather than silently fall through to a default — same discipline as
    ``_row_to_document`` and ``parse_status``. A schema bug that silently
    fills in a default would mask real data drift between code and DB.
    """
    src = _full_analysis_row()
    db_row = _analysis_to_db_row(src)
    del db_row[required_key]
    with pytest.raises(KeyError):
        _db_row_to_analysis(db_row)
