"""Dual-write tests for ``CostTrackingBedrockClient`` (migration 078).

Existing audit_log writes stay intact; a parallel row lands in
``llm_costs`` when the wrapper is constructed with an
``LLMCostRepository``. The non-Protocol surface (``generate_text``,
``invoke_with_web_search``, ``invoke_tool_json``) also flows through
the dual-write.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from aegis.audit import InMemoryAuditLog
from aegis.ops.cost_tracking import CostTrackingBedrockClient
from aegis.ops.llm_cost_repository import InMemoryLLMCostRepository

_WINDOW_START = datetime(2000, 1, 1, tzinfo=UTC)
_WINDOW_END = datetime(2100, 1, 1, tzinfo=UTC)


# --- minimal fakes (kept slim; the wider behaviour is covered in
# ``tests/ops/test_cost_tracking.py``'s richer fixtures) ----------------


@dataclass
class _FakeUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class _FakeText:
    text: str = '{"x": 1}'
    type: str = "text"


@dataclass
class _FakeToolUse:
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class _FakeResponse:
    usage: _FakeUsage
    content: list[Any] = field(default_factory=list)
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


class _FakeMessages:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.create_calls: int = 0
        self.stream_calls: int = 0

    def stream(self, **_kwargs: Any) -> _FakeStream:
        self.stream_calls += 1
        return _FakeStream(self._response)

    def create(self, **_kwargs: Any) -> _FakeResponse:
        self.create_calls += 1
        return self._response


class _FakeAnthropicClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.messages = _FakeMessages(response)


class _FakeInner:
    def __init__(self, response: _FakeResponse) -> None:
        self._model = "us.anthropic.claude-sonnet-4-6"
        self._client = _FakeAnthropicClient(response)


# --- tests ----------------------------------------------------------------


def test_extract_writes_both_audit_and_llm_costs() -> None:
    audit = InMemoryAuditLog()
    repo = InMemoryLLMCostRepository()
    document_id = uuid4()
    merchant_id = uuid4()
    inner = _FakeInner(_FakeResponse(usage=_FakeUsage(1000, 500), content=[_FakeText()]))

    wrapper = CostTrackingBedrockClient(
        inner=inner,  # type: ignore[arg-type]
        audit=audit,
        cost_repo=repo,
        document_id=document_id,
        merchant_id=merchant_id,
        # No explicit call_type — extract method infers "extraction".
    )

    wrapper.extract_raw_json(b"%PDF-1.4 fake", "prompt")

    # audit_log row landed (existing behaviour).
    assert len(audit.entries) == 1
    assert audit.entries[0]["action"] == "bedrock.usage"
    assert audit.entries[0]["details"]["operation"] == "extract"

    # llm_costs row landed too (new dual-write).
    in_window = repo.list_in_window(start=_WINDOW_START, end=_WINDOW_END)
    assert len(in_window) == 1
    row = in_window[0]
    assert row.merchant_id == merchant_id
    assert row.document_id == document_id
    assert row.input_tokens == 1000
    assert row.output_tokens == 500
    assert row.call_type == "extraction"
    # 1000 in @ $3/M = $0.003; 500 out @ $15/M = $0.0075; total $0.0105.
    assert row.estimated_cost_usd == Decimal("0.010500")


def test_classify_infers_classification_call_type() -> None:
    audit = InMemoryAuditLog()
    repo = InMemoryLLMCostRepository()
    inner = _FakeInner(_FakeResponse(usage=_FakeUsage(200, 80), content=[_FakeText()]))

    wrapper = CostTrackingBedrockClient(
        inner=inner,  # type: ignore[arg-type]
        audit=audit,
        cost_repo=repo,
    )
    wrapper.classify_batch_json("classify-prompt")

    rows = repo.list_in_window(start=_WINDOW_START, end=_WINDOW_END)
    assert len(rows) == 1
    assert rows[0].call_type == "classification"


def test_explicit_call_type_overrides_inference() -> None:
    """Wrapper constructed with ``call_type="web_presence"`` tags every call."""
    audit = InMemoryAuditLog()
    repo = InMemoryLLMCostRepository()
    inner = _FakeInner(_FakeResponse(usage=_FakeUsage(2000, 200), content=[_FakeText()]))

    wrapper = CostTrackingBedrockClient(
        inner=inner,  # type: ignore[arg-type]
        audit=audit,
        cost_repo=repo,
        call_type="web_presence",
    )

    result = wrapper.invoke_with_web_search("search prompt")
    assert result == '{"x": 1}'

    rows = repo.list_in_window(start=_WINDOW_START, end=_WINDOW_END)
    assert len(rows) == 1
    assert rows[0].call_type == "web_presence"
    assert rows[0].input_tokens == 2000
    assert rows[0].output_tokens == 200


def test_generate_text_routes_to_dual_write_with_call_type() -> None:
    audit = InMemoryAuditLog()
    repo = InMemoryLLMCostRepository()
    inner = _FakeInner(_FakeResponse(usage=_FakeUsage(100, 50), content=[_FakeText()]))

    wrapper = CostTrackingBedrockClient(
        inner=inner,  # type: ignore[arg-type]
        audit=audit,
        cost_repo=repo,
        call_type="narrator",
    )

    out = wrapper.generate_text("write a sentence")
    assert out == '{"x": 1}'
    # audit_log + llm_costs both wrote.
    assert any(e["details"]["operation"] == "generate_text" for e in audit.entries)
    rows = repo.list_in_window(start=_WINDOW_START, end=_WINDOW_END)
    assert len(rows) == 1
    assert rows[0].call_type == "narrator"


def test_invoke_tool_json_routes_to_dual_write() -> None:
    audit = InMemoryAuditLog()
    repo = InMemoryLLMCostRepository()
    tool_input = {"text": "hi"}
    inner = _FakeInner(
        _FakeResponse(
            usage=_FakeUsage(300, 150),
            content=[_FakeToolUse(name="my_tool", input=tool_input)],
        )
    )

    wrapper = CostTrackingBedrockClient(
        inner=inner,  # type: ignore[arg-type]
        audit=audit,
        cost_repo=repo,
        call_type="narrator",
    )

    output, model_id = wrapper.invoke_tool_json(
        system_prompt="sys",
        user_prompt="user",
        tool_name="my_tool",
        tool_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        max_tokens=512,
        temperature=0.0,
    )
    assert output == tool_input
    assert model_id == "us.anthropic.claude-sonnet-4-6"
    assert any(e["details"]["operation"] == "invoke_tool_json" for e in audit.entries)
    rows = repo.list_in_window(start=_WINDOW_START, end=_WINDOW_END)
    assert len(rows) == 1
    assert rows[0].call_type == "narrator"


def test_no_call_type_and_no_inference_skips_llm_costs_but_keeps_audit() -> None:
    """``invoke_with_web_search`` without explicit call_type can't infer.

    The audit_log row still lands (it's the canonical record), the
    llm_costs insert is skipped, the call returns normally.
    """
    audit = InMemoryAuditLog()
    repo = InMemoryLLMCostRepository()
    inner = _FakeInner(_FakeResponse(usage=_FakeUsage(50, 25), content=[_FakeText()]))

    wrapper = CostTrackingBedrockClient(
        inner=inner,  # type: ignore[arg-type]
        audit=audit,
        cost_repo=repo,
        # call_type omitted on purpose.
    )

    wrapper.invoke_with_web_search("p")
    assert any(e["details"]["operation"] == "invoke_with_web_search" for e in audit.entries)
    assert repo.list_in_window(start=_WINDOW_START, end=_WINDOW_END) == []


def test_wrapper_without_cost_repo_only_writes_audit() -> None:
    """Backwards-compat: no cost_repo = no llm_costs writes."""
    audit = InMemoryAuditLog()
    inner = _FakeInner(_FakeResponse(usage=_FakeUsage(100, 50), content=[_FakeText()]))

    wrapper = CostTrackingBedrockClient(
        inner=inner,  # type: ignore[arg-type]
        audit=audit,
        # cost_repo omitted — existing behaviour.
    )

    wrapper.extract_raw_json(b"%PDF-1.4 fake", "p")
    assert len(audit.entries) == 1
