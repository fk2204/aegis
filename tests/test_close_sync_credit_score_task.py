"""Tests for the credit-score Close task spawned by push_decision_to_close
when the merchant has no credit score on file.

Behavior:
  * merchant.credit_score is None or 0 → after successful PATCH, POST a
    Close task and write `close.task.credit_score_requested` audit row.
  * Audit row is the dedupe key — a second sync skips silently.
  * merchant.credit_score is set → no task created.
  * merchant kwarg omitted → backward-compatible, no task created.
  * Close 4xx/5xx on task POST → audit a failure row but do NOT raise
    (the core sync already succeeded; the task is best-effort).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest

from aegis.audit import InMemoryAuditLog
from aegis.close.client import CloseClient
from aegis.close.field_map import CLOSE_FIELD_IDS
from aegis.close.sync import push_decision_to_close
from aegis.config import get_settings
from aegis.merchants.models import MerchantRow


def _set_close_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOSE_API_KEY", "api_test_close_key")
    monkeypatch.setenv("CLOSE_API_BASE", "https://api.close.example")
    get_settings.cache_clear()


def _cf_key(aegis_name: str) -> str:
    return f"custom.{CLOSE_FIELD_IDS[aegis_name]}"


def _lead_payload() -> dict[str, Any]:
    return {"id": "lead_abc", "display_name": "Acme"}


class _RecordingTransport:
    """Returns canned responses keyed by (method, path-prefix). Records
    every request so tests can inspect what was sent.

    Defaults:
      GET  /api/v1/lead/   → 200 with lead_payload
      PUT  /api/v1/lead/   → 200
      POST /api/v1/task/   → 201
    """

    def __init__(
        self,
        *,
        lead_payload: dict[str, Any] | None = None,
        task_response: httpx.Response | None = None,
    ) -> None:
        self._lead_payload = lead_payload or _lead_payload()
        self._task_response = task_response or httpx.Response(201, json={"id": "task_xyz"})
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET" and "/lead/" in request.url.path:
            return httpx.Response(200, json=self._lead_payload)
        if request.method == "PUT" and "/lead/" in request.url.path:
            return httpx.Response(200, json={"id": "lead_abc"})
        if request.method == "POST" and request.url.path.endswith("/task/"):
            return self._task_response
        return httpx.Response(405)

    def task_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST" and r.url.path.endswith("/task/")]


def _make_client(transport: _RecordingTransport) -> CloseClient:
    return CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport)))


_FIXED_NOW = datetime(2026, 5, 21, 10, 0, 0, tzinfo=UTC)


def _merchant(credit_score: int | None) -> MerchantRow:
    return MerchantRow(
        business_name="Acme Logistics LLC",
        credit_score=credit_score,
        close_lead_id="lead_abc",
    )


# ---------------------------------------------------------------------------


def test_task_created_when_credit_score_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_close_env(monkeypatch)
    merchant = _merchant(credit_score=None)
    audit = InMemoryAuditLog()
    transport = _RecordingTransport()

    push_decision_to_close(
        close_lead_id="lead_abc",
        decision_id=uuid4(),
        score=Decimal("70"),
        recommendation="approve",
        ofac_status="Clear",
        client=_make_client(transport),
        audit=audit,
        now=_FIXED_NOW,
        merchant=merchant,
    )

    task_reqs = transport.task_requests()
    assert len(task_reqs) == 1
    body = task_reqs[0].read()
    import json as _json

    payload = _json.loads(body)
    assert payload["_type"] == "lead"
    assert payload["lead_id"] == "lead_abc"
    assert "Acme Logistics LLC" in payload["text"]
    assert payload["date"] == (date(2026, 5, 22)).isoformat()

    audited = [e for e in audit.entries if e["action"] == "close.task.credit_score_requested"]
    assert len(audited) == 1
    assert audited[0]["subject_type"] == "merchant"
    assert audited[0]["subject_id"] == str(merchant.id)
    assert audited[0]["details"]["close_lead_id"] == "lead_abc"


def test_no_task_when_credit_score_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_close_env(monkeypatch)
    audit = InMemoryAuditLog()
    transport = _RecordingTransport()
    push_decision_to_close(
        close_lead_id="lead_abc",
        decision_id=uuid4(),
        score=Decimal("70"),
        recommendation="approve",
        ofac_status="Clear",
        client=_make_client(transport),
        audit=audit,
        now=_FIXED_NOW,
        merchant=_merchant(credit_score=720),
    )
    assert transport.task_requests() == []
    assert not any(e["action"] == "close.task.credit_score_requested" for e in audit.entries)


def test_no_task_when_merchant_kwarg_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backward-compat: every existing call site that doesn't supply
    merchant gets the prior (no-task) behavior."""
    _set_close_env(monkeypatch)
    audit = InMemoryAuditLog()
    transport = _RecordingTransport()
    push_decision_to_close(
        close_lead_id="lead_abc",
        decision_id=uuid4(),
        score=Decimal("70"),
        recommendation="approve",
        ofac_status="Clear",
        client=_make_client(transport),
        audit=audit,
        now=_FIXED_NOW,
    )
    assert transport.task_requests() == []


def test_second_sync_with_missing_credit_does_not_duplicate_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the audit row exists, subsequent syncs skip the task call."""
    _set_close_env(monkeypatch)
    merchant = _merchant(credit_score=None)
    audit = InMemoryAuditLog()
    transport = _RecordingTransport()

    push_decision_to_close(
        close_lead_id="lead_abc",
        decision_id=uuid4(),
        score=Decimal("70"),
        recommendation="approve",
        ofac_status="Clear",
        client=_make_client(transport),
        audit=audit,
        now=_FIXED_NOW,
        merchant=merchant,
    )
    # Second sync — same merchant, audit row already exists. Use a
    # FRESH transport so we can assert no task POST fired on this call;
    # the existing transport's request log carries the first call's
    # entry.
    transport2 = _RecordingTransport(
        lead_payload={
            "id": "lead_abc",
            "display_name": "Acme",
            _cf_key("aegis_score"): 99,
        }
    )
    push_decision_to_close(
        close_lead_id="lead_abc",
        decision_id=uuid4(),
        score=Decimal("70"),
        recommendation="approve",
        ofac_status="Clear",
        client=_make_client(transport2),
        audit=audit,
        now=_FIXED_NOW,
        merchant=merchant,
    )
    assert transport2.task_requests() == [], "second sync must not POST a second task"
    audited = [e for e in audit.entries if e["action"] == "close.task.credit_score_requested"]
    assert len(audited) == 1


def test_task_post_failure_audits_but_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    audit = InMemoryAuditLog()
    transport = _RecordingTransport(
        task_response=httpx.Response(400, json={"error": "bad task"}),
    )
    # Should NOT raise — sync already succeeded; task is best-effort.
    push_decision_to_close(
        close_lead_id="lead_abc",
        decision_id=uuid4(),
        score=Decimal("70"),
        recommendation="approve",
        ofac_status="Clear",
        client=_make_client(transport),
        audit=audit,
        now=_FIXED_NOW,
        merchant=_merchant(credit_score=None),
    )
    failed = [e for e in audit.entries if e["action"] == "close.task.credit_score_request_failed"]
    assert len(failed) == 1
    assert failed[0]["details"]["status_code"] == 400
    # And no success audit row written.
    assert not any(e["action"] == "close.task.credit_score_requested" for e in audit.entries)
