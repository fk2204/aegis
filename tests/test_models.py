"""Tests for parser/scoring Pydantic models — strict validation, source attribution."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aegis.parser.models import (
    Aggregates,
    ClassifiedTransaction,
    ExtractedStatement,
    StatementSummary,
    Transaction,
)
from aegis.scoring.models import ScoreInput, ScoreResult


def _make_txn(**overrides: object) -> Transaction:
    base = {
        "posted_date": date(2026, 1, 15),
        "description": "DEPOSIT - ACH CREDIT",
        "amount": Decimal("250.00"),
        "running_balance": Decimal("1250.00"),
        "source_page": 1,
        "source_line": 12,
    }
    base.update(overrides)
    return Transaction(**base)


class TestTransaction:
    def test_minimal_valid(self) -> None:
        txn = _make_txn()
        assert txn.amount == Decimal("250.00")
        assert txn.source_page == 1
        assert txn.source_line == 12

    def test_source_page_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            _make_txn(source_page=0)

    def test_source_line_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            _make_txn(source_line=0)

    def test_description_cannot_be_empty(self) -> None:
        with pytest.raises(ValidationError):
            _make_txn(description="")

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            _make_txn(unexpected_field="oops")

    def test_running_balance_optional(self) -> None:
        txn = _make_txn(running_balance=None)
        assert txn.running_balance is None

    def test_id_is_unique_per_instance(self) -> None:
        a = _make_txn()
        b = _make_txn()
        assert a.id != b.id


class TestClassifiedTransaction:
    def test_valid_category(self) -> None:
        ct = ClassifiedTransaction(
            posted_date=date(2026, 1, 15),
            description="MCA DAILY ACH",
            amount=Decimal("130.00"),
            source_page=2,
            source_line=4,
            category="mca_debit",
            classification_confidence=92,
        )
        assert ct.category == "mca_debit"

    def test_confidence_bounds(self) -> None:
        with pytest.raises(ValidationError):
            ClassifiedTransaction(
                posted_date=date(2026, 1, 15),
                description="x",
                amount=Decimal("1.00"),
                source_page=1,
                source_line=1,
                category="other",
                classification_confidence=101,
            )

    def test_invalid_category_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ClassifiedTransaction(
                posted_date=date(2026, 1, 15),
                description="x",
                amount=Decimal("1.00"),
                source_page=1,
                source_line=1,
                category="not_a_category",
                classification_confidence=50,
            )


class TestStatementSummary:
    def test_period_dates_required(self) -> None:
        s = StatementSummary(
            beginning_balance=Decimal("1000.00"),
            ending_balance=Decimal("1500.00"),
            deposit_total=Decimal("3000.00"),
            withdrawal_total=Decimal("2500.00"),
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
        )
        assert s.printed_transaction_count is None


class TestExtractedStatement:
    def test_holds_summary_and_transactions(self) -> None:
        es = ExtractedStatement(
            summary=StatementSummary(
                beginning_balance=Decimal("100.00"),
                ending_balance=Decimal("200.00"),
                deposit_total=Decimal("250.00"),
                withdrawal_total=Decimal("150.00"),
                period_start=date(2026, 1, 1),
                period_end=date(2026, 1, 31),
            ),
            transactions=[_make_txn()],
        )
        assert len(es.transactions) == 1


class TestAggregates:
    def test_sourced_metrics_carry_ids(self) -> None:
        ids = [uuid4(), uuid4()]
        agg = Aggregates(
            avg_daily_balance={"value": Decimal("1234.56"), "source_ids": ids},
            true_revenue={"value": Decimal("50000.00"), "source_ids": ids},
            num_nsf={"value": 2, "source_ids": ids},
            days_negative={"value": 0, "source_ids": []},
            debt_to_revenue=Decimal("0.30"),
            mca_daily_total={"value": Decimal("260.00"), "source_ids": ids},
        )
        assert agg.avg_daily_balance.source_ids == ids
        assert agg.num_nsf.value == 2


class TestScoreInput:
    def test_state_must_be_two_letters(self) -> None:
        with pytest.raises(ValidationError):
            ScoreInput(
                merchant_id=uuid4(),
                business_name="Acme Co",
                owner_name="Jane Doe",
                state="California",  # too long
                avg_daily_balance=Decimal("1000.00"),
                true_revenue=Decimal("50000.00"),
                num_nsf=0,
                days_negative=0,
                mca_positions=0,
                mca_daily_total=Decimal("0.00"),
                statement_period_start=date(2026, 1, 1),
                statement_period_end=date(2026, 1, 31),
                statement_days=30,
                requested_amount=Decimal("10000.00"),
                requested_factor=Decimal("1.30"),
                requested_term_days=100,
            )

    def test_credit_score_range(self) -> None:
        with pytest.raises(ValidationError):
            ScoreInput(
                merchant_id=uuid4(),
                business_name="Acme Co",
                owner_name="Jane Doe",
                state="CA",
                credit_score=900,  # > 850
                avg_daily_balance=Decimal("1000.00"),
                true_revenue=Decimal("50000.00"),
                num_nsf=0,
                days_negative=0,
                mca_positions=0,
                mca_daily_total=Decimal("0.00"),
                statement_period_start=date(2026, 1, 1),
                statement_period_end=date(2026, 1, 31),
                statement_days=30,
                requested_amount=Decimal("10000.00"),
                requested_factor=Decimal("1.30"),
                requested_term_days=100,
            )


class TestScoreResult:
    def test_recommendation_literal(self) -> None:
        with pytest.raises(ValidationError):
            ScoreResult(
                score=80,
                recommendation="maybe",
            )

    def test_score_bounds(self) -> None:
        with pytest.raises(ValidationError):
            ScoreResult(score=101, recommendation="approve")
