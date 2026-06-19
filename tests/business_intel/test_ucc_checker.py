"""Tests for ``aegis.business_intel.ucc_checker.check_ucc_and_defaults``.

Same three concerns as the web-presence scanner:

1. Happy path — valid JSON response parses into a UCCResult with the
   right shape.
2. Graceful failure — Bedrock raise, malformed JSON, bad shapes all
   collapse to an empty result.
3. Empty input — empty business name short-circuits without any
   client call.
"""

from __future__ import annotations

from typing import Any

import pytest

from aegis.business_intel.ucc_checker import UCCResult, check_ucc_and_defaults


class _StubClient:
    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.calls = 0
        self.last_prompt: str | None = None

    def invoke_with_web_search(self, prompt: str) -> str:
        self.calls += 1
        self.last_prompt = prompt
        return self.raw


class _RaisingClient:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    def invoke_with_web_search(self, prompt: str) -> str:
        self.calls += 1
        raise self.exc


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_response_parses_into_result() -> None:
    raw = (
        '{"ucc_filings": ["OnDeck Capital", "Fora Financial"], '
        '"default_indicators": ["lawsuit_2024_civil_judgment"], '
        '"source_summary": "Two UCC filings on SoS; one civil judgment on PACER."}'
    )
    client = _StubClient(raw)
    result = check_ucc_and_defaults("Acme Inc.", "MA", "Jane Doe", client=client)
    assert isinstance(result, UCCResult)
    assert result.ucc_filings == ("OnDeck Capital", "Fora Financial")
    assert result.default_indicators == ("lawsuit_2024_civil_judgment",)
    assert "Two UCC filings" in result.source_summary
    assert result.checked_at is not None


def test_empty_lists_are_acceptable() -> None:
    raw = (
        '{"ucc_filings": [], "default_indicators": [], '
        '"source_summary": "No public UCC filings or default indicators located."}'
    )
    result = check_ucc_and_defaults("Clean Co.", "CA", client=_StubClient(raw))
    assert result.ucc_filings == ()
    assert result.default_indicators == ()
    assert result.checked_at is not None


def test_code_fence_wrapped_response_still_parses() -> None:
    raw = '```json\n{"ucc_filings": ["X"], "default_indicators": [], "source_summary": "ok"}\n```'
    result = check_ucc_and_defaults("Test Co", client=_StubClient(raw))
    assert result.ucc_filings == ("X",)


def test_filings_are_deduplicated() -> None:
    raw = (
        '{"ucc_filings": ["OnDeck", "OnDeck", "  ", ""], '
        '"default_indicators": [], "source_summary": "s"}'
    )
    result = check_ucc_and_defaults("Test Co", client=_StubClient(raw))
    assert result.ucc_filings == ("OnDeck",)


def test_lists_capped_at_fifteen() -> None:
    filings = ", ".join(f'"f{i}"' for i in range(30))
    raw = f'{{"ucc_filings": [{filings}], "default_indicators": [], "source_summary": "s"}}'
    result = check_ucc_and_defaults("Test Co", client=_StubClient(raw))
    assert len(result.ucc_filings) == 15


def test_prompt_includes_business_state_and_owner() -> None:
    client = _StubClient('{"ucc_filings":[],"default_indicators":[],"source_summary":""}')
    check_ucc_and_defaults("Joe's Diner", "NY", "John Smith", client=client)
    assert client.last_prompt is not None
    assert "Joe's Diner" in client.last_prompt
    assert "NY" in client.last_prompt
    assert "John Smith" in client.last_prompt


# ---------------------------------------------------------------------------
# Graceful failure
# ---------------------------------------------------------------------------


def test_empty_business_name_short_circuits() -> None:
    client = _StubClient('{"ucc_filings":[],"default_indicators":[],"source_summary":""}')
    result = check_ucc_and_defaults("", client=client)
    assert result == UCCResult()
    assert client.calls == 0


def test_bedrock_raises_returns_empty() -> None:
    client = _RaisingClient(RuntimeError("bedrock_unavailable"))
    result = check_ucc_and_defaults("Test Co", client=client)
    assert result == UCCResult()


def test_malformed_json_returns_empty() -> None:
    result = check_ucc_and_defaults("Test Co", client=_StubClient("not json"))
    assert result == UCCResult()


def test_non_object_json_returns_empty() -> None:
    result = check_ucc_and_defaults("Test Co", client=_StubClient('["a"]'))
    assert result == UCCResult()


def test_wrong_typed_filings_returns_empty() -> None:
    raw = '{"ucc_filings": "not a list", "default_indicators": [], "source_summary": ""}'
    result = check_ucc_and_defaults("Test Co", client=_StubClient(raw))
    assert result == UCCResult()


def test_non_string_filing_returns_empty() -> None:
    raw = '{"ucc_filings": ["ok", 42], "default_indicators": [], "source_summary": ""}'
    result = check_ucc_and_defaults("Test Co", client=_StubClient(raw))
    assert result == UCCResult()


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_unusual_inputs_short_circuit(bad: Any) -> None:
    client = _StubClient('{"ucc_filings":[],"default_indicators":[],"source_summary":""}')
    result = check_ucc_and_defaults(bad, client=client)
    assert result == UCCResult()
