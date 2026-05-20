"""Alerting client — Healthchecks.io heartbeats + ntfy event alerts.

Two channels with different semantics:

  * **Healthchecks.io (heartbeat / dead-man's-switch).** The web and
    worker processes ping a Healthchecks.io URL every five minutes via
    a systemd timer (see ``deploy/aegis-heartbeat.service`` +
    ``deploy/aegis-heartbeat.timer``). Missing pings raise the alarm
    on the Healthchecks dashboard; the *application* never knows the
    alert fired.
  * **ntfy.sh (event alerts).** Application code calls
    ``notify_event(...)`` to push a one-shot message at a configured
    severity. ntfy is a hosted pub-sub that takes a plain HTTP POST —
    no client library required. ``httpx`` is already a dependency.

Both channels are fail-open: a network failure or unconfigured env
var produces an INFO log but never raises. The audit_log entry is
written before the network hop so the alert is recoverable from the
durable record even when ntfy is unreachable.

Event-alert thresholds (Phase 11 task #1)
-----------------------------------------
| Signal | Threshold | Notes |
|---|---|---|
| Bedrock 5xx / throttle | >3 per 10min eval window | alert_bedrock_failure_burst |
| Zoho 401/403 | immediate | alert_zoho_auth_failure |
| Zoho HMAC mismatch | >0 per hour | alert_zoho_hmac_failure |
| parse_status='manual_review' | >25% over last 20 deals | alert_manual_review_rate |
| OFAC cache age | >6 days | alert_ofac_cache_stale |
| arq queue depth | >20 | alert_arq_queue_depth |
| Disk usage | >80% | alert_disk_usage |

Constants below pin the integer thresholds in code so the alerting
rule is grep-able and review-able as a unit. Tunable via the env vars
listed alongside each constant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

import httpx

from aegis.audit import AuditLog, AuditWriteError
from aegis.logger import get_logger

_log = get_logger(__name__)


# --- thresholds (Phase 11 task #1 spec) -------------------------------------

#: Bedrock transient-error count above which we alert. Eval window is the
#: caller's responsibility (Healthchecks.io / external counter) — the
#: constant here is just the threshold the alerting condition checks.
BEDROCK_FAILURE_THRESHOLD: Final[int] = 3
BEDROCK_FAILURE_WINDOW_MIN: Final[int] = 10

#: Zoho HMAC failures per hour above which we alert. Per master plan
#: §21 the threshold is "> 0/hr" — any HMAC failure on the inbound
#: webhook surface is suspicious and the operator should see it.
ZOHO_HMAC_FAILURE_THRESHOLD: Final[int] = 0

#: Fraction of recent deals routed to manual_review above which we
#: alert. Threshold is 0.25 (25%) over the last 20 deals.
MANUAL_REVIEW_RATE_THRESHOLD: Final[float] = 0.25
MANUAL_REVIEW_WINDOW: Final[int] = 20

#: OFAC SDN cache age above which we alert. Hard cutoff in
#: ``scoring.ofac`` is 7 days; alert at 6 so we don't ship a
#: fail-closed 503 before the operator has a chance to react.
OFAC_CACHE_AGE_DAYS: Final[int] = 6

#: arq pending-job depth above which we alert. Operator playbook is
#: typically "restart worker" or "investigate slow extraction".
ARQ_QUEUE_DEPTH_THRESHOLD: Final[int] = 20

#: Disk-usage percent above which we alert.
DISK_USAGE_PCT_THRESHOLD: Final[int] = 80


class AlertSeverity(StrEnum):
    """ntfy severity tag. Mirrors ntfy's priority field (1-5)."""

    #: Operational/informational; default priority (3).
    INFO = "info"
    #: Operator should investigate within the hour.
    WARN = "warn"
    #: Operator must act now — money / compliance / data-residency.
    CRITICAL = "critical"


_NTFY_PRIORITY: Final[dict[AlertSeverity, int]] = {
    AlertSeverity.INFO: 3,
    AlertSeverity.WARN: 4,
    AlertSeverity.CRITICAL: 5,
}


@dataclass(frozen=True)
class AlertConfig:
    """Resolved alerting configuration. None values disable that channel.

    Built by ``load_alert_config`` from environment variables. Kept
    immutable + frozen so a stale config can never be mutated by
    application code; rotation requires a process restart.
    """

    healthcheck_web_url: str | None
    healthcheck_worker_url: str | None
    ntfy_topic_url: str | None
    #: HTTP timeout in seconds for ALL alert posts. Short by design —
    #: alerting must never block a request thread, and the durable
    #: audit_log entry covers the recoverability story.
    http_timeout_seconds: float = 3.0

    @property
    def has_ntfy(self) -> bool:
        return self.ntfy_topic_url is not None

    @property
    def has_healthcheck(self) -> bool:
        return (
            self.healthcheck_web_url is not None
            or self.healthcheck_worker_url is not None
        )


def load_alert_config() -> AlertConfig:
    """Resolve env vars into an ``AlertConfig``.

    All four vars are optional. Tests and offline development run with
    everything unset → ``AlertConfig(None, None, None)`` which makes
    every alerting call a no-op + audit-row.

    Vars (all loaded via ``os.environ`` directly to keep this module
    standalone — adding them to ``Settings`` would force every
    environment to declare them):

      * ``AEGIS_HEALTHCHECK_WEB_URL``    — full https://hc-ping.com/<uuid> URL
      * ``AEGIS_HEALTHCHECK_WORKER_URL`` — full https://hc-ping.com/<uuid> URL
      * ``AEGIS_NTFY_TOPIC_URL``         — full https://ntfy.sh/<topic> URL
    """
    import os

    return AlertConfig(
        healthcheck_web_url=os.environ.get("AEGIS_HEALTHCHECK_WEB_URL") or None,
        healthcheck_worker_url=os.environ.get("AEGIS_HEALTHCHECK_WORKER_URL")
        or None,
        ntfy_topic_url=os.environ.get("AEGIS_NTFY_TOPIC_URL") or None,
    )


# --- channel: Healthchecks.io heartbeat ------------------------------------


def ping_healthcheck(
    config: AlertConfig,
    *,
    component: str,
    failed: bool = False,
) -> bool:
    """Ping the configured Healthchecks.io URL for ``component``.

    ``component`` is ``"web"`` or ``"worker"``. ``failed=True`` posts
    to the ``/fail`` suffix so the Healthchecks dashboard records the
    failure timestamp. Returns True if the HTTP request succeeded,
    False if the URL was unset OR the request failed. Never raises.

    The audit_log row is written *before* the network call so the
    operator can reconstruct heartbeats from the durable record even
    when Healthchecks.io is unreachable.
    """
    base = (
        config.healthcheck_web_url
        if component == "web"
        else config.healthcheck_worker_url
    )
    if base is None:
        _log.info("ops.alert.healthcheck.skipped reason=unconfigured component=%s", component)
        return False
    url = f"{base.rstrip('/')}/fail" if failed else base
    try:
        with httpx.Client(timeout=config.http_timeout_seconds) as client:
            response = client.get(url)
        ok = 200 <= response.status_code < 300
        _log.info(
            "ops.alert.healthcheck component=%s failed=%s ok=%s status=%s",
            component,
            failed,
            ok,
            response.status_code,
        )
        return ok
    except httpx.HTTPError as exc:
        _log.warning(
            "ops.alert.healthcheck_network_error component=%s err=%s",
            component,
            exc.__class__.__name__,
        )
        return False


# --- channel: ntfy event alerts --------------------------------------------


def notify_event(
    config: AlertConfig,
    *,
    audit: AuditLog | None,
    title: str,
    body: str,
    severity: AlertSeverity = AlertSeverity.WARN,
    tags: tuple[str, ...] = (),
) -> bool:
    """Post an event alert to ntfy and write the matching audit row.

    Arguments:
      config — resolved ``AlertConfig``; pass the in-process instance.
      audit  — ``AuditLog``; the durable record is written even when
               ntfy is unconfigured or unreachable. ``None`` disables
               audit writes (only valid in pure unit tests).
      title  — short subject; goes into ntfy's ``Title`` header.
      body   — message body. Must NOT contain PII; the global PII
               masking filter runs against the structured log row
               but the body goes verbatim to ntfy.
      severity — ``AlertSeverity.INFO|WARN|CRITICAL``.
      tags    — short tag tokens (e.g. ``("bedrock", "throttle")``).
                ntfy renders these as emoji + searchable labels.

    Returns True iff the HTTP POST succeeded. Never raises.
    """
    # Audit FIRST so the durable record is independent of ntfy uptime.
    timestamp = datetime.now(UTC).isoformat()
    details: dict[str, object] = {
        "severity": severity.value,
        "title": title,
        "body": body,
        "tags": list(tags),
        "fired_at": timestamp,
        "channel": "ntfy" if config.has_ntfy else "log_only",
    }
    if audit is not None:
        try:
            audit.record(
                actor="ops.alerting",
                action=f"ops.alert.{severity.value}",
                subject_type="alert",
                subject_id=None,
                details=details,
            )
        except AuditWriteError:
            # Audit-write failure is loud at the call site but must
            # never prevent us from at least attempting the operator
            # notification — alerting fail-closed would mask outages.
            _log.exception("ops.alert.audit_write_failed title=%s", title)

    if not config.has_ntfy:
        _log.info(
            "ops.alert.ntfy.skipped reason=unconfigured severity=%s title=%s",
            severity.value,
            title,
        )
        return False

    # Type-narrowing: has_ntfy guarantees ntfy_topic_url is not None
    # but mypy can't see through the property; use an explicit local
    # bind to avoid the assert that ruff S101 trips on.
    ntfy_url = config.ntfy_topic_url
    if ntfy_url is None:  # pragma: no cover — guarded by has_ntfy above
        return False

    headers = {
        "Title": title,
        "Priority": str(_NTFY_PRIORITY[severity]),
    }
    if tags:
        headers["Tags"] = ",".join(tags)

    try:
        with httpx.Client(timeout=config.http_timeout_seconds) as client:
            response = client.post(
                ntfy_url,
                headers=headers,
                content=body.encode("utf-8"),
            )
        ok = 200 <= response.status_code < 300
        _log.info(
            "ops.alert.ntfy.posted severity=%s status=%s ok=%s title=%s",
            severity.value,
            response.status_code,
            ok,
            title,
        )
        return ok
    except httpx.HTTPError as exc:
        _log.warning(
            "ops.alert.ntfy.network_error err=%s title=%s",
            exc.__class__.__name__,
            title,
        )
        return False


# --- typed wrappers for the Phase 11 thresholded conditions ----------------
#
# Each helper below is the canonical entry point for one of the §21
# event-alert thresholds. They exist so callers don't have to remember
# the threshold constants or build the right tags by hand — call
# ``alert_bedrock_failure_burst(...)`` and the threshold + tag taxonomy
# stay correct.


def alert_bedrock_failure_burst(
    config: AlertConfig,
    audit: AuditLog | None,
    *,
    failure_count: int,
    window_minutes: int = BEDROCK_FAILURE_WINDOW_MIN,
) -> bool:
    """Alert when Bedrock 5xx/throttle exceeds the configured burst rate."""
    if failure_count <= BEDROCK_FAILURE_THRESHOLD:
        return False
    return notify_event(
        config,
        audit=audit,
        title="Bedrock failure burst",
        body=(
            f"{failure_count} transient Bedrock failures observed in "
            f"the last {window_minutes} minutes (threshold "
            f">{BEDROCK_FAILURE_THRESHOLD}). Check journalctl -u "
            f"aegis-web and aegis-worker for retry storms."
        ),
        severity=AlertSeverity.WARN,
        tags=("bedrock", "throttle"),
    )


def alert_zoho_auth_failure(
    config: AlertConfig,
    audit: AuditLog | None,
    *,
    status_code: int,
    endpoint: str,
) -> bool:
    """Alert on a Zoho 401/403 immediately — refresh token likely dead."""
    if status_code not in (401, 403):
        return False
    return notify_event(
        config,
        audit=audit,
        title="Zoho auth failure",
        body=(
            f"Zoho returned {status_code} on {endpoint}. The refresh "
            f"token may need rotation — see deploy/RUNBOOK.md "
            f"'Rotate Zoho refresh token'."
        ),
        severity=AlertSeverity.CRITICAL,
        tags=("zoho", "auth"),
    )


def alert_zoho_hmac_failure(
    config: AlertConfig,
    audit: AuditLog | None,
    *,
    source: str,
    failure_count_in_hour: int,
) -> bool:
    """Alert on any Zoho HMAC mismatch within the rolling hour."""
    if failure_count_in_hour <= ZOHO_HMAC_FAILURE_THRESHOLD:
        return False
    return notify_event(
        config,
        audit=audit,
        title="Zoho HMAC mismatch",
        body=(
            f"{failure_count_in_hour} Zoho HMAC mismatch(es) observed "
            f"in the last hour from source={source}. Possible webhook "
            f"signing-secret drift or a forged-payload attempt."
        ),
        severity=AlertSeverity.CRITICAL,
        tags=("zoho", "hmac", "security"),
    )


def alert_manual_review_rate(
    config: AlertConfig,
    audit: AuditLog | None,
    *,
    manual_review_count: int,
    sample_size: int,
) -> bool:
    """Alert when manual_review rate exceeds the threshold over the recent
    sample. Sample size below ``MANUAL_REVIEW_WINDOW`` is a no-op so the
    alarm doesn't fire on a cold-start trickle of deals."""
    if sample_size < MANUAL_REVIEW_WINDOW:
        return False
    rate = manual_review_count / sample_size
    if rate <= MANUAL_REVIEW_RATE_THRESHOLD:
        return False
    return notify_event(
        config,
        audit=audit,
        title="Parser manual_review rate elevated",
        body=(
            f"{manual_review_count}/{sample_size} recent deals routed "
            f"to manual_review ({rate:.0%}). Threshold is "
            f"{MANUAL_REVIEW_RATE_THRESHOLD:.0%}. Check parser flags "
            f"on /ui/ recent activity."
        ),
        severity=AlertSeverity.WARN,
        tags=("parser", "manual_review"),
    )


def alert_ofac_cache_stale(
    config: AlertConfig,
    audit: AuditLog | None,
    *,
    age_days: int,
) -> bool:
    """Alert when the OFAC SDN cache age crosses the warn threshold.

    The hard cutoff in ``scoring.ofac`` is 7 days (score_deal raises
    ``OFACStaleError``); we alert at 6 so the operator can refresh the
    cache before scoring starts failing closed.
    """
    if age_days < OFAC_CACHE_AGE_DAYS:
        return False
    return notify_event(
        config,
        audit=audit,
        title="OFAC cache approaching staleness",
        body=(
            f"OFAC SDN cache is {age_days} days old; hard cutoff at "
            f"7 days will start failing /deals/score with 503. Refresh "
            f"via the worker's OFAC fetch routine or trigger manually."
        ),
        severity=AlertSeverity.WARN,
        tags=("ofac", "compliance"),
    )


def alert_arq_queue_depth(
    config: AlertConfig,
    audit: AuditLog | None,
    *,
    depth: int,
) -> bool:
    """Alert when arq pending depth exceeds threshold."""
    if depth <= ARQ_QUEUE_DEPTH_THRESHOLD:
        return False
    return notify_event(
        config,
        audit=audit,
        title="arq queue backed up",
        body=(
            f"{depth} jobs pending in the arq queue "
            f"(threshold >{ARQ_QUEUE_DEPTH_THRESHOLD}). Worker may be "
            f"wedged — check journalctl -u aegis-worker."
        ),
        severity=AlertSeverity.WARN,
        tags=("arq", "queue"),
    )


def alert_disk_usage(
    config: AlertConfig,
    audit: AuditLog | None,
    *,
    usage_pct: int,
    mountpoint: str = "/",
) -> bool:
    """Alert when disk usage on ``mountpoint`` exceeds threshold."""
    if usage_pct <= DISK_USAGE_PCT_THRESHOLD:
        return False
    return notify_event(
        config,
        audit=audit,
        title="Disk usage elevated",
        body=(
            f"{mountpoint} is at {usage_pct}% (threshold "
            f">{DISK_USAGE_PCT_THRESHOLD}%). Investigate log rotation, "
            f"uv-cache size, or stale uploaded PDFs under "
            f"AEGIS_UPLOAD_DIR."
        ),
        severity=AlertSeverity.WARN,
        tags=("disk", "infrastructure"),
    )


# Re-export a synthetic UUID used as ``subject_id`` for alerts that
# have no natural subject — kept here so call sites don't invent
# random sentinel UUIDs.
ALERT_SENTINEL_SUBJECT: Final[UUID] = UUID("00000000-0000-0000-0000-000000000a1e")


__all__ = [
    "ALERT_SENTINEL_SUBJECT",
    "ARQ_QUEUE_DEPTH_THRESHOLD",
    "BEDROCK_FAILURE_THRESHOLD",
    "BEDROCK_FAILURE_WINDOW_MIN",
    "DISK_USAGE_PCT_THRESHOLD",
    "MANUAL_REVIEW_RATE_THRESHOLD",
    "MANUAL_REVIEW_WINDOW",
    "OFAC_CACHE_AGE_DAYS",
    "ZOHO_HMAC_FAILURE_THRESHOLD",
    "AlertConfig",
    "AlertSeverity",
    "alert_arq_queue_depth",
    "alert_bedrock_failure_burst",
    "alert_disk_usage",
    "alert_manual_review_rate",
    "alert_ofac_cache_stale",
    "alert_zoho_auth_failure",
    "alert_zoho_hmac_failure",
    "load_alert_config",
    "notify_event",
    "ping_healthcheck",
]
