"""Feature D — ``CloseClient.list_recent_notes`` /
``list_recent_calls`` tests against captured + sanitized fixtures.

Per CLAUDE.md external-integration discipline: fixtures mirror the
real wire shape of ``GET /api/v1/activity/note/?lead_id=...&_limit=N``
and ``GET /api/v1/activity/call/?lead_id=...&_limit=N`` from the Close
API. PII (named individuals, real lead/org IDs, broker / funder names)
has been redacted in-fixture.

Fixtures:
  * ``activity_note_list.json`` — three notes with ``note`` text bodies
    and empty ``attachments[]``. Mirrors the structural keys of the
    pre-existing ``acti_note_with_pdf.json`` fixture (which exercises
    the attachment path) but with text-only notes — the path Feature
    D consumes.
  * ``activity_call_list.json`` — two calls each carrying a ``note``
    field (the post-call operator disposition summary), plus the
    ``duration`` / ``direction`` / ``disposition`` fields the wire
    shape includes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from aegis.close.client import (
    CloseCall,
    CloseClient,
    CloseError,
    CloseNote,
)
from aegis.config import get_settings

_TEST_KEY = "api_test_close_key"
_BASE = "https://api.close.example"

_FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _set_close_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOSE_API_KEY", _TEST_KEY)
    monkeypatch.setenv("CLOSE_API_BASE", _BASE)
    get_settings.cache_clear()


def _load_fixture(name: str) -> dict[str, Any]:
    parsed = json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise AssertionError(f"fixture {name} is not a JSON object")
    return parsed


def _transport_returning(
    payload: dict[str, Any],
) -> tuple[
    list[tuple[str, dict[str, str]]],
    httpx.MockTransport,
]:
    """Mock transport that returns the supplied payload for every
    request. Records the request URLs + params for assertion.
    """
    requests: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = {k: v for k, v in request.url.params.items()}
        requests.append((request.url.path, params))
        return httpx.Response(200, json=payload)

    return requests, httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# list_recent_notes
# ---------------------------------------------------------------------------


def test_list_recent_notes_parses_captured_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three notes round-trip through the Pydantic model with ids, bodies,
    and timestamps preserved verbatim."""
    _set_close_env(monkeypatch)
    payload = _load_fixture("activity_note_list.json")
    _, transport = _transport_returning(payload)
    client = CloseClient(http_client=httpx.Client(transport=transport))

    notes = client.list_recent_notes("lead_test", limit=5)
    assert len(notes) == 3
    assert all(isinstance(n, CloseNote) for n in notes)
    assert notes[0].id == "acti_sanitized_NoTe01aBcD"
    assert notes[0].note is not None
    assert "Broker confirmed merchant has 2 active MCA" in notes[0].note
    assert notes[0].date_created == "2026-06-15T19:12:08.512000+00:00"


def test_list_recent_notes_sends_correct_query_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    payload = _load_fixture("activity_note_list.json")
    requests, transport = _transport_returning(payload)
    client = CloseClient(http_client=httpx.Client(transport=transport))

    client.list_recent_notes("lead_xyz", limit=5)

    assert len(requests) == 1
    path, params = requests[0]
    assert path == "/api/v1/activity/note/"
    assert params["lead_id"] == "lead_xyz"
    assert params["_limit"] == "5"


def test_list_recent_notes_clamps_limit_below_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    payload = _load_fixture("activity_note_list.json")
    requests, transport = _transport_returning(payload)
    client = CloseClient(http_client=httpx.Client(transport=transport))

    client.list_recent_notes("lead_test", limit=0)
    _, params = requests[0]
    assert params["_limit"] == "1"


def test_list_recent_notes_raises_on_non_list_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    _, transport = _transport_returning({"data": "not-a-list", "has_more": False})
    client = CloseClient(http_client=httpx.Client(transport=transport))

    with pytest.raises(CloseError):
        client.list_recent_notes("lead_test", limit=5)


def test_list_recent_notes_tolerates_missing_note_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A note record without a ``note`` field still parses — the model
    allows None and the orchestrator filters empties out at the join step.
    """
    _set_close_env(monkeypatch)
    payload = {
        "data": [{"id": "acti_no_body", "note": None, "date_created": "2026-06-15T00:00:00+00:00"}],
        "has_more": False,
    }
    _, transport = _transport_returning(payload)
    client = CloseClient(http_client=httpx.Client(transport=transport))

    notes = client.list_recent_notes("lead_test", limit=5)
    assert len(notes) == 1
    assert notes[0].note is None


# ---------------------------------------------------------------------------
# list_recent_calls
# ---------------------------------------------------------------------------


def test_list_recent_calls_parses_captured_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two calls round-trip; extra fields (duration / direction /
    disposition) are ignored — only id / note / date_created surface."""
    _set_close_env(monkeypatch)
    payload = _load_fixture("activity_call_list.json")
    _, transport = _transport_returning(payload)
    client = CloseClient(http_client=httpx.Client(transport=transport))

    calls = client.list_recent_calls("lead_test", limit=3)
    assert len(calls) == 2
    assert all(isinstance(c, CloseCall) for c in calls)
    assert calls[0].id == "acti_sanitized_CaLL01aBcd"
    assert calls[0].note is not None
    assert "Confirmed merchant has 2 active MCAs" in calls[0].note
    assert calls[1].note is not None
    assert "Discovery call" in calls[1].note


def test_list_recent_calls_sends_correct_query_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    payload = _load_fixture("activity_call_list.json")
    requests, transport = _transport_returning(payload)
    client = CloseClient(http_client=httpx.Client(transport=transport))

    client.list_recent_calls("lead_abc", limit=3)

    assert len(requests) == 1
    path, params = requests[0]
    assert path == "/api/v1/activity/call/"
    assert params["lead_id"] == "lead_abc"
    assert params["_limit"] == "3"


def test_list_recent_calls_raises_on_non_object_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    _, transport = _transport_returning({"data": ["string-not-object"], "has_more": False})
    client = CloseClient(http_client=httpx.Client(transport=transport))

    with pytest.raises(CloseError):
        client.list_recent_calls("lead_test", limit=3)
