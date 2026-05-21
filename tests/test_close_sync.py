"""Tests for src/aegis/close/sync.py — outbound write-back to Close.

Coverage per the step-5 spec:

* First push: all 4 fields PATCHed + Applicant ID set
* Second push, same decision: no-diff path, PATCH NOT called,
  audit shows patched=False
* Decision changes (refer -> approve): PATCH with only Recommendation
  + Last Synced
* Score changes by 1 point: PATCH with Score + Last Synced
* OFAC status changes: PATCH with OFAC Status + Last Synced
* Lead not found (404): no raise, audit logged appropriately
* Lead get fails 5xx: propagates
* Applicant ID already set: not overwritten on subsequent pushes
* Applicant ID not set: set on first push, audited

Also: derive_ofac_status helper, recommendation mapping, score
normalization, PATCH failure path.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest

from aegis.audit import InMemoryAuditLog
from aegis.close.client import CloseClient
from aegis.close.field_map import CLOSE_FIELD_IDS
from aegis.close.sync import (
    SyncError,
    SyncResult,
    derive_ofac_status,
    push_decision_to_close,
)
from aegis.config import get_settings

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _set_close_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOSE_API_KEY", "api_test_close_key")
    monkeypatch.setenv("CLOSE_API_BASE", "https://api.close.example")
    get_settings.cache_clear()


def _cf_key(aegis_name: str) -> str:
    return f"custom.{CLOSE_FIELD_IDS[aegis_name]}"


def _lead_with_aegis_fields(
    *,
    applicant_id: str | None = None,
    score: int | None = None,
    recommendation: str | None = None,
    ofac: str | None = None,
    last_synced: str | None = None,
) -> dict[str, Any]:
    """Build a canned Close Lead payload populated with whatever Aegis-*
    custom fields the test wants set."""
    lead: dict[str, Any] = {"id": "lead_abc", "display_name": "Acme"}
    if applicant_id is not None:
        lead[_cf_key("aegis_applicant_id")] = applicant_id
    if score is not None:
        lead[_cf_key("aegis_score")] = score
    if recommendation is not None:
        lead[_cf_key("aegis_recommendation")] = recommendation
    if ofac is not None:
        lead[_cf_key("ofac_status")] = ofac
    if last_synced is not None:
        lead[_cf_key("aegis_last_synced")] = last_synced
    return lead


class _MockCloseTransport:
    """httpx.MockTransport adapter that records requests and returns
    canned responses keyed by method. Lets tests inspect what was sent
    while also driving the response."""

    def __init__(
        self,
        *,
        get_response: httpx.Response,
        put_response: httpx.Response | None = None,
    ) -> None:
        self._get = get_response
        self._put = put_response or httpx.Response(200, json={"id": "lead_abc"})
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET":
            return self._get
        if request.method == "PUT":
            return self._put
        return httpx.Response(405)


def _make_client(transport: _MockCloseTransport) -> CloseClient:
    return CloseClient(
        http_client=httpx.Client(transport=httpx.MockTransport(transport))
    )


_FIXED_NOW = datetime(2026, 5, 21, 10, 0, 0, tzinfo=UTC)


# ----------------------------------------------------------------------
# derive_ofac_status
# ----------------------------------------------------------------------


def test_derive_ofac_status_flagged_when_reason_code_present() -> None:
    assert (
        derive_ofac_status(
            decision_reason_codes=["ofac_sanctions_match"],
            ofac_cache_timestamp=_FIXED_NOW,
        )
        == "Flagged"
    )


def test_derive_ofac_status_clear_when_timestamp_set_and_no_match() -> None:
    assert (
        derive_ofac_status(
            decision_reason_codes=["other_reason"],
            ofac_cache_timestamp=_FIXED_NOW,
        )
        == "Clear"
    )


def test_derive_ofac_status_pending_when_no_timestamp() -> None:
    assert (
        derive_ofac_status(
            decision_reason_codes=[],
            ofac_cache_timestamp=None,
        )
        == "Pending"
    )


def test_derive_ofac_status_flagged_beats_clear() -> None:
    """If both a match exists AND the cache timestamp is set, Flagged wins."""
    assert (
        derive_ofac_status(
            decision_reason_codes=["ofac_sanctions_match", "x"],
            ofac_cache_timestamp=_FIXED_NOW,
        )
        == "Flagged"
    )


# ----------------------------------------------------------------------
# First push — all 4 fields PATCHed
# ----------------------------------------------------------------------


def test_first_push_patches_all_four_fields_and_sets_applicant_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    decision_id = uuid4()
    audit = InMemoryAuditLog()
    transport = _MockCloseTransport(
        get_response=httpx.Response(200, json=_lead_with_aegis_fields()),
    )

    result = push_decision_to_close(
        close_lead_id="lead_abc",
        decision_id=decision_id,
        score=Decimal("78"),
        recommendation="approve",
        ofac_status="Clear",
        client=_make_client(transport),
        audit=audit,
        now=_FIXED_NOW,
    )

    assert result == SyncResult(
        patched=True,
        fields_diffed=sorted(
            ["aegis_applicant_id", "aegis_score", "aegis_recommendation", "ofac_status"]
        ),
        reason="patched",
    )

    # One GET + one PUT.
    methods = [r.method for r in transport.requests]
    assert methods == ["GET", "PUT"]

    put_body = json.loads(transport.requests[1].content)
    # All four business fields + Last Synced.
    assert put_body[_cf_key("aegis_applicant_id")] == str(decision_id)
    assert put_body[_cf_key("aegis_score")] == 78
    assert put_body[_cf_key("aegis_recommendation")] == "Approve"
    assert put_body[_cf_key("ofac_status")] == "Clear"
    assert put_body[_cf_key("aegis_last_synced")] == _FIXED_NOW.isoformat()

    # Audit row.
    attempts = [e for e in audit.entries if e["action"] == "close.lead.sync_attempted"]
    assert len(attempts) == 1
    assert attempts[0]["details"]["patched"] is True
    assert "aegis_applicant_id" in attempts[0]["details"]["fields_diffed"]


# ----------------------------------------------------------------------
# Second push, same decision — no-diff, no PATCH
# ----------------------------------------------------------------------


def test_second_push_same_decision_no_patch_no_diff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If Close already has the desired Aegis-* values, no PATCH fires
    and the audit row shows patched=False with empty fields_diffed."""
    _set_close_env(monkeypatch)
    decision_id = uuid4()
    audit = InMemoryAuditLog()
    transport = _MockCloseTransport(
        get_response=httpx.Response(
            200,
            json=_lead_with_aegis_fields(
                applicant_id=str(decision_id),
                score=78,
                recommendation="Approve",
                ofac="Clear",
                # Last Synced from a prior run — value-aware sync ignores it.
                last_synced="2026-05-15T00:00:00+00:00",
            ),
        ),
    )

    result = push_decision_to_close(
        close_lead_id="lead_abc",
        decision_id=decision_id,
        score=78,
        recommendation="approve",
        ofac_status="Clear",
        client=_make_client(transport),
        audit=audit,
        now=_FIXED_NOW,
    )

    assert result.patched is False
    assert result.fields_diffed == []
    assert result.reason == "no_diff"

    # Only GET, no PUT.
    methods = [r.method for r in transport.requests]
    assert methods == ["GET"], (
        f"PATCH should not fire when all business fields match; saw {methods}"
    )

    attempts = [e for e in audit.entries if e["action"] == "close.lead.sync_attempted"]
    assert len(attempts) == 1
    assert attempts[0]["details"]["patched"] is False
    assert attempts[0]["details"]["fields_diffed"] == []


# ----------------------------------------------------------------------
# Decision changes — only the changed business field + Last Synced
# ----------------------------------------------------------------------


def test_recommendation_change_patches_only_recommendation_and_last_synced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    decision_id = uuid4()
    audit = InMemoryAuditLog()
    # Close holds "Refer"; AEGIS now wants "Approve".
    transport = _MockCloseTransport(
        get_response=httpx.Response(
            200,
            json=_lead_with_aegis_fields(
                applicant_id=str(decision_id),
                score=78,
                recommendation="Refer",
                ofac="Clear",
            ),
        ),
    )

    result = push_decision_to_close(
        close_lead_id="lead_abc",
        decision_id=decision_id,
        score=78,
        recommendation="approve",
        ofac_status="Clear",
        client=_make_client(transport),
        audit=audit,
        now=_FIXED_NOW,
    )

    assert result.patched is True
    assert result.fields_diffed == ["aegis_recommendation"]

    put_body = json.loads(transport.requests[1].content)
    # Only recommendation + last synced. NOT score, applicant_id, ofac.
    assert set(put_body.keys()) == {
        _cf_key("aegis_recommendation"),
        _cf_key("aegis_last_synced"),
    }
    assert put_body[_cf_key("aegis_recommendation")] == "Approve"


def test_score_change_by_one_point_patches_only_score_and_last_synced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One-point score drift triggers a PATCH carrying only score +
    Last Synced. The other three business fields stay untouched."""
    _set_close_env(monkeypatch)
    decision_id = uuid4()
    audit = InMemoryAuditLog()
    transport = _MockCloseTransport(
        get_response=httpx.Response(
            200,
            json=_lead_with_aegis_fields(
                applicant_id=str(decision_id),
                score=77,
                recommendation="Approve",
                ofac="Clear",
            ),
        ),
    )

    result = push_decision_to_close(
        close_lead_id="lead_abc",
        decision_id=decision_id,
        score=78,
        recommendation="approve",
        ofac_status="Clear",
        client=_make_client(transport),
        audit=audit,
        now=_FIXED_NOW,
    )

    assert result.fields_diffed == ["aegis_score"]
    put_body = json.loads(transport.requests[1].content)
    assert set(put_body.keys()) == {
        _cf_key("aegis_score"),
        _cf_key("aegis_last_synced"),
    }
    assert put_body[_cf_key("aegis_score")] == 78


def test_ofac_change_patches_only_ofac_and_last_synced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    decision_id = uuid4()
    audit = InMemoryAuditLog()
    transport = _MockCloseTransport(
        get_response=httpx.Response(
            200,
            json=_lead_with_aegis_fields(
                applicant_id=str(decision_id),
                score=78,
                recommendation="Approve",
                ofac="Pending",
            ),
        ),
    )

    result = push_decision_to_close(
        close_lead_id="lead_abc",
        decision_id=decision_id,
        score=78,
        recommendation="approve",
        ofac_status="Clear",
        client=_make_client(transport),
        audit=audit,
        now=_FIXED_NOW,
    )

    assert result.fields_diffed == ["ofac_status"]
    put_body = json.loads(transport.requests[1].content)
    assert set(put_body.keys()) == {
        _cf_key("ofac_status"),
        _cf_key("aegis_last_synced"),
    }
    assert put_body[_cf_key("ofac_status")] == "Clear"


# ----------------------------------------------------------------------
# Applicant ID set-once semantics
# ----------------------------------------------------------------------


def test_applicant_id_set_on_first_push_when_close_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    decision_id = uuid4()
    audit = InMemoryAuditLog()
    transport = _MockCloseTransport(
        get_response=httpx.Response(
            200,
            json=_lead_with_aegis_fields(
                # No applicant_id — Close field empty.
                score=78,
                recommendation="Approve",
                ofac="Clear",
            ),
        ),
    )

    result = push_decision_to_close(
        close_lead_id="lead_abc",
        decision_id=decision_id,
        score=78,
        recommendation="approve",
        ofac_status="Clear",
        client=_make_client(transport),
        audit=audit,
        now=_FIXED_NOW,
    )

    assert "aegis_applicant_id" in result.fields_diffed
    put_body = json.loads(transport.requests[1].content)
    assert put_body[_cf_key("aegis_applicant_id")] == str(decision_id)


def test_applicant_id_never_overwritten_on_subsequent_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close already has an Applicant ID (set on a prior push, possibly
    with a different decision_id). The current push must NOT overwrite
    it — it's the operator's stable reference."""
    _set_close_env(monkeypatch)
    old_applicant_id = str(uuid4())  # Close holds this
    new_decision_id = uuid4()  # this push has a different id
    audit = InMemoryAuditLog()
    transport = _MockCloseTransport(
        get_response=httpx.Response(
            200,
            json=_lead_with_aegis_fields(
                applicant_id=old_applicant_id,
                score=77,  # different from desired -> forces a PATCH
                recommendation="Approve",
                ofac="Clear",
            ),
        ),
    )

    result = push_decision_to_close(
        close_lead_id="lead_abc",
        decision_id=new_decision_id,
        score=78,
        recommendation="approve",
        ofac_status="Clear",
        client=_make_client(transport),
        audit=audit,
        now=_FIXED_NOW,
    )

    # A PATCH did fire (score differed) — but the applicant_id was NOT in
    # the diff because Close's value won.
    assert "aegis_applicant_id" not in result.fields_diffed
    put_body = json.loads(transport.requests[1].content)
    assert _cf_key("aegis_applicant_id") not in put_body, (
        "applicant_id must not appear in PATCH body when Close already has one"
    )


# ----------------------------------------------------------------------
# Lead not found (404) — no raise
# ----------------------------------------------------------------------


def test_lead_not_found_returns_result_no_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    decision_id = uuid4()
    audit = InMemoryAuditLog()
    transport = _MockCloseTransport(
        get_response=httpx.Response(404, text="not found"),
    )

    result = push_decision_to_close(
        close_lead_id="lead_deleted",
        decision_id=decision_id,
        score=78,
        recommendation="approve",
        ofac_status="Clear",
        client=_make_client(transport),
        audit=audit,
        now=_FIXED_NOW,
    )

    assert result.reason == "lead_not_found"
    assert result.patched is False
    # No PUT fired.
    assert [r.method for r in transport.requests] == ["GET"]
    # Audit logged with the not_found action.
    not_found = [
        e for e in audit.entries if e["action"] == "close.lead.sync_failed_not_found"
    ]
    assert len(not_found) == 1
    assert not_found[0]["details"]["close_lead_id"] == "lead_deleted"


# ----------------------------------------------------------------------
# GET 5xx propagates after CloseClient retries
# ----------------------------------------------------------------------


def test_get_lead_5xx_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_close_env(monkeypatch)
    monkeypatch.setattr("aegis.close.client.time.sleep", lambda _s: None)
    audit = InMemoryAuditLog()
    transport = _MockCloseTransport(
        get_response=httpx.Response(500, text="boom"),
    )

    with pytest.raises(Exception):  # noqa: B017 — CloseRateLimitError 5xx after retry
        push_decision_to_close(
            close_lead_id="lead_abc",
            decision_id=uuid4(),
            score=78,
            recommendation="approve",
            ofac_status="Clear",
            client=_make_client(transport),
            audit=audit,
            now=_FIXED_NOW,
        )


# ----------------------------------------------------------------------
# PATCH failure — audit row + raise
# ----------------------------------------------------------------------


def test_patch_failure_audits_and_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 400 on the PATCH must produce an audit row (patched=False,
    error_status=400) AND propagate the CloseError so the caller sees
    the failure."""
    _set_close_env(monkeypatch)
    decision_id = uuid4()
    audit = InMemoryAuditLog()
    transport = _MockCloseTransport(
        get_response=httpx.Response(
            200,
            json=_lead_with_aegis_fields(
                # Drift → forces a PATCH attempt.
                score=10,
                recommendation="Refer",
                ofac="Pending",
            ),
        ),
        put_response=httpx.Response(400, text="bad field value"),
    )

    with pytest.raises(Exception):  # noqa: B017 — CloseError 400
        push_decision_to_close(
            close_lead_id="lead_abc",
            decision_id=decision_id,
            score=78,
            recommendation="approve",
            ofac_status="Clear",
            client=_make_client(transport),
            audit=audit,
            now=_FIXED_NOW,
        )

    attempts = [e for e in audit.entries if e["action"] == "close.lead.sync_attempted"]
    assert len(attempts) == 1
    assert attempts[0]["details"]["patched"] is False
    assert attempts[0]["details"]["error_status"] == 400
    assert attempts[0]["details"]["fields_diffed"]  # non-empty


# ----------------------------------------------------------------------
# Recommendation validation
# ----------------------------------------------------------------------


def test_redisclosure_recommendation_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """redisclosure is a DecisionLiteral but NOT a recommendation we
    push to Close — push_decision_to_close must refuse rather than
    silently treating it as something else."""
    _set_close_env(monkeypatch)
    audit = InMemoryAuditLog()
    transport = _MockCloseTransport(
        get_response=httpx.Response(200, json=_lead_with_aegis_fields()),
    )

    with pytest.raises(SyncError, match="cannot be pushed to Close"):
        push_decision_to_close(
            close_lead_id="lead_abc",
            decision_id=uuid4(),
            score=78,
            recommendation="redisclosure",
            ofac_status="Clear",
            client=_make_client(transport),
            audit=audit,
            now=_FIXED_NOW,
        )


def test_unknown_recommendation_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_close_env(monkeypatch)
    audit = InMemoryAuditLog()
    transport = _MockCloseTransport(
        get_response=httpx.Response(200, json=_lead_with_aegis_fields()),
    )

    with pytest.raises(SyncError):
        push_decision_to_close(
            close_lead_id="lead_abc",
            decision_id=uuid4(),
            score=78,
            recommendation="fraud_alert",
            ofac_status="Clear",
            client=_make_client(transport),
            audit=audit,
            now=_FIXED_NOW,
        )


# ----------------------------------------------------------------------
# Manual review maps to Refer
# ----------------------------------------------------------------------


def test_manual_review_maps_to_refer(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_close_env(monkeypatch)
    decision_id = uuid4()
    audit = InMemoryAuditLog()
    transport = _MockCloseTransport(
        get_response=httpx.Response(200, json=_lead_with_aegis_fields()),
    )

    push_decision_to_close(
        close_lead_id="lead_abc",
        decision_id=decision_id,
        score=55,
        recommendation="manual_review",
        ofac_status="Pending",
        client=_make_client(transport),
        audit=audit,
        now=_FIXED_NOW,
    )

    put_body = json.loads(transport.requests[1].content)
    assert put_body[_cf_key("aegis_recommendation")] == "Refer"


# ----------------------------------------------------------------------
# None score handled (no decision recorded score)
# ----------------------------------------------------------------------


def test_none_score_skips_score_field_when_close_also_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If AEGIS has no score (e.g. hard-decline before scoring) AND
    Close also has no Aegis Score, the score field is not part of the
    diff."""
    _set_close_env(monkeypatch)
    decision_id = uuid4()
    audit = InMemoryAuditLog()
    transport = _MockCloseTransport(
        get_response=httpx.Response(
            200,
            json=_lead_with_aegis_fields(
                applicant_id=str(decision_id),
                # No score field.
                recommendation="Decline",
                ofac="Clear",
            ),
        ),
    )

    result = push_decision_to_close(
        close_lead_id="lead_abc",
        decision_id=decision_id,
        score=None,
        recommendation="decline",
        ofac_status="Clear",
        client=_make_client(transport),
        audit=audit,
        now=_FIXED_NOW,
    )

    assert "aegis_score" not in result.fields_diffed
