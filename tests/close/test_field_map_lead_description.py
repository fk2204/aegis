"""Tests for ``extract_lead_description`` (Feature D / migration 064).

Loads the captured-and-sanitized Lead fixture and verifies the helper
pulls the ``description`` field robustly across the realistic surface:

  * verbatim string read with surrounding whitespace trimmed,
  * missing key → None,
  * explicit JSON null → None,
  * empty / whitespace-only string → None,
  * non-string value (defensive) → None.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aegis.close.field_map import extract_lead_description

_FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict[str, Any]:
    parsed = json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise AssertionError(f"fixture {name} is not a JSON object")
    return parsed


def test_extracts_description_from_real_lead_fixture() -> None:
    payload = _load_fixture("lead_with_description.json")
    out = extract_lead_description(payload)
    assert out is not None
    assert out.startswith("Inbound from broker")
    # No trailing whitespace from the fixture round-trip.
    assert out == out.strip()


def test_missing_key_returns_none() -> None:
    out = extract_lead_description({"id": "lead_x", "display_name": "Foo"})
    assert out is None


def test_explicit_null_returns_none() -> None:
    out = extract_lead_description({"id": "lead_x", "description": None})
    assert out is None


def test_empty_string_returns_none() -> None:
    out = extract_lead_description({"id": "lead_x", "description": ""})
    assert out is None


def test_whitespace_only_returns_none() -> None:
    out = extract_lead_description({"id": "lead_x", "description": "   \n\t  "})
    assert out is None


def test_non_string_value_returns_none() -> None:
    """Defensive — Close's contract says string, but a list / dict surprise
    must collapse to None rather than raise. The orchestrator is
    best-effort and a single weird field can't block the refresh."""
    out = extract_lead_description({"id": "lead_x", "description": ["a", "b"]})
    assert out is None


def test_trims_surrounding_whitespace_but_preserves_internal_newlines() -> None:
    payload = {"id": "lead_x", "description": "  \n line one\nline two  \n"}
    out = extract_lead_description(payload)
    assert out == "line one\nline two"
