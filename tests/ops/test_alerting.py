"""Tests for the alerting module (mp Phase 11 task #1).

The alerting subsystem is fail-open by design — a failed HTTP post
must NEVER raise. These tests pin that contract plus verify each
threshold helper fires only above its configured boundary.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from aegis.audit import InMemoryAuditLog
from aegis.ops.alerting import (
    ARQ_QUEUE_DEPTH_THRESHOLD,
    BEDROCK_FAILURE_THRESHOLD,
    DISK_USAGE_PCT_THRESHOLD,
    MANUAL_REVIEW_WINDOW,
    OFAC_CACHE_AGE_DAYS,
    AlertConfig,
    AlertSeverity,
    alert_arq_queue_depth,
    alert_bedrock_failure_burst,
    alert_disk_usage,
    alert_manual_review_rate,
    alert_ofac_cache_stale,
    alert_zoho_auth_failure,
    alert_zoho_hmac_failure,
    load_alert_config,
    notify_event,
    ping_healthcheck,
)

# --- fixtures ---------------------------------------------------------------


class _CapturingTransport(httpx.BaseTransport):
    """httpx transport that records every request and returns a fixed status.

    Lets us assert that the alerting module actually constructed the
    right URL + headers without touching the network.
    """

    def __init__(self, status_code: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self._status = status_code

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self._status, request=request)


class _RaisingTransport(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated")


def _install_transport(
    monkeypatch: pytest.MonkeyPatch, transport: httpx.BaseTransport
) -> None:
    """Replace ``aegis.ops.alerting.httpx.Client`` with a transport-bound factory.

    We patch the module-local reference, NOT the global httpx.Client,
    so the constructor inside our factory still resolves to the real
    httpx.Client and we don't recurse.
    """
    real_client = httpx.Client

    def _factory(*_args: Any, **kwargs: Any) -> httpx.Client:
        return real_client(transport=transport, timeout=kwargs.get("timeout"))

    monkeypatch.setattr("aegis.ops.alerting.httpx.Client", _factory)


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> AlertConfig:
    """Return an AlertConfig with all three channels configured."""
    monkeypatch.setenv("AEGIS_HEALTHCHECK_WEB_URL", "https://hc-ping.com/web-uuid")
    monkeypatch.setenv(
        "AEGIS_HEALTHCHECK_WORKER_URL", "https://hc-ping.com/worker-uuid"
    )
    monkeypatch.setenv("AEGIS_NTFY_TOPIC_URL", "https://ntfy.sh/aegis-test")
    return load_alert_config()


@pytest.fixture
def unconfigured(monkeypatch: pytest.MonkeyPatch) -> AlertConfig:
    """Return an AlertConfig with every channel disabled."""
    monkeypatch.delenv("AEGIS_HEALTHCHECK_WEB_URL", raising=False)
    monkeypatch.delenv("AEGIS_HEALTHCHECK_WORKER_URL", raising=False)
    monkeypatch.delenv("AEGIS_NTFY_TOPIC_URL", raising=False)
    return load_alert_config()


# --- load_alert_config ------------------------------------------------------


def test_load_alert_config_resolves_env(configured: AlertConfig) -> None:
    assert configured.healthcheck_web_url == "https://hc-ping.com/web-uuid"
    assert configured.healthcheck_worker_url == "https://hc-ping.com/worker-uuid"
    assert configured.ntfy_topic_url == "https://ntfy.sh/aegis-test"
    assert configured.has_ntfy is True
    assert configured.has_healthcheck is True


def test_load_alert_config_default_unconfigured(unconfigured: AlertConfig) -> None:
    assert unconfigured.healthcheck_web_url is None
    assert unconfigured.healthcheck_worker_url is None
    assert unconfigured.ntfy_topic_url is None
    assert unconfigured.has_ntfy is False
    assert unconfigured.has_healthcheck is False


# --- ping_healthcheck --------------------------------------------------------


def test_ping_healthcheck_unconfigured_returns_false(
    unconfigured: AlertConfig,
) -> None:
    """No URL → no ping, but never raise."""
    assert ping_healthcheck(unconfigured, component="web") is False
    assert ping_healthcheck(unconfigured, component="worker") is False


def test_ping_healthcheck_success_uses_base_url(
    configured: AlertConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = _CapturingTransport(status_code=200)
    _install_transport(monkeypatch, transport)
    assert ping_healthcheck(configured, component="web") is True
    assert len(transport.requests) == 1
    assert str(transport.requests[0].url) == "https://hc-ping.com/web-uuid"


def test_ping_healthcheck_failure_uses_fail_suffix(
    configured: AlertConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = _CapturingTransport(status_code=200)
    _install_transport(monkeypatch, transport)
    ping_healthcheck(configured, component="worker", failed=True)
    assert str(transport.requests[0].url) == "https://hc-ping.com/worker-uuid/fail"


def test_ping_healthcheck_network_error_does_not_raise(
    configured: AlertConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A network failure must return False, never propagate the exception."""
    _install_transport(monkeypatch, _RaisingTransport())
    assert ping_healthcheck(configured, component="web") is False


# --- notify_event ----------------------------------------------------------


def test_notify_event_writes_audit_before_network(
    configured: AlertConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The durable record must be written even when the POST fails.

    Most important fail-open property: a future operator must be able
    to reconstruct alerts from audit_log after an ntfy outage, so the
    audit row has to land BEFORE the network hop.
    """
    _install_transport(monkeypatch, _RaisingTransport())
    audit = InMemoryAuditLog()
    assert (
        notify_event(
            configured,
            audit=audit,
            title="t",
            body="b",
            severity=AlertSeverity.WARN,
            tags=("test",),
        )
        is False
    )
    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert entry["action"] == "ops.alert.warn"
    assert entry["details"]["title"] == "t"
    assert entry["details"]["tags"] == ["test"]


def test_notify_event_posts_title_priority_tags(
    configured: AlertConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = _CapturingTransport(status_code=200)
    _install_transport(monkeypatch, transport)
    audit = InMemoryAuditLog()
    notify_event(
        configured,
        audit=audit,
        title="incident",
        body="body",
        severity=AlertSeverity.CRITICAL,
        tags=("zoho", "auth"),
    )
    assert len(transport.requests) == 1
    req = transport.requests[0]
    assert str(req.url) == "https://ntfy.sh/aegis-test"
    assert req.headers["Title"] == "incident"
    assert req.headers["Priority"] == "5"
    assert req.headers["Tags"] == "zoho,auth"
    assert req.content == b"body"


def test_notify_event_unconfigured_still_audits(unconfigured: AlertConfig) -> None:
    audit = InMemoryAuditLog()
    assert (
        notify_event(
            unconfigured,
            audit=audit,
            title="t",
            body="b",
            severity=AlertSeverity.INFO,
        )
        is False
    )
    assert len(audit.entries) == 1
    assert audit.entries[0]["details"]["channel"] == "log_only"


# --- threshold helpers ------------------------------------------------------


def test_alert_bedrock_failure_below_threshold_no_op(
    configured: AlertConfig,
) -> None:
    audit = InMemoryAuditLog()
    fired = alert_bedrock_failure_burst(
        configured, audit, failure_count=BEDROCK_FAILURE_THRESHOLD
    )
    assert fired is False
    assert audit.entries == []


def test_alert_bedrock_failure_above_threshold_fires(
    configured: AlertConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_transport(monkeypatch, _CapturingTransport(status_code=200))
    audit = InMemoryAuditLog()
    assert (
        alert_bedrock_failure_burst(
            configured, audit, failure_count=BEDROCK_FAILURE_THRESHOLD + 1
        )
        is True
    )
    assert len(audit.entries) == 1
    assert "bedrock" in audit.entries[0]["details"]["tags"]


@pytest.mark.parametrize("status", [401, 403])
def test_alert_zoho_auth_fires_on_401_403(
    configured: AlertConfig, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    _install_transport(monkeypatch, _CapturingTransport(status_code=200))
    audit = InMemoryAuditLog()
    assert (
        alert_zoho_auth_failure(
            configured, audit, status_code=status, endpoint="/crm/v3/Leads"
        )
        is True
    )
    assert audit.entries[0]["details"]["severity"] == "critical"


def test_alert_zoho_auth_ignores_other_status(configured: AlertConfig) -> None:
    """A 500 is not an auth failure — different alert path."""
    audit = InMemoryAuditLog()
    assert (
        alert_zoho_auth_failure(
            configured, audit, status_code=500, endpoint="/crm/v3/Leads"
        )
        is False
    )
    assert audit.entries == []


def test_alert_zoho_hmac_fires_on_any_failure(
    configured: AlertConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_transport(monkeypatch, _CapturingTransport(status_code=200))
    audit = InMemoryAuditLog()
    assert (
        alert_zoho_hmac_failure(
            configured, audit, source="zoho-webhook", failure_count_in_hour=1
        )
        is True
    )


def test_alert_manual_review_below_window_no_op(configured: AlertConfig) -> None:
    """Sample size below the window must NOT fire — cold-start protection."""
    audit = InMemoryAuditLog()
    assert (
        alert_manual_review_rate(
            configured,
            audit,
            manual_review_count=5,
            sample_size=MANUAL_REVIEW_WINDOW - 1,
        )
        is False
    )


def test_alert_manual_review_above_threshold_fires(
    configured: AlertConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_transport(monkeypatch, _CapturingTransport(status_code=200))
    audit = InMemoryAuditLog()
    # threshold is 0.25 over 20 → 6/20 = 0.30 fires
    assert (
        alert_manual_review_rate(
            configured, audit, manual_review_count=6, sample_size=20
        )
        is True
    )


def test_alert_manual_review_at_threshold_does_not_fire(
    configured: AlertConfig,
) -> None:
    audit = InMemoryAuditLog()
    # 5/20 = 0.25 == threshold; helper uses `<=` so equal does NOT fire
    assert (
        alert_manual_review_rate(
            configured, audit, manual_review_count=5, sample_size=20
        )
        is False
    )
    assert audit.entries == []


def test_alert_ofac_cache_stale(
    configured: AlertConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_transport(monkeypatch, _CapturingTransport(status_code=200))
    audit = InMemoryAuditLog()
    assert (
        alert_ofac_cache_stale(configured, audit, age_days=OFAC_CACHE_AGE_DAYS - 1)
        is False
    )
    assert (
        alert_ofac_cache_stale(configured, audit, age_days=OFAC_CACHE_AGE_DAYS) is True
    )


def test_alert_arq_queue_depth(
    configured: AlertConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_transport(monkeypatch, _CapturingTransport(status_code=200))
    audit = InMemoryAuditLog()
    assert (
        alert_arq_queue_depth(configured, audit, depth=ARQ_QUEUE_DEPTH_THRESHOLD)
        is False
    )
    assert (
        alert_arq_queue_depth(configured, audit, depth=ARQ_QUEUE_DEPTH_THRESHOLD + 1)
        is True
    )


def test_alert_disk_usage(
    configured: AlertConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_transport(monkeypatch, _CapturingTransport(status_code=200))
    audit = InMemoryAuditLog()
    assert (
        alert_disk_usage(configured, audit, usage_pct=DISK_USAGE_PCT_THRESHOLD)
        is False
    )
    assert (
        alert_disk_usage(configured, audit, usage_pct=DISK_USAGE_PCT_THRESHOLD + 1)
        is True
    )
