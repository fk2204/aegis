"""Tests for the per-call Bedrock cost ledger (migration 078)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from aegis.ops.llm_cost_repository import (
    InMemoryLLMCostRepository,
    MonthlyCost,
    PerDocumentCost,
    PerMerchantCost,
)


def test_insert_returns_row_with_assigned_id() -> None:
    repo = InMemoryLLMCostRepository()
    merchant_id = uuid4()
    document_id = uuid4()

    row = repo.insert(
        merchant_id=merchant_id,
        document_id=document_id,
        model_id="us.anthropic.claude-sonnet-4-6",
        input_tokens=1000,
        output_tokens=500,
        estimated_cost_usd=Decimal("0.010500"),
        call_type="extraction",
    )

    assert row.id is not None
    assert row.merchant_id == merchant_id
    assert row.document_id == document_id
    assert row.input_tokens == 1000
    assert row.output_tokens == 500
    assert row.estimated_cost_usd == Decimal("0.010500")
    assert row.call_type == "extraction"


def test_list_in_window_filters_by_called_at() -> None:
    repo = InMemoryLLMCostRepository()
    inside_ts = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    before_ts = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)
    after_ts = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)

    for ts in (before_ts, inside_ts, after_ts):
        repo.insert(
            merchant_id=None,
            document_id=None,
            model_id="us.anthropic.claude-sonnet-4-6",
            input_tokens=100,
            output_tokens=50,
            estimated_cost_usd=Decimal("0.001"),
            call_type="extraction",
            called_at=ts,
        )

    rows = repo.list_in_window(
        start=datetime(2026, 6, 1, tzinfo=UTC),
        end=datetime(2026, 7, 1, tzinfo=UTC),
    )
    assert len(rows) == 1
    assert rows[0].called_at == inside_ts


def test_per_merchant_aggregates_and_sorts_desc() -> None:
    repo = InMemoryLLMCostRepository()
    merchant_a = uuid4()
    merchant_b = uuid4()
    inside_ts = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)

    # Merchant A: 2 extraction calls, 0.005 + 0.003 = 0.008
    for cost in (Decimal("0.005"), Decimal("0.003")):
        repo.insert(
            merchant_id=merchant_a,
            document_id=uuid4(),
            model_id="us.anthropic.claude-sonnet-4-6",
            input_tokens=500,
            output_tokens=200,
            estimated_cost_usd=cost,
            call_type="extraction",
            called_at=inside_ts,
        )

    # Merchant B: 1 web_presence call, 0.02
    repo.insert(
        merchant_id=merchant_b,
        document_id=None,
        model_id="us.anthropic.claude-sonnet-4-6",
        input_tokens=2000,
        output_tokens=200,
        estimated_cost_usd=Decimal("0.020"),
        call_type="web_presence",
        called_at=inside_ts,
    )

    rows = repo.per_merchant(
        start=datetime(2026, 6, 1, tzinfo=UTC),
        end=datetime(2026, 7, 1, tzinfo=UTC),
    )
    assert len(rows) == 2
    # Merchant B's total (0.020) > Merchant A's (0.008) — sorted desc.
    assert rows[0].merchant_id == merchant_b
    assert rows[0].total_cost_usd == Decimal("0.020000")
    assert rows[0].call_count == 1
    assert rows[0].counts_by_type == {"web_presence": 1}
    assert rows[1].merchant_id == merchant_a
    assert rows[1].total_cost_usd == Decimal("0.008000")
    assert rows[1].call_count == 2
    assert rows[1].counts_by_type == {"extraction": 2}


def test_per_document_groups_by_doc_merchant_model() -> None:
    repo = InMemoryLLMCostRepository()
    document_id = uuid4()
    merchant_id = uuid4()
    inside_ts = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)

    repo.insert(
        merchant_id=merchant_id,
        document_id=document_id,
        model_id="us.anthropic.claude-sonnet-4-6",
        input_tokens=500,
        output_tokens=200,
        estimated_cost_usd=Decimal("0.001"),
        call_type="extraction",
        called_at=inside_ts,
    )
    repo.insert(
        merchant_id=merchant_id,
        document_id=document_id,
        model_id="us.anthropic.claude-sonnet-4-6",
        input_tokens=200,
        output_tokens=100,
        estimated_cost_usd=Decimal("0.002"),
        call_type="classification",
        called_at=inside_ts,
    )

    rows = repo.per_document(
        start=datetime(2026, 6, 1, tzinfo=UTC),
        end=datetime(2026, 7, 1, tzinfo=UTC),
    )
    assert len(rows) == 1
    assert rows[0].document_id == document_id
    assert rows[0].total_cost_usd == Decimal("0.003000")
    assert rows[0].call_count == 2
    assert rows[0].call_types == ("classification", "extraction")


def test_monthly_trend_groups_by_year_month_desc() -> None:
    repo = InMemoryLLMCostRepository()
    may_ts = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    june_ts = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)

    repo.insert(
        merchant_id=None,
        document_id=None,
        model_id="m",
        input_tokens=100,
        output_tokens=50,
        estimated_cost_usd=Decimal("0.005"),
        call_type="extraction",
        called_at=may_ts,
    )
    repo.insert(
        merchant_id=None,
        document_id=None,
        model_id="m",
        input_tokens=200,
        output_tokens=100,
        estimated_cost_usd=Decimal("0.010"),
        call_type="extraction",
        called_at=june_ts,
    )

    trend = repo.monthly_trend(months=6)
    assert len(trend) == 2
    # Newer month first.
    assert trend[0].month_iso == "2026-06"
    assert trend[0].total_cost_usd == Decimal("0.010000")
    assert trend[1].month_iso == "2026-05"
    assert trend[1].total_cost_usd == Decimal("0.005000")


def test_per_merchant_handles_null_merchant_id() -> None:
    """Calls without a merchant context (e.g. ad-hoc scripts) still aggregate."""
    repo = InMemoryLLMCostRepository()
    inside_ts = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    repo.insert(
        merchant_id=None,
        document_id=None,
        model_id="m",
        input_tokens=100,
        output_tokens=50,
        estimated_cost_usd=Decimal("0.005"),
        call_type="extraction",
        called_at=inside_ts,
    )

    rows = repo.per_merchant(
        start=datetime(2026, 6, 1, tzinfo=UTC),
        end=datetime(2026, 7, 1, tzinfo=UTC),
    )
    assert len(rows) == 1
    assert rows[0].merchant_id is None
    assert rows[0].total_cost_usd == Decimal("0.005000")


def test_dataclass_defaults_are_callable_for_per_merchant_cost() -> None:
    """Smoke: dataclass shapes are usable by templates and JSON serializers."""
    row = PerMerchantCost(
        merchant_id=None,
        total_cost_usd=Decimal("0.000001"),
        total_input_tokens=0,
        total_output_tokens=0,
        call_count=0,
    )
    assert row.counts_by_type == {}


def test_per_document_default_call_types_empty_tuple() -> None:
    row = PerDocumentCost(
        document_id=None,
        merchant_id=None,
        model_id="m",
        total_cost_usd=Decimal("0"),
        call_count=0,
    )
    assert row.call_types == ()


def test_monthly_cost_is_a_simple_record() -> None:
    rec = MonthlyCost(month_iso="2026-06", total_cost_usd=Decimal("1.00"), call_count=10)
    assert rec.month_iso == "2026-06"
    assert rec.total_cost_usd == Decimal("1.00")
    assert rec.call_count == 10
