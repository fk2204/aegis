"""Tests for ``aegis.close.lead_intelligence`` — dossier-side aggregation
of Close lead context (status, description, activities, agent notes,
document folder URL).

Fixtures
--------
* ``lead_with_description.json`` — pre-existing sanitized Close Lead
  payload. The description is rewritten per-test to exercise the
  Zoho / Drive URL extractor (the canned description has no URL).
* ``activity_unified_feed.json`` — captured + sanitized
  ``GET /api/v1/activity/?lead_id=...`` payload covering Call, Note,
  Email, LeadStatusChange row types. Named individuals + real org/user
  IDs sanitized in-fixture.

PII discipline (CLAUDE.md): the audit-log row written by
``get_lead_intelligence`` MUST NOT include note bodies — only counts.
``test_audit_log_carries_counts_only_no_pii`` is the canary.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from aegis.audit import InMemoryAuditLog
from aegis.close.client import CloseClient
from aegis.close.lead_intelligence import (
    CloseActivity,
    CloseLeadIntelligence,
    _clear_cache_for_tests,
    get_lead_intelligence,
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


def _build_client(
    lead_payload: dict[str, Any],
    activity_payload: dict[str, Any],
) -> CloseClient:
    """MockTransport that routes ``/lead/{id}/`` → lead_payload and
    ``/activity/`` → activity_payload. Anything else returns 404 so an
    unexpected call surfaces loudly in tests."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/api/v1/lead/"):
            return httpx.Response(200, json=lead_payload)
        if path == "/api/v1/activity/":
            return httpx.Response(200, json=activity_payload)
        return httpx.Response(404, json={"error": f"unexpected path {path}"})

    return CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.fixture(autouse=True)
def _clear_intel_cache() -> None:
    """Drop the module-level lead_intelligence cache between every test.
    Without this, the second test reuses the first test's result and
    every assertion past the first hit reads a stale payload."""
    _clear_cache_for_tests()


# ---------------------------------------------------------------------------
# Activity extraction — Call / Note / Email mix
# ---------------------------------------------------------------------------


def test_extracts_mixed_activity_types_sorted_desc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Call + Note + Email + LeadStatusChange all surface; sort is by
    date DESC; the call/note/email/status_change taxonomy is honored."""
    _set_close_env(monkeypatch)
    lead = _load_fixture("lead_with_description.json")
    activities = _load_fixture("activity_unified_feed.json")
    client = _build_client(lead, activities)

    intel = get_lead_intelligence(client, "lead_test")

    assert isinstance(intel, CloseLeadIntelligence)
    assert intel.lead_id == "lead_test"
    assert intel.status_label == "Active"
    # 5 raw rows: 1 Call + 2 Notes + 1 Email + 1 LeadStatusChange = all 5
    # surface (cap is 20). Sort is DESC by date_created.
    assert len(intel.activities) == 5
    assert [a.type for a in intel.activities] == [
        "call",
        "note",
        "email",
        "status_change",
        "note",
    ]
    # First row is the Call carrying the operator's post-call note.
    first = intel.activities[0]
    assert first.type == "call"
    assert first.direction == "outbound"
    assert "Confirmed merchant has 2 active MCAs" in first.summary
    # Email row pulls the subject (not the body) into summary.
    email_row = next(a for a in intel.activities if a.type == "email")
    assert email_row.summary == "Stips request — Sanitized Test Merchant"
    # Status change row pulls the new status label.
    status_row = next(a for a in intel.activities if a.type == "status_change")
    assert status_row.summary == "Underwriting"


def test_call_count_and_last_contact(monkeypatch: pytest.MonkeyPatch) -> None:
    """``call_count`` counts Calls only. ``last_contact`` is the most
    recent Call OR Email date (Notes don't qualify as outbound contact)."""
    _set_close_env(monkeypatch)
    lead = _load_fixture("lead_with_description.json")
    activities = _load_fixture("activity_unified_feed.json")
    client = _build_client(lead, activities)

    intel = get_lead_intelligence(client, "lead_test")

    assert intel.call_count == 1
    # Most recent contact = the Call at 2026-06-25 (newer than the Email
    # at 2026-06-23). last_contact is the raw ISO string from Close.
    assert intel.last_contact is not None
    assert intel.last_contact.startswith("2026-06-25")


def test_agent_notes_collect_note_bodies_desc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``agent_notes`` collects Note bodies (call notes are NOT folded in),
    sorted DESC by date, capped at 10."""
    _set_close_env(monkeypatch)
    lead = _load_fixture("lead_with_description.json")
    activities = _load_fixture("activity_unified_feed.json")
    client = _build_client(lead, activities)

    intel = get_lead_intelligence(client, "lead_test")

    # Two Note rows in the fixture.
    assert len(intel.agent_notes) == 2
    # First (newest) note is the 2026-06-24 "Broker confirmed" body.
    assert "Broker confirmed merchant has 2 active MCA" in intel.agent_notes[0]
    assert "prior decline from REDACTED_FUNDER" in intel.agent_notes[1]


# ---------------------------------------------------------------------------
# Document folder URL extraction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "description,expected",
    [
        # Zoho WorkDrive folder URL embedded in a sentence.
        (
            "Broker uploaded docs to https://workdrive.zoho.com/folder/abc123def — review tonight.",
            "https://workdrive.zoho.com/folder/abc123def",
        ),
        # Google Drive folder URL with query string.
        (
            "Folder: https://drive.google.com/drive/folders/1abcXYZ?usp=sharing for statements.",
            "https://drive.google.com/drive/folders/1abcXYZ?usp=sharing",
        ),
        # Trailing punctuation stripped.
        (
            "See https://drive.google.com/file/abc, sending now.",
            "https://drive.google.com/file/abc",
        ),
    ],
)
def test_document_folder_url_extracted(
    monkeypatch: pytest.MonkeyPatch,
    description: str,
    expected: str,
) -> None:
    _set_close_env(monkeypatch)
    lead = dict(_load_fixture("lead_with_description.json"))
    lead["description"] = description
    activities = {"data": [], "has_more": False}
    client = _build_client(lead, activities)

    intel = get_lead_intelligence(client, f"lead_doc_{uuid4().hex[:6]}")

    assert intel.document_folder_url == expected


@pytest.mark.parametrize(
    "description",
    [
        # Dropbox is not in the whitelist.
        "Broker sent files via https://www.dropbox.com/sh/abc/def — please pull.",
        # No URL at all.
        "Standard broker description with no links anywhere.",
        # Bare host with no scheme — must NOT match.
        "workdrive.zoho.com/folder/abc",
        "",
    ],
)
def test_document_folder_url_misses_non_matching(
    monkeypatch: pytest.MonkeyPatch,
    description: str,
) -> None:
    _set_close_env(monkeypatch)
    lead = dict(_load_fixture("lead_with_description.json"))
    lead["description"] = description
    activities = {"data": [], "has_more": False}
    client = _build_client(lead, activities)

    intel = get_lead_intelligence(client, f"lead_nodoc_{uuid4().hex[:6]}")

    assert intel.document_folder_url is None


# ---------------------------------------------------------------------------
# Disqualified detection
# ---------------------------------------------------------------------------


def test_disqualified_reason_set_when_status_contains_disqualified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    lead = dict(_load_fixture("lead_with_description.json"))
    lead["status_label"] = "Disqualified - bad fit"
    activities = {"data": [], "has_more": False}
    client = _build_client(lead, activities)

    intel = get_lead_intelligence(client, "lead_dq")

    assert intel.disqualified_reason == "Disqualified - bad fit"


def test_disqualified_reason_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    lead = dict(_load_fixture("lead_with_description.json"))
    lead["status_label"] = "DISQUALIFIED"
    activities = {"data": [], "has_more": False}
    client = _build_client(lead, activities)

    intel = get_lead_intelligence(client, "lead_dq_caps")

    assert intel.disqualified_reason == "DISQUALIFIED"


def test_disqualified_reason_none_when_status_normal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    lead = dict(_load_fixture("lead_with_description.json"))
    lead["status_label"] = "Active"
    activities = {"data": [], "has_more": False}
    client = _build_client(lead, activities)

    intel = get_lead_intelligence(client, "lead_active")

    assert intel.disqualified_reason is None


# ---------------------------------------------------------------------------
# Zero-activity lead
# ---------------------------------------------------------------------------


def test_zero_activity_lead_returns_empty_lists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lead with no activities returns a CloseLeadIntelligence with
    empty ``activities`` + ``agent_notes`` and zero ``call_count``.
    The dossier template uses this to hide the section entirely."""
    _set_close_env(monkeypatch)
    lead = _load_fixture("lead_with_description.json")
    activities = {"data": [], "has_more": False}
    client = _build_client(lead, activities)

    intel = get_lead_intelligence(client, "lead_empty")

    assert intel.activities == []
    assert intel.agent_notes == []
    assert intel.call_count == 0
    assert intel.last_contact is None
    # The status / description still populate from the lead payload.
    assert intel.status_label == "Active"
    assert intel.description.startswith("Inbound from broker")


# ---------------------------------------------------------------------------
# PII canary on the audit row
# ---------------------------------------------------------------------------


def test_audit_log_carries_counts_only_no_pii(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audit row MUST NOT include note bodies, call dispositions, or
    email subjects. Only ``lead_id`` + counts (``activity_count`` /
    ``call_count``). Failing this test means a PII leak shipped."""
    _set_close_env(monkeypatch)
    lead = _load_fixture("lead_with_description.json")
    activities = _load_fixture("activity_unified_feed.json")
    client = _build_client(lead, activities)
    audit = InMemoryAuditLog()

    get_lead_intelligence(client, "lead_test", audit=audit)

    rows = [e for e in audit.entries if e["action"] == "close.intelligence_fetched"]
    assert len(rows) == 1
    row = rows[0]
    details = row["details"]
    assert isinstance(details, dict)
    assert set(details.keys()) == {"lead_id", "activity_count", "call_count"}
    assert details["lead_id"] == "lead_test"
    assert details["activity_count"] == 5
    assert details["call_count"] == 1
    # PII canary — no body strings, no subject strings.
    serialized = json.dumps(details)
    forbidden = [
        "Broker confirmed",
        "Confirmed merchant",
        "Stips request",
        "REDACTED_FUNDER",
        "Underwriting",
    ]
    for token in forbidden:
        assert token not in serialized, f"PII leak — found {token!r} in audit details"


# ---------------------------------------------------------------------------
# Cache TTL
# ---------------------------------------------------------------------------


def test_second_call_within_ttl_does_not_refetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Within the 15-min TTL the second call returns the cached object
    without re-hitting Close."""
    _set_close_env(monkeypatch)
    lead = _load_fixture("lead_with_description.json")
    activities = _load_fixture("activity_unified_feed.json")

    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        path = request.url.path
        if path.startswith("/api/v1/lead/"):
            return httpx.Response(200, json=lead)
        if path == "/api/v1/activity/":
            return httpx.Response(200, json=activities)
        return httpx.Response(404)

    client = CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    first = get_lead_intelligence(client, "lead_cache_test")
    n_after_first = call_count["n"]
    second = get_lead_intelligence(client, "lead_cache_test")
    n_after_second = call_count["n"]

    assert first is second  # exact same cached object
    assert n_after_second == n_after_first  # no extra Close calls


# ---------------------------------------------------------------------------
# Type round-trip
# ---------------------------------------------------------------------------


def test_close_activity_model_round_trips() -> None:
    """``CloseActivity`` accepts the canonical taxonomy and validates."""
    activity = CloseActivity(
        type="call",
        date="2026-06-25T17:30:00+00:00",
        user_name="Test Agent",
        summary="discovery call",
        direction="outbound",
    )
    assert activity.type == "call"
    assert activity.direction == "outbound"
