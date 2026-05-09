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
from aegis.parser.pipeline import PipelineResult
from aegis.storage import (
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
