"""Tests for `aegis.llm._first_json_object`.

The extractor must stop at the end of the FIRST balanced JSON object so that
trailing commentary or a second JSON object does NOT crash the funder import
flow (this is the Shor Capital ISO PDF regression that surfaced 2026-06-05).
"""

from __future__ import annotations

import pytest

from aegis.llm import _first_json_object


def test_first_json_object_extracts_object_when_trailing_text() -> None:
    """Object followed by prose must parse cleanly (the Shor PDF regression)."""
    payload = (
        '{"name": "Shor Capital", "factor_min": 1.25}'
        "\n\nNote: extra commentary the model decided to append."
    )

    result = _first_json_object(payload)

    assert result == {"name": "Shor Capital", "factor_min": 1.25}


def test_first_json_object_extracts_object_when_trailing_second_object() -> None:
    """If Bedrock emits two JSON objects, return ONLY the first one."""
    payload = (
        '{"name": "Shor Capital", "factor_min": 1.25}'
        "\n\n"
        '{"name": "Different Funder", "factor_min": 1.40}'
    )

    result = _first_json_object(payload)

    assert result == {"name": "Shor Capital", "factor_min": 1.25}


def test_first_json_object_extracts_object_with_leading_prose() -> None:
    """Leading commentary before the object must be tolerated."""
    payload = (
        "Here is the extracted JSON for the funder guidelines:\n\n"
        '{"name": "Shor Capital", "factor_min": 1.25}'
    )

    result = _first_json_object(payload)

    assert result == {"name": "Shor Capital", "factor_min": 1.25}


def test_first_json_object_handles_nested_braces() -> None:
    """Nested objects must not be truncated by the raw_decode bookkeeping."""
    payload = '{"outer": {"inner": {"deep": 1}}, "trailing": [1, 2]}\nignored'

    result = _first_json_object(payload)

    assert result == {"outer": {"inner": {"deep": 1}}, "trailing": [1, 2]}


def test_first_json_object_raises_on_no_object() -> None:
    """No `{` at all -> a useful error."""
    with pytest.raises(ValueError, match="no JSON object in LLM response"):
        _first_json_object("just some text without any json")


def test_first_json_object_raises_on_top_level_array() -> None:
    """Top-level array -> point operators at the prompt to add an object wrapper."""
    with pytest.raises(ValueError, match="top-level JSON array"):
        _first_json_object("[1, 2, 3]")


def test_first_json_object_raises_on_unparseable_object() -> None:
    """Malformed JSON inside the candidate region surfaces a clear error."""
    with pytest.raises(ValueError, match="could not parse JSON"):
        _first_json_object('{"name": "broken", missing-value}')


def test_first_json_object_raises_when_top_level_is_not_object() -> None:
    """If raw_decode returns a non-object (e.g. number), reject it loudly."""
    # `start` finds the `{` inside the array element, but raw_decode lands on `1`.
    # We need a scenario where the first `{` succeeds in parsing but yields a
    # non-dict. With raw_decode that's only possible when no `{` exists; that
    # case is the array branch above. Guard for completeness.
    with pytest.raises(ValueError, match="no JSON object"):
        _first_json_object("12345")
