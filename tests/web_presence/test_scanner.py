"""Tests for ``aegis.web_presence.scanner.scan_web_presence``.

Three concerns:

1. Happy path — valid JSON response parses into a WebPresenceResult
   with the right shape.
2. Graceful failure — Bedrock raise, malformed JSON, bad shapes all
   collapse to an empty result (no exception escapes).
3. Empty input — empty business name short-circuits without any
   client call.
"""

from __future__ import annotations

from typing import Any

import pytest

from aegis.web_presence.scanner import WebPresenceResult, scan_web_presence


class _StubClient:
    """Returns whatever ``raw`` is fixed at construction time."""

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
        '{"summary": "Local restaurant with positive Yelp reviews. '
        'No BBB complaints found.", "risk_flags": []}'
    )
    client = _StubClient(raw)
    result = scan_web_presence("Joe's Diner", "Boston", "MA", client=client)
    assert isinstance(result, WebPresenceResult)
    assert "positive Yelp reviews" in result.summary
    assert result.risk_flags == ()
    assert result.scanned_at is not None
    assert client.calls == 1


def test_summary_and_flags_round_trip() -> None:
    raw = (
        '{"summary": "Strip mall storefront in Boston. '
        'BBB has one unresolved complaint from 2024.", '
        '"risk_flags": ["bbb_unresolved_complaints", "negative_review_pattern"]}'
    )
    client = _StubClient(raw)
    result = scan_web_presence("Test Co", "Boston", "MA", client=client)
    assert result.risk_flags == ("bbb_unresolved_complaints", "negative_review_pattern")


def test_code_fence_wrapped_response_still_parses() -> None:
    """Claude sometimes ignores the prompt and wraps JSON in a fence."""
    raw = '```json\n{"summary": "Plain.", "risk_flags": []}\n```'
    result = scan_web_presence("Test Co", client=_StubClient(raw))
    assert result.summary == "Plain."
    assert result.risk_flags == ()


def test_flags_are_lowercased_and_deduped() -> None:
    raw = '{"summary": "x", "risk_flags": ["Active_Lawsuits", "active_lawsuits", "", "  "]}'
    result = scan_web_presence("Test Co", client=_StubClient(raw))
    assert result.risk_flags == ("active_lawsuits",)


def test_summary_is_capped() -> None:
    long_summary = "X" * 5000
    raw = f'{{"summary": "{long_summary}", "risk_flags": []}}'
    result = scan_web_presence("Test Co", client=_StubClient(raw))
    assert len(result.summary) == 800


def test_flag_count_is_capped() -> None:
    flags = ", ".join(f'"flag_{i}"' for i in range(50))
    raw = f'{{"summary": "x", "risk_flags": [{flags}]}}'
    result = scan_web_presence("Test Co", client=_StubClient(raw))
    assert len(result.risk_flags) == 20


def test_prompt_includes_business_name_and_location() -> None:
    client = _StubClient('{"summary":"x","risk_flags":[]}')
    scan_web_presence("Acme Inc.", "Chicago", "IL", client=client)
    assert client.last_prompt is not None
    assert "Acme Inc." in client.last_prompt
    assert "Chicago, IL" in client.last_prompt


def test_prompt_handles_missing_location() -> None:
    client = _StubClient('{"summary":"x","risk_flags":[]}')
    scan_web_presence("Acme Inc.", client=client)
    assert client.last_prompt is not None
    assert "(not specified)" in client.last_prompt


# ---------------------------------------------------------------------------
# Graceful failure
# ---------------------------------------------------------------------------


def test_empty_business_name_short_circuits() -> None:
    client = _StubClient('{"summary":"x","risk_flags":[]}')
    result = scan_web_presence("", client=client)
    assert result == WebPresenceResult()
    assert client.calls == 0


def test_whitespace_only_business_name_short_circuits() -> None:
    client = _StubClient('{"summary":"x","risk_flags":[]}')
    result = scan_web_presence("   ", client=client)
    assert result == WebPresenceResult()
    assert client.calls == 0


def test_bedrock_raises_runtime_error_returns_empty_result() -> None:
    """Any exception out of the client collapses to empty — never raises."""
    client = _RaisingClient(RuntimeError("bedrock_unavailable"))
    result = scan_web_presence("Test Co", client=client)
    assert result == WebPresenceResult()
    assert client.calls == 1


def test_bedrock_raises_value_error_returns_empty() -> None:
    client = _RaisingClient(ValueError("tool not supported"))
    result = scan_web_presence("Test Co", client=client)
    assert result == WebPresenceResult()


def test_malformed_json_returns_empty() -> None:
    client = _StubClient("definitely not json")
    result = scan_web_presence("Test Co", client=client)
    assert result == WebPresenceResult()


def test_non_object_json_returns_empty() -> None:
    """Claude returns a JSON array — not the expected object shape."""
    client = _StubClient('["a", "b"]')
    result = scan_web_presence("Test Co", client=client)
    assert result == WebPresenceResult()


def test_missing_summary_field_returns_empty() -> None:
    client = _StubClient('{"risk_flags": []}')
    result = scan_web_presence("Test Co", client=client)
    # ``summary`` missing → default empty string → still valid; result has
    # scanned_at and an empty summary. That's "scan ran, returned nothing".
    assert result.summary == ""
    assert result.risk_flags == ()
    assert result.scanned_at is not None


def test_wrong_typed_summary_returns_empty() -> None:
    client = _StubClient('{"summary": 42, "risk_flags": []}')
    result = scan_web_presence("Test Co", client=client)
    assert result == WebPresenceResult()


def test_flag_list_contains_non_strings_returns_empty() -> None:
    client = _StubClient('{"summary": "x", "risk_flags": ["ok", 42]}')
    result = scan_web_presence("Test Co", client=client)
    assert result == WebPresenceResult()


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_scan_handles_unusual_inputs(bad: Any) -> None:
    result = scan_web_presence(bad, client=_StubClient('{"summary":"x","risk_flags":[]}'))
    assert result == WebPresenceResult()
