"""InMemoryDocumentRepository unit tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from aegis.parser.metadata import MetadataAnalysis
from aegis.parser.models import (
    Aggregates,
    ClassifiedTransaction,
    ExtractedStatement,
    StatementSummary,
    ValidationResult,
)
from aegis.parser.patterns import PatternAnalysisDTO
from aegis.parser.pipeline import PipelineResult
from aegis.storage import (
    AnalysisRow,
    DocumentExistsError,
    DocumentNotFoundError,
    InMemoryDocumentRepository,
)


def _sourced_money(value: str, ids: list[UUID] | None = None) -> _SourcedMoneyT:
    from aegis.parser.models import _SourcedMoney

    return _SourcedMoney(value=Decimal(value), source_ids=ids or [])


def _sourced_int(value: int, ids: list[UUID] | None = None) -> _SourcedIntT:
    from aegis.parser.models import _SourcedInt

    return _SourcedInt(value=value, source_ids=ids or [])


# Forward declarations for the pydantic types so the helper signatures resolve.
from aegis.parser.models import _SourcedInt as _SourcedIntT  # noqa: E402
from aegis.parser.models import _SourcedMoney as _SourcedMoneyT  # noqa: E402


def _make_pipeline_result() -> PipelineResult:
    tx_id = uuid4()
    summary = StatementSummary(
        beginning_balance=Decimal("1000.00"),
        ending_balance=Decimal("2000.00"),
        deposit_total=Decimal("3000.00"),
        withdrawal_total=Decimal("2000.00"),
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
    )
    classified = [
        ClassifiedTransaction(
            id=tx_id,
            posted_date=date(2026, 1, 5),
            description="DEPOSIT",
            amount=Decimal("3000.00"),
            running_balance=Decimal("4000.00"),
            source_page=1,
            source_line=10,
            category="deposit",
            classification_confidence=95,
        )
    ]
    aggregates = Aggregates(
        avg_daily_balance=_sourced_money("1500.00", [tx_id]),
        true_revenue=_sourced_money("3000.00", [tx_id]),
        num_nsf=_sourced_int(0),
        days_negative=_sourced_int(0),
        debt_to_revenue=Decimal("0.00"),
        mca_daily_total=_sourced_money("0.00"),
    )
    extraction_stub: Any = type(
        "Stub", (), {"statement": ExtractedStatement(summary=summary, transactions=classified)}
    )()
    return PipelineResult(
        parse_status="proceed",
        metadata=MetadataAnalysis(
            pdf_creation_date=None,
            pdf_modification_date=None,
            pdf_producer=None,
            pdf_creator=None,
            pdf_author=None,
            page_count=2,
            file_size_bytes=10240,
            eof_markers=1,
            page_sizes=["LETTER"],
            flags=[],
            fraud_score=0,
        ),
        extraction=extraction_stub,
        validation=ValidationResult(passed=True),
        classified=classified,
        patterns=None,
        aggregates=aggregates,
        fraud_score=10,
        fraud_score_breakdown={"metadata_score": 0, "math_score": 0, "patterns_score": 0},
        all_flags=[],
    )


def test_create_and_get() -> None:
    repo = InMemoryDocumentRepository()
    row = repo.create_document(
        file_hash="a" * 64, byte_size=1234, original_filename="x.pdf"
    )
    assert repo.get_document(row.id) == row
    assert repo.find_by_hash("a" * 64) == row


def test_duplicate_hash_rejected() -> None:
    repo = InMemoryDocumentRepository()
    repo.create_document(file_hash="b" * 64, byte_size=10, original_filename="x.pdf")
    with pytest.raises(DocumentExistsError):
        repo.create_document(file_hash="b" * 64, byte_size=10, original_filename="y.pdf")


def test_get_unknown_id_raises() -> None:
    repo = InMemoryDocumentRepository()
    with pytest.raises(DocumentNotFoundError):
        repo.get_document(uuid4())


def test_persist_parse_result_writes_status_transactions_and_analysis() -> None:
    repo = InMemoryDocumentRepository()
    row = repo.create_document(
        file_hash="c" * 64, byte_size=4096, original_filename="stmt.pdf"
    )
    result = _make_pipeline_result()
    repo.persist_parse_result(row.id, result=result)

    updated = repo.get_document(row.id)
    assert updated.parse_status == "proceed"
    assert updated.fraud_score == 10
    assert updated.parsed_at is not None

    txs = repo.list_transactions(row.id)
    assert len(txs) == 1
    assert txs[0].category == "deposit"

    analysis = repo.get_analysis(row.id)
    assert analysis is not None
    assert analysis.true_revenue == Decimal("3000.00")
    assert analysis.statement_days == 30
    assert analysis.true_revenue_source_ids == [txs[0].id]


def test_list_transactions_filter_by_category() -> None:
    repo = InMemoryDocumentRepository()
    row = repo.create_document(
        file_hash="d" * 64, byte_size=10, original_filename="s.pdf"
    )
    repo.persist_parse_result(row.id, result=_make_pipeline_result())
    assert repo.list_transactions(row.id, category="deposit")
    assert repo.list_transactions(row.id, category="mca_debit") == []


# ---------------------------------------------------------------------------
# pattern_analysis persistence (migration 032 / stage 2 chunk 1)
#
# Round-trips through _analysis_to_db_row + _db_row_to_analysis without
# any pipeline writes — those happen in chunk 2. Chunk 1 only proves
# the plumbing: a constructed PatternAnalysisDTO survives the
# Supabase-side serializer / deserializer pair, and rows where
# pattern_analysis is NULL (every legacy row, every brand-new chunk-1
# row) read back with pattern_analysis=None.
# ---------------------------------------------------------------------------


def _build_dummy_analysis_row(
    *, pattern_analysis_dto: PatternAnalysisDTO | None = None
) -> AnalysisRow:
    """Construct an AnalysisRow with the minimum required fields plus
    the pattern_analysis under test. Field values are arbitrary —
    only the round-trip matters here."""
    return AnalysisRow(
        id=uuid4(),
        document_id=uuid4(),
        merchant_id=None,
        statement_period_start=date(2026, 4, 1),
        statement_period_end=date(2026, 4, 30),
        statement_days=30,
        beginning_balance=Decimal("1000.00"),
        ending_balance=Decimal("2000.00"),
        avg_daily_balance=Decimal("1500.00"),
        true_revenue=Decimal("5000.00"),
        monthly_revenue=Decimal("5000.00"),
        lowest_balance=Decimal("500.00"),
        num_nsf=0,
        days_negative=0,
        mca_positions=0,
        mca_daily_total=Decimal("0.00"),
        debt_to_revenue=Decimal("0.00"),
        payroll_detected=False,
        returned_ach_count=0,
        pattern_analysis=pattern_analysis_dto,
    )


def test_analysis_row_pattern_analysis_defaults_to_none() -> None:
    """A freshly constructed AnalysisRow has pattern_analysis=None.
    Catches accidental Field(default_factory=...) regressions that
    would conjure a populated DTO out of nowhere."""
    row = _build_dummy_analysis_row()
    assert row.pattern_analysis is None


def test_analysis_row_pattern_analysis_round_trips_through_supabase_helpers() -> None:
    """Construct an AnalysisRow with a fully-populated DTO, run it
    through _analysis_to_db_row -> _db_row_to_analysis, assert
    round-trip equality on every PatternAnalysisDTO field including
    Decimal (McaPosition.daily_equivalent) and UUID
    (Pattern.source_ids)."""
    from aegis.parser.patterns import (
        CounterpartySignalsDTO,
        McaPositionDTO,
        PatternAnalysisDTO,
        PatternDTO,
    )
    from aegis.storage import _analysis_to_db_row, _db_row_to_analysis

    src_id_1 = uuid4()
    src_id_2 = uuid4()
    mca_src_id = uuid4()
    cp_src_id = uuid4()

    dto = PatternAnalysisDTO(
        schema_version=1,
        patterns=[
            PatternDTO(
                code="wash_deposit_suspected",
                severity=35,
                detail="2 round-trip deposit/withdrawal pairs within 5 days",
                source_ids=[src_id_1, src_id_2],
            ),
            PatternDTO(
                code="mca_stacking",
                severity=30,
                detail="3 MCA position(s) detected",
                source_ids=[mca_src_id],
            ),
        ],
        mca_positions=[
            McaPositionDTO(
                funder_label="OnDeck",
                daily_equivalent=Decimal("123.45"),
                occurrences=10,
                source_ids=[mca_src_id],
            ),
        ],
        has_kiting=True,
        paydown_suspected=False,
        counterparty_signals=CounterpartySignalsDTO(
            top_counterparty_pct=78,
            top_counterparty_label="payward interactive",
            top_counterparty_source_ids=[cp_src_id],
            top_5_revenue_share_pct=92,
            top_5_revenue_source_ids=[cp_src_id, src_id_1],
        ),
        payroll_present=True,
        acceleration_clause_triggered=False,
        unauthorized_withdrawal_dispute=True,
        ai_generated_score=42,
    )
    src = _build_dummy_analysis_row(pattern_analysis_dto=dto)

    # Round-trip: serialize to db dict, deserialize back.
    db_row = _analysis_to_db_row(src)
    restored = _db_row_to_analysis(db_row)

    assert restored.pattern_analysis is not None
    rt = restored.pattern_analysis

    # Schema version preserved.
    assert rt.schema_version == 1

    # Patterns survive — including UUIDs in source_ids and the detail
    # string verbatim.
    assert len(rt.patterns) == 2
    assert rt.patterns[0].code == "wash_deposit_suspected"
    assert rt.patterns[0].severity == 35
    assert rt.patterns[0].source_ids == [src_id_1, src_id_2]
    assert rt.patterns[1].code == "mca_stacking"
    assert rt.patterns[1].source_ids == [mca_src_id]

    # Decimal survives — exact equality, not float-approximate.
    assert len(rt.mca_positions) == 1
    assert rt.mca_positions[0].funder_label == "OnDeck"
    assert rt.mca_positions[0].daily_equivalent == Decimal("123.45")
    assert rt.mca_positions[0].occurrences == 10

    # Boolean flags survive.
    assert rt.has_kiting is True
    assert rt.paydown_suspected is False
    assert rt.payroll_present is True
    assert rt.acceleration_clause_triggered is False
    assert rt.unauthorized_withdrawal_dispute is True
    assert rt.ai_generated_score == 42

    # CounterpartySignals nested model survives.
    assert rt.counterparty_signals is not None
    assert rt.counterparty_signals.top_counterparty_pct == 78
    assert rt.counterparty_signals.top_counterparty_label == "payward interactive"
    assert rt.counterparty_signals.top_counterparty_source_ids == [cp_src_id]
    assert rt.counterparty_signals.top_5_revenue_share_pct == 92
    assert rt.counterparty_signals.top_5_revenue_source_ids == [cp_src_id, src_id_1]


def test_analysis_row_with_null_pattern_analysis_round_trips() -> None:
    """A row with pattern_analysis=None (the chunk-1 default; every
    legacy row) survives the serialize/deserialize cycle with the
    field still None. Backward-compat guard so chunk 1 deploys to
    rows that haven't been re-parsed yet without crashing."""
    from aegis.storage import _analysis_to_db_row, _db_row_to_analysis

    src = _build_dummy_analysis_row(pattern_analysis_dto=None)

    db_row = _analysis_to_db_row(src)
    assert db_row["pattern_analysis"] is None

    restored = _db_row_to_analysis(db_row)
    assert restored.pattern_analysis is None


def test_analysis_row_handles_missing_pattern_analysis_key() -> None:
    """A row dict from before migration 032 has no 'pattern_analysis'
    key at all (the column didn't exist). The deserializer must
    handle the missing key without KeyError — read it as None."""
    from aegis.storage import _analysis_to_db_row, _db_row_to_analysis

    src = _build_dummy_analysis_row(pattern_analysis_dto=None)
    db_row = _analysis_to_db_row(src)

    # Simulate a pre-migration row by dropping the key entirely.
    del db_row["pattern_analysis"]

    restored = _db_row_to_analysis(db_row)
    assert restored.pattern_analysis is None


def test_pattern_analysis_dto_round_trips_through_runtime_dataclass() -> None:
    """pattern_analysis_to_dto -> pattern_analysis_from_dto is lossless.
    Future surfaces (post-stage-2 dossier cleanup) will use this pair
    to read a stored PatternAnalysisDTO back into the runtime
    PatternAnalysis without re-running analyze_patterns()."""
    from aegis.parser.patterns import (
        CounterpartySignals,
        McaPosition,
        Pattern,
        PatternAnalysis,
        pattern_analysis_from_dto,
        pattern_analysis_to_dto,
    )

    src_id = uuid4()
    pa = PatternAnalysis(
        patterns=[
            Pattern(
                code="wash_deposit_suspected",
                severity=35,
                detail="2 pairs in 5 days",
                source_ids=[src_id],
            ),
        ],
        mca_positions=[
            McaPosition(
                funder_label="OnDeck",
                daily_equivalent=Decimal("89.10"),
                occurrences=7,
                source_ids=[src_id],
            ),
        ],
        has_kiting=False,
        paydown_suspected=True,
        counterparty_signals=CounterpartySignals(
            top_counterparty_pct=55,
            top_counterparty_label="some payee",
            top_counterparty_source_ids=[src_id],
        ),
        payroll_present=False,
        acceleration_clause_triggered=True,
        unauthorized_withdrawal_dispute=False,
        ai_generated_score=18,
    )

    dto = pattern_analysis_to_dto(pa)
    rt = pattern_analysis_from_dto(dto)

    assert rt.patterns[0].code == "wash_deposit_suspected"
    assert rt.patterns[0].source_ids == [src_id]
    assert rt.mca_positions[0].daily_equivalent == Decimal("89.10")
    assert rt.has_kiting is False
    assert rt.paydown_suspected is True
    assert rt.counterparty_signals.top_counterparty_pct == 55
    assert rt.counterparty_signals.top_counterparty_label == "some payee"
    assert rt.acceleration_clause_triggered is True
    assert rt.ai_generated_score == 18
    # Derived properties keep working.
    assert rt.fraud_score == 35
    assert rt.flags == ["wash_deposit_suspected"]


def test_pattern_analysis_dto_default_schema_version_is_one() -> None:
    """Forward-compat sentinel — every newly-constructed DTO records
    its schema version explicitly. If a future v2 lands, this default
    needs to bump in lockstep with the read-branch."""
    from aegis.parser.patterns import PatternAnalysisDTO

    dto = PatternAnalysisDTO()
    assert dto.schema_version == 1
