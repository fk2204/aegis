"""Tests for ``aegis.close.sync.push_offer_to_opportunity``.

Mirrors ``tests/test_close_sync.py``'s pattern (canned Lead-style
payload, ``httpx.MockTransport`` adapter, ``InMemoryAuditLog``):

* All seven fields PATCHed on a fresh opportunity.
* No-diff redelivery does NOT PATCH; audit row says ``patched=False``.
* Subset push (some inputs ``None``) only diffs the defined fields.
* Single-field change → PATCH that field alone, none of the others.
* Opportunity not found (404) returns ``SyncResult(reason="opportunity_not_found")``
  without raising; audit captures the failure.
* PATCH-side failure (4xx other than 404, 5xx) audits + re-raises.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest

from aegis.audit import InMemoryAuditLog
from aegis.close.client import CloseClient, CloseError
from aegis.close.field_map import CLOSE_OPPORTUNITY_FIELD_IDS
from aegis.close.sync import push_offer_to_opportunity
from aegis.config import get_settings


def _set_close_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOSE_API_KEY", "api_test_close_key")
    monkeypatch.setenv("CLOSE_API_BASE", "https://api.close.example")
    get_settings.cache_clear()


def _cf_key(aegis_name: str) -> str:
    return f"custom.{CLOSE_OPPORTUNITY_FIELD_IDS[aegis_name]}"


def _opportunity_with_fields(**aegis_values: Any) -> dict[str, Any]:
    """Build a canned Close Opportunity payload populated with whatever
    AEGIS-side fields the test wants pre-set on Close."""
    opportunity: dict[str, Any] = {"id": "oppo_abc", "lead_id": "lead_abc"}
    for aegis_name, value in aegis_values.items():
        if value is not None:
            opportunity[_cf_key(aegis_name)] = value
    return opportunity


class _MockCloseTransport:
    """Same shape as ``test_close_sync._MockCloseTransport``: captures
    GET + PUT requests; returns canned responses."""

    def __init__(
        self,
        *,
        get_response: httpx.Response,
        put_response: httpx.Response | None = None,
    ) -> None:
        self._get = get_response
        self._put = put_response or httpx.Response(200, json={"id": "oppo_abc"})
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET":
            return self._get
        return self._put


def _client_with_transport(transport: _MockCloseTransport) -> CloseClient:
    """Construct a CloseClient bound to the mock transport."""
    http_client = httpx.Client(transport=httpx.MockTransport(transport))
    return CloseClient(http_client=http_client)


def _put_request(transport: _MockCloseTransport) -> httpx.Request:
    """Pull the single PUT request out of the recorded transport.
    Fails the test if there isn't exactly one."""
    puts = [r for r in transport.requests if r.method == "PUT"]
    assert len(puts) == 1, f"expected 1 PUT, got {len(puts)}"
    return puts[0]


# ─────────────────────────────────────────────────────────────────────
# Case 1 — all seven fields PATCHed on a fresh opportunity
# ─────────────────────────────────────────────────────────────────────


def test_first_push_patches_all_defined_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    transport = _MockCloseTransport(
        get_response=httpx.Response(200, json=_opportunity_with_fields()),
    )
    client = _client_with_transport(transport)
    audit = InMemoryAuditLog()

    result = push_offer_to_opportunity(
        close_opportunity_id="oppo_abc",
        decision_id=uuid4(),
        suggested_max_advance=Decimal("50000.00"),
        recommended_factor_rate=Decimal("1.180"),
        recommended_holdback_pct=Decimal("0.1500"),
        true_revenue_monthly=Decimal("30000.00"),
        holdback_capacity_monthly=Decimal("7500.00"),
        existing_mca_count=2,
        existing_mca_daily_total=Decimal("250.00"),
        client=client,
        audit=audit,
    )
    assert result.patched is True
    assert result.reason == "patched"
    assert set(result.fields_diffed) == {
        "suggested_max_advance",
        "recommended_factor_rate",
        "recommended_holdback_pct",
        "true_revenue",
        "holdback_capacity",
        "existing_mca_count",
        "existing_mca_daily_total",
    }

    body = json.loads(_put_request(transport).content)
    # Each defined field maps to its CLOSE_OPPORTUNITY_FIELD_IDS cf_id.
    assert body[_cf_key("suggested_max_advance")] == "50000.00"
    assert body[_cf_key("recommended_factor_rate")] == "1.180"
    assert body[_cf_key("recommended_holdback_pct")] == "0.1500"
    assert body[_cf_key("true_revenue")] == "30000.00"
    assert body[_cf_key("holdback_capacity")] == "7500.00"
    assert body[_cf_key("existing_mca_count")] == 2
    assert body[_cf_key("existing_mca_daily_total")] == "250.00"

    rows = audit.list_recent(limit=50)
    assert any(
        r["action"] == "close.opportunity.sync_attempted" and r["details"]["patched"] is True
        for r in rows
    )


# ─────────────────────────────────────────────────────────────────────
# Case 2 — no-diff redelivery does NOT PATCH
# ─────────────────────────────────────────────────────────────────────


def test_no_diff_redelivery_does_not_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    pre_existing = _opportunity_with_fields(
        suggested_max_advance="50000.00",
        recommended_factor_rate="1.180",
        recommended_holdback_pct="0.1500",
        true_revenue="30000.00",
        holdback_capacity="7500.00",
        existing_mca_count=2,
        existing_mca_daily_total="250.00",
    )
    transport = _MockCloseTransport(
        get_response=httpx.Response(200, json=pre_existing),
    )
    client = _client_with_transport(transport)
    audit = InMemoryAuditLog()

    result = push_offer_to_opportunity(
        close_opportunity_id="oppo_abc",
        decision_id=uuid4(),
        suggested_max_advance=Decimal("50000.00"),
        recommended_factor_rate=Decimal("1.180"),
        recommended_holdback_pct=Decimal("0.1500"),
        true_revenue_monthly=Decimal("30000.00"),
        holdback_capacity_monthly=Decimal("7500.00"),
        existing_mca_count=2,
        existing_mca_daily_total=Decimal("250.00"),
        client=client,
        audit=audit,
    )
    assert result.patched is False
    assert result.reason == "no_diff"
    assert result.fields_diffed == []

    # No PUT fired.
    puts = [r for r in transport.requests if r.method == "PUT"]
    assert puts == []
    # Audit row still captures the attempt.
    rows = audit.list_recent(limit=50)
    assert any(
        r["action"] == "close.opportunity.sync_attempted" and r["details"]["patched"] is False
        for r in rows
    )


# ─────────────────────────────────────────────────────────────────────
# Case 3 — subset push: None inputs are skipped (not pushed)
# ─────────────────────────────────────────────────────────────────────


def test_none_inputs_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """``recommended_factor_rate=None`` (the v1 deferral) must not
    PATCH anything onto Close; the other six fields still flow."""
    _set_close_env(monkeypatch)
    transport = _MockCloseTransport(
        get_response=httpx.Response(200, json=_opportunity_with_fields()),
    )
    client = _client_with_transport(transport)
    audit = InMemoryAuditLog()

    result = push_offer_to_opportunity(
        close_opportunity_id="oppo_abc",
        decision_id=uuid4(),
        suggested_max_advance=Decimal("50000.00"),
        recommended_factor_rate=None,  # v1 deferred — must not appear in PATCH
        recommended_holdback_pct=Decimal("0.1500"),
        true_revenue_monthly=Decimal("30000.00"),
        holdback_capacity_monthly=Decimal("7500.00"),
        existing_mca_count=2,
        existing_mca_daily_total=Decimal("250.00"),
        client=client,
        audit=audit,
    )
    assert result.patched is True
    assert "recommended_factor_rate" not in result.fields_diffed
    body = json.loads(_put_request(transport).content)
    assert _cf_key("recommended_factor_rate") not in body
    # The other six fields landed.
    for name in [
        "suggested_max_advance",
        "recommended_holdback_pct",
        "true_revenue",
        "holdback_capacity",
        "existing_mca_count",
        "existing_mca_daily_total",
    ]:
        assert _cf_key(name) in body


# ─────────────────────────────────────────────────────────────────────
# Case 4 — single-field change PATCHes only that field
# ─────────────────────────────────────────────────────────────────────


def test_single_field_change_patches_only_that_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    # Everything but existing_mca_count already matches.
    pre_existing = _opportunity_with_fields(
        suggested_max_advance="50000.00",
        recommended_factor_rate="1.180",
        recommended_holdback_pct="0.1500",
        true_revenue="30000.00",
        holdback_capacity="7500.00",
        existing_mca_count=1,  # Close says 1, AEGIS now says 2
        existing_mca_daily_total="250.00",
    )
    transport = _MockCloseTransport(
        get_response=httpx.Response(200, json=pre_existing),
    )
    client = _client_with_transport(transport)
    audit = InMemoryAuditLog()

    result = push_offer_to_opportunity(
        close_opportunity_id="oppo_abc",
        decision_id=uuid4(),
        suggested_max_advance=Decimal("50000.00"),
        recommended_factor_rate=Decimal("1.180"),
        recommended_holdback_pct=Decimal("0.1500"),
        true_revenue_monthly=Decimal("30000.00"),
        holdback_capacity_monthly=Decimal("7500.00"),
        existing_mca_count=2,  # Only this differs.
        existing_mca_daily_total=Decimal("250.00"),
        client=client,
        audit=audit,
    )
    assert result.patched is True
    assert result.fields_diffed == ["existing_mca_count"]
    body = json.loads(_put_request(transport).content)
    assert body == {_cf_key("existing_mca_count"): 2}


# ─────────────────────────────────────────────────────────────────────
# Case 5 — opportunity 404 returns gracefully
# ─────────────────────────────────────────────────────────────────────


def test_opportunity_not_found_returns_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    transport = _MockCloseTransport(
        get_response=httpx.Response(404, json={"error": "not found"}),
    )
    client = _client_with_transport(transport)
    audit = InMemoryAuditLog()

    result = push_offer_to_opportunity(
        close_opportunity_id="oppo_missing",
        decision_id=uuid4(),
        suggested_max_advance=Decimal("50000.00"),
        recommended_factor_rate=None,
        recommended_holdback_pct=Decimal("0.1500"),
        true_revenue_monthly=Decimal("30000.00"),
        holdback_capacity_monthly=Decimal("7500.00"),
        existing_mca_count=2,
        existing_mca_daily_total=Decimal("250.00"),
        client=client,
        audit=audit,
    )
    assert result.patched is False
    assert result.reason == "opportunity_not_found"
    assert result.fields_diffed == []

    # 404 audit row was written.
    rows = audit.list_recent(limit=50)
    assert any(
        r["action"] == "close.opportunity.sync_failed_not_found"
        and r["details"]["status_code"] == 404
        for r in rows
    )


# ─────────────────────────────────────────────────────────────────────
# Case 6 — PATCH failure (5xx) audits + re-raises
# ─────────────────────────────────────────────────────────────────────


def test_patch_failure_audits_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    transport = _MockCloseTransport(
        get_response=httpx.Response(200, json=_opportunity_with_fields()),
        put_response=httpx.Response(500, json={"error": "server error"}),
    )
    # Disable retry so the 500 surfaces immediately.
    monkeypatch.setenv("CLOSE_MAX_RETRIES", "0")
    get_settings.cache_clear()
    client = _client_with_transport(transport)
    audit = InMemoryAuditLog()

    with pytest.raises(CloseError):
        push_offer_to_opportunity(
            close_opportunity_id="oppo_abc",
            decision_id=uuid4(),
            suggested_max_advance=Decimal("50000.00"),
            recommended_factor_rate=Decimal("1.180"),
            recommended_holdback_pct=Decimal("0.1500"),
            true_revenue_monthly=Decimal("30000.00"),
            holdback_capacity_monthly=Decimal("7500.00"),
            existing_mca_count=2,
            existing_mca_daily_total=Decimal("250.00"),
            client=client,
            audit=audit,
        )

    rows = audit.list_recent(limit=50)
    failures = [
        r
        for r in rows
        if r["action"] == "close.opportunity.sync_attempted"
        and r["details"].get("patched") is False
        and r["details"].get("error_status") == 500
    ]
    assert len(failures) == 1
