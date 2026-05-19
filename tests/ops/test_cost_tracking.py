"""Tests for Bedrock per-deal cost tracking (mp Phase 11 task #2).

Two surfaces under test:

* ``compute_cost_usd`` + the wrapper client — verify the per-call
  accounting produces the right Decimal cost and writes the right
  audit row.
* ``build_weekly_digest`` — verify roll-up math, per-deal grouping,
  funded-deal averaging, and cost-as-percent-of-revenue.

The wrapper tests use a fake inner BedrockClient that exposes the
same ``_client``/``_model`` attributes the wrapper reaches into; the
fake's ``messages.stream`` returns a context manager yielding a
``response`` with a usage attribute.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from aegis.audit import InMemoryAuditLog
from aegis.ops.cost_tracking import (
    CostTrackingBedrockClient,
    build_weekly_digest,
    compute_cost_usd,
)

# --- pricing helpers --------------------------------------------------------


def test_compute_cost_usd_default_prices() -> None:
    """1M input + 1M output @ default prices = $3 + $15 = $18."""
    cost = compute_cost_usd(input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost == Decimal("18.000000")


def test_compute_cost_usd_handles_small_numbers() -> None:
    """1 token at $3/MTok = $0.000003 (six decimal places kept)."""
    cost = compute_cost_usd(input_tokens=1, output_tokens=0)
    assert cost == Decimal("0.000003")


def test_compute_cost_usd_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEGIS_BEDROCK_INPUT_USD_PER_MTOK", "5.00")
    monkeypatch.setenv("AEGIS_BEDROCK_OUTPUT_USD_PER_MTOK", "25.00")
    # New prices: 1M in + 1M out = $5 + $25 = $30
    assert compute_cost_usd(
        input_tokens=1_000_000, output_tokens=1_000_000
    ) == Decimal("30.000000")


# --- wrapper client --------------------------------------------------------


@dataclass
class _FakeUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class _FakeResponse:
    usage: _FakeUsage
    content: list[Any]
    stop_reason: str = "end_turn"


class _FakeStream:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def get_final_message(self) -> _FakeResponse:
        return self._response


class _FakeText:
    type = "text"
    text = '{"summary": {}, "transactions": []}'


class _FakeMessages:
    def __init__(self, usage: _FakeUsage) -> None:
        self._usage = usage
        self.last_messages: list[Any] = []

    def stream(self, **kwargs: Any) -> _FakeStream:
        self.last_messages = kwargs.get("messages", [])
        return _FakeStream(
            _FakeResponse(usage=self._usage, content=[_FakeText()])
        )

    def create(self, **kwargs: Any) -> _FakeResponse:
        self.last_messages = kwargs.get("messages", [])
        return _FakeResponse(usage=self._usage, content=[_FakeText()])


class _FakeAnthropicClient:
    def __init__(self, usage: _FakeUsage) -> None:
        self.messages = _FakeMessages(usage)


class _FakeInner:
    """Stand-in for a configured BedrockClient.

    Only exposes the two attributes the wrapper touches.
    """

    def __init__(self, *, input_tokens: int, output_tokens: int) -> None:
        self._model = "us.anthropic.claude-sonnet-4-6"
        self._client = _FakeAnthropicClient(
            _FakeUsage(input_tokens=input_tokens, output_tokens=output_tokens)
        )


def test_wrapper_writes_bedrock_usage_audit_row() -> None:
    audit = InMemoryAuditLog()
    inner = _FakeInner(input_tokens=1500, output_tokens=400)
    wrapper = CostTrackingBedrockClient(inner=inner, audit=audit)  # type: ignore[arg-type]

    parsed, truncated = wrapper.extract_raw_json(b"%PDF-1.4 fake", "prompt")
    assert parsed == {"summary": {}, "transactions": []}
    assert truncated is False
    assert len(audit.entries) == 1
    row = audit.entries[0]
    assert row["action"] == "bedrock.usage"
    details = row["details"]
    assert details["operation"] == "extract"
    assert details["input_tokens"] == 1500
    assert details["output_tokens"] == 400
    # 1500 in @ $3/MTok = $0.0045; 400 out @ $15/MTok = $0.006
    assert details["total_cost_usd"] == "0.010500"
    assert details["model_id"] == "us.anthropic.claude-sonnet-4-6"


def test_wrapper_vision_path_audits() -> None:
    audit = InMemoryAuditLog()
    inner = _FakeInner(input_tokens=5000, output_tokens=200)
    wrapper = CostTrackingBedrockClient(inner=inner, audit=audit)  # type: ignore[arg-type]

    wrapper.extract_raw_json_from_images([b"\x89PNG"], "vision-prompt")
    assert audit.entries[-1]["details"]["operation"] == "extract_vision"
    assert audit.entries[-1]["details"]["input_tokens"] == 5000


def test_wrapper_classify_audits() -> None:
    audit = InMemoryAuditLog()
    inner = _FakeInner(input_tokens=200, output_tokens=80)
    wrapper = CostTrackingBedrockClient(inner=inner, audit=audit)  # type: ignore[arg-type]

    wrapper.classify_batch_json("classify-prompt")
    assert audit.entries[-1]["details"]["operation"] == "classify"


def test_wrapper_handles_missing_usage_gracefully() -> None:
    """If the response object lacks .usage, we don't crash + skip the row."""
    audit = InMemoryAuditLog()

    class _NoUsageMessages:
        def stream(self, **_kw: Any) -> _FakeStream:
            return _FakeStream(
                _FakeResponse(
                    usage=None,  # type: ignore[arg-type]
                    content=[_FakeText()],
                )
            )

    class _NoUsageInner:
        _model = "us.anthropic.claude-sonnet-4-6"

        def __init__(self) -> None:
            self._client = type("Client", (), {"messages": _NoUsageMessages()})()

    wrapper = CostTrackingBedrockClient(
        inner=_NoUsageInner(),  # type: ignore[arg-type]
        audit=audit,
    )
    wrapper.extract_raw_json(b"%PDF-1.4 fake", "p")
    # No row written when usage is missing, but no exception either.
    assert audit.entries == []


# --- weekly digest ---------------------------------------------------------


def _usage_row(
    *,
    doc_id: UUID | None,
    input_tokens: int,
    output_tokens: int,
) -> dict[str, Any]:
    cost = compute_cost_usd(
        input_tokens=input_tokens, output_tokens=output_tokens
    )
    return {
        "actor": "bedrock.client",
        "action": "bedrock.usage",
        "subject_type": "document" if doc_id else None,
        "subject_id": str(doc_id) if doc_id else None,
        "details": {
            "operation": "extract",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_cost_usd": str(cost),
        },
    }


def test_weekly_digest_aggregates_totals() -> None:
    d1 = uuid4()
    d2 = uuid4()
    rows = [
        _usage_row(doc_id=d1, input_tokens=10_000, output_tokens=1_000),
        _usage_row(doc_id=d1, input_tokens=5_000, output_tokens=300),
        _usage_row(doc_id=d2, input_tokens=20_000, output_tokens=2_000),
    ]
    digest = build_weekly_digest(
        rows, window_start="2026-05-12T00:00:00Z", window_end="2026-05-19T00:00:00Z"
    )

    assert digest.total_calls == 3
    assert digest.total_input_tokens == 35_000
    assert digest.total_output_tokens == 3_300
    # 35k in @ $3/MTok = $0.105; 3.3k out @ $15/MTok = $0.0495 -> $0.1545
    assert digest.total_cost_usd == Decimal("0.154500")

    # Two unique docs → two PerDealCost rows
    assert len(digest.deals) == 2
    d1_row = next(d for d in digest.deals if d.document_id == d1)
    assert d1_row.call_count == 2
    assert d1_row.input_tokens == 15_000


def test_weekly_digest_funded_avg(monkeypatch: pytest.MonkeyPatch) -> None:
    d_funded = uuid4()
    d_not_funded = uuid4()
    rows = [
        _usage_row(doc_id=d_funded, input_tokens=10_000, output_tokens=1_000),
        _usage_row(doc_id=d_not_funded, input_tokens=20_000, output_tokens=2_000),
    ]
    digest = build_weekly_digest(
        rows,
        window_start="2026-05-12T00:00:00Z",
        window_end="2026-05-19T00:00:00Z",
        funded_document_ids={d_funded},
    )
    funded_avg = digest.avg_cost_per_funded_deal
    assert funded_avg is not None
    # 10k in @ $3 + 1k out @ $15 = 0.030 + 0.015 = 0.045
    assert funded_avg == Decimal("0.0450")


def test_weekly_digest_cost_pct_revenue() -> None:
    d = uuid4()
    rows = [_usage_row(doc_id=d, input_tokens=10_000, output_tokens=1_000)]
    digest = build_weekly_digest(
        rows,
        window_start="2026-05-12T00:00:00Z",
        window_end="2026-05-19T00:00:00Z",
        funded_document_ids={d},
        revenue_by_document={d: Decimal("50000.00")},
    )
    # cost = 0.045 USD; revenue = 50_000; pct = 0.045 / 50000 * 100
    # = 0.00009%
    pct = digest.cost_pct_of_revenue
    assert pct is not None
    assert pct == Decimal("0.0001")  # quantized to 4 dp


def test_weekly_digest_handles_no_funded_deals() -> None:
    rows = [_usage_row(doc_id=uuid4(), input_tokens=100, output_tokens=10)]
    digest = build_weekly_digest(
        rows,
        window_start="x",
        window_end="y",
        funded_document_ids=set(),
    )
    assert digest.avg_cost_per_funded_deal is None
    assert digest.cost_pct_of_revenue is None


def test_weekly_digest_handles_orphan_calls() -> None:
    """Rows without subject_id (e.g. OFAC refresh) hit totals but
    don't fall into per-deal averages."""
    d = uuid4()
    rows = [
        _usage_row(doc_id=None, input_tokens=1_000, output_tokens=10),
        _usage_row(doc_id=d, input_tokens=10_000, output_tokens=1_000),
    ]
    digest = build_weekly_digest(
        rows, window_start="x", window_end="y"
    )
    assert digest.total_calls == 2
    # avg_cost_per_deal counts only the deal with a document_id
    assert digest.avg_cost_per_deal == Decimal("0.0450")
