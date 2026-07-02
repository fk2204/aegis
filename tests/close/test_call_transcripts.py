"""Tests for ``aegis.close.field_map.fetch_call_transcripts_for_lead``.

Covers three shapes:

  * Happy path — captured-fixture-style payload with two calls returns
    the ``[Call YYYY-MM-DD — Ns]\\n{note}`` blocks joined by ``\\n\\n``.
  * Empty path — empty ``data`` list, every note empty, and missing
    ``data`` key all collapse to ``None`` (never a bare
    separator-only string).
  * Exception path — ``client.request`` raises any exception. The helper
    logs a warning and returns ``None`` — best-effort, never blocks
    the caller.

Uses ``httpx.MockTransport`` per CLAUDE.md external-integration test
discipline (captured-payload shape mirrors the real
``/api/v1/activity/call/`` response).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from aegis.close.client import CloseClient, CloseError
from aegis.close.field_map import fetch_call_transcripts_for_lead
from aegis.config import get_settings

_TEST_KEY = "api_test_close_key"
_BASE = "https://api.close.example"


def _set_close_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOSE_API_KEY", _TEST_KEY)
    monkeypatch.setenv("CLOSE_API_BASE", _BASE)
    get_settings.cache_clear()


def _client_returning(
    payload: dict[str, Any],
) -> tuple[list[tuple[str, dict[str, str]]], CloseClient]:
    """Build a CloseClient that returns ``payload`` for every request
    and records path + params so callers can assert the wire shape."""
    requests: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = {k: v for k, v in request.url.params.items()}
        requests.append((request.url.path, params))
        return httpx.Response(200, json=payload)

    return requests, CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def _client_raising(exc: BaseException) -> CloseClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(handler)))


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_fetch_call_transcripts_returns_formatted_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    payload = {
        "data": [
            {
                "id": "acti_test_call_01",
                "note": "Confirmed merchant has 2 active MCAs. Sending statements EOD Friday.",
                "date_created": "2026-06-15T17:30:00.000000+00:00",
                "duration": 442,
            },
            {
                "id": "acti_test_call_02",
                "note": "Discovery call. Owner mentioned looking at 3 lenders.",
                "date_created": "2026-06-13T10:15:00.000000+00:00",
                "duration": 318,
            },
        ],
        "has_more": False,
    }
    requests, client = _client_returning(payload)

    result = fetch_call_transcripts_for_lead("lead_test_123", client)

    assert result is not None
    # Both blocks present, header + note pattern intact, joined by \n\n
    assert "[Call 2026-06-15 — 442s]" in result
    assert "Confirmed merchant has 2 active MCAs" in result
    assert "[Call 2026-06-13 — 318s]" in result
    assert "Discovery call" in result
    assert "\n\n" in result
    # Wire params were pinned per spec
    assert len(requests) == 1
    path, params = requests[0]
    assert path == "/api/v1/activity/call/"
    assert params["lead_id"] == "lead_test_123"
    assert params["_limit"] == "10"
    assert params["_fields"] == "note,duration,date_created"
    assert params["_order_by"] == "-date_created"


def test_fetch_call_transcripts_falls_back_when_date_or_duration_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing / unparseable ``date_created`` and ``duration`` produce
    ``[Call unknown — ?s]`` rather than dropping the row — the operator
    still gets the note body."""
    _set_close_env(monkeypatch)
    payload = {
        "data": [
            {
                "id": "acti_no_meta",
                "note": "call happened but metadata missing",
                # date_created + duration omitted
            }
        ],
        "has_more": False,
    }
    _, client = _client_returning(payload)

    result = fetch_call_transcripts_for_lead("lead_test", client)
    assert result is not None
    assert result.startswith("[Call unknown — ?s]")
    assert "call happened but metadata missing" in result


# ---------------------------------------------------------------------------
# Empty path
# ---------------------------------------------------------------------------


def test_fetch_call_transcripts_returns_none_on_empty_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    _, client = _client_returning({"data": [], "has_more": False})

    assert fetch_call_transcripts_for_lead("lead_test", client) is None


def test_fetch_call_transcripts_returns_none_when_every_note_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A payload where every ``note`` is empty / whitespace / None
    collapses to ``None`` — no separator-only artifact leaks into the
    merchant column."""
    _set_close_env(monkeypatch)
    payload = {
        "data": [
            {"id": "a", "note": "", "date_created": "2026-06-15T00:00:00+00:00", "duration": 10},
            {"id": "b", "note": "   ", "date_created": "2026-06-14T00:00:00+00:00", "duration": 20},
            {"id": "c", "note": None, "date_created": "2026-06-13T00:00:00+00:00", "duration": 30},
        ],
        "has_more": False,
    }
    _, client = _client_returning(payload)

    assert fetch_call_transcripts_for_lead("lead_test", client) is None


def test_fetch_call_transcripts_returns_none_when_data_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    _, client = _client_returning({"has_more": False})

    assert fetch_call_transcripts_for_lead("lead_test", client) is None


# ---------------------------------------------------------------------------
# Exception path
# ---------------------------------------------------------------------------


def test_fetch_call_transcripts_returns_none_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    client = _client_raising(httpx.ConnectError("simulated network drop"))

    assert fetch_call_transcripts_for_lead("lead_test", client) is None


def test_fetch_call_transcripts_returns_none_on_close_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CloseError (e.g. 500 from Close) is caught — the helper never
    blocks merchant creation on Close-side failure."""
    _set_close_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = fetch_call_transcripts_for_lead("lead_test", client)
    assert result is None
    # And a bare CloseError raise from the wire would have surfaced —
    # the helper's blanket except covers it.
    assert not isinstance(result, CloseError)
