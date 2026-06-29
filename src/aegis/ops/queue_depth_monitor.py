"""Redis queue depth monitor — alert when the arq pending queue is backed up.

Fired every 5 minutes by ``deploy/aegis-queue-monitor.timer``. Reads the
length of the arq pending list in Redis; if it exceeds the configured
threshold, writes one ``system.queue_depth_alert`` row to ``audit_log``
AND logs to stderr at WARNING level (journald captures both). When the
depth is at or below threshold, the run is a silent no-op — an audit row
on every healthy tick would be noise, not signal (build plan §10.2).

Threshold defaults to 20 (matching ``ARQ_QUEUE_DEPTH_THRESHOLD`` in
``aegis.ops.alerting``) and is overridable via the env var
``AEGIS_QUEUE_DEPTH_ALERT_THRESHOLD``. Override is read at process start
— a tuning change requires either rebooting the box or letting the
5-minute timer cycle around to the next fire, which is fine for an ops
knob that's tuned once and then forgotten.

Exit code is always 0. The audit row + WARNING log line is the alert
signal; failing the unit would noise up ``systemctl --failed`` on
transient Redis blips, which is the opposite of the intent (the monitor
should fail quiet on its own infrastructure and let the heartbeat
timers + Healthchecks dead-man's-switch surface "Redis is down" via
the existing aegis-heartbeat-worker.service path).

When Redis itself is unreachable, the monitor writes one
``system.queue_monitor_error`` audit row (so the operator has a durable
record of the monitor's own outage) and still exits 0.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Final

from arq import create_pool
from arq.constants import default_queue_name
from redis.exceptions import RedisError

from aegis.audit import AuditLog, AuditWriteError
from aegis.logger import configure_logging, get_logger
from aegis.ops.alerting import ARQ_QUEUE_DEPTH_THRESHOLD
from aegis.workers import build_redis_settings

_log = get_logger(__name__)

#: Env-var name for tuning the alert threshold without a code change.
#: Default matches ``ARQ_QUEUE_DEPTH_THRESHOLD`` (20) so the monitor and
#: the in-process ``alert_arq_queue_depth`` helper stay in lockstep.
QUEUE_DEPTH_THRESHOLD_ENV: Final[str] = "AEGIS_QUEUE_DEPTH_ALERT_THRESHOLD"

_ACTION_ALERT: Final[str] = "system.queue_depth_alert"
_ACTION_ERROR: Final[str] = "system.queue_monitor_error"
_ACTOR: Final[str] = "system:queue_monitor"


def _resolve_threshold() -> int:
    """Read the threshold from env; fall back to the alerting default.

    A non-int / negative override is logged and ignored (the default
    wins) — the monitor must never fail closed on a malformed knob.
    """
    raw = os.environ.get(QUEUE_DEPTH_THRESHOLD_ENV)
    if raw is None or raw.strip() == "":
        return ARQ_QUEUE_DEPTH_THRESHOLD
    try:
        value = int(raw)
    except ValueError:
        _log.warning(
            "ops.queue_monitor.threshold_invalid raw=%r default=%d",
            raw,
            ARQ_QUEUE_DEPTH_THRESHOLD,
        )
        return ARQ_QUEUE_DEPTH_THRESHOLD
    if value < 0:
        _log.warning(
            "ops.queue_monitor.threshold_negative raw=%d default=%d",
            value,
            ARQ_QUEUE_DEPTH_THRESHOLD,
        )
        return ARQ_QUEUE_DEPTH_THRESHOLD
    return value


async def get_queue_depth_for_ui() -> tuple[int | None, int]:
    """Return ``(depth, threshold)`` for the dashboard alert banner.

    Returns ``(None, threshold)`` when Redis is unreachable so the
    template renders no banner (defensive — a Redis outage shouldn't
    take the dashboard down with it). The threshold is the live value
    from ``_resolve_threshold`` so an operator env-var change is
    picked up on the next request without restarting the web service.
    """
    threshold = _resolve_threshold()
    try:
        depth = await _measure_queue_depth()
    except Exception:
        return None, threshold
    return depth, threshold


async def _measure_queue_depth() -> int:
    """Connect to Redis, ``LLEN`` the arq pending queue, close the pool.

    Uses ``arq.create_pool`` with the same ``RedisSettings`` the worker
    is configured against — that's the only way to guarantee the
    monitor and the worker are looking at the same Redis instance and
    the same queue key. The queue key is sourced from
    ``arq.constants.default_queue_name`` so the literal string never
    appears in this file (avoids the documented "hardcode twice"
    risk).
    """
    pool = await create_pool(build_redis_settings())
    try:
        depth = await pool.llen(default_queue_name)
    finally:
        await pool.close(close_connection_pool=True)
    return int(depth)


def _record_alert(audit: AuditLog, *, depth: int, threshold: int) -> None:
    """Write the depth-alert audit row. Never raises."""
    details = {
        "queue_depth": depth,
        "threshold": threshold,
        "queue_name": default_queue_name,
    }
    try:
        audit.record(
            actor=_ACTOR,
            action=_ACTION_ALERT,
            subject_type=None,
            subject_id=None,
            details=details,
        )
    except AuditWriteError:
        # Audit failure is loud at the call site (the WARNING log line
        # below still fires) but must not crash the monitor — the
        # journald entry is a recoverable signal even when the DB
        # write failed.
        _log.exception("ops.queue_monitor.audit_write_failed depth=%d", depth)


def _record_monitor_error(audit: AuditLog, *, error: str) -> None:
    """Write a monitor-self-failure audit row. Never raises."""
    try:
        audit.record(
            actor=_ACTOR,
            action=_ACTION_ERROR,
            subject_type=None,
            subject_id=None,
            details={"error": error, "queue_name": default_queue_name},
        )
    except AuditWriteError:
        _log.exception("ops.queue_monitor.audit_write_failed_on_error error=%s", error)


def _get_audit() -> AuditLog:
    """Resolve the process-wide AuditLog the same way the API does.

    Local import keeps ``aegis.api.deps`` out of the import graph at
    module load time — the monitor is standalone and shouldn't pull
    FastAPI dependencies just to write one audit row.
    """
    from aegis.api.deps import get_audit

    return get_audit()


async def run_once(audit: AuditLog | None = None) -> int:
    """Run one monitor pass. Returns the observed queue depth (or -1 on error).

    Exposed for tests so they can drive the full path without exercising
    the ``main()`` argv / process-exit wrapper. Production callers go
    through ``main()``.
    """
    threshold = _resolve_threshold()
    audit_log = audit if audit is not None else _get_audit()
    try:
        depth = await _measure_queue_depth()
    except (RedisError, OSError) as exc:
        # OSError covers "Redis host unreachable" at the socket layer
        # (DNS failure, connection refused) which redis-py raises as a
        # bare OSError before wrapping into RedisError in some paths.
        error = f"{exc.__class__.__name__}: {exc}"
        _log.warning("ops.queue_monitor.redis_unreachable error=%s", error)
        _record_monitor_error(audit_log, error=error)
        return -1

    if depth > threshold:
        # WARNING via stderr is captured by the journald sink with the
        # right priority because the unit sets SyslogLevelPrefix=true
        # — `journalctl -u aegis-queue-monitor -p warning` surfaces it.
        _log.warning(
            "ops.queue_monitor.depth_alert depth=%d threshold=%d queue=%s",
            depth,
            threshold,
            default_queue_name,
        )
        _record_alert(audit_log, depth=depth, threshold=threshold)
    else:
        # Healthy tick — INFO log, NO audit row (CLAUDE.md: no noise).
        _log.info(
            "ops.queue_monitor.depth_ok depth=%d threshold=%d queue=%s",
            depth,
            threshold,
            default_queue_name,
        )
    return depth


def main() -> int:
    """Entry point fired by the systemd timer.

    Always returns 0 — see module docstring for the rationale.
    """
    configure_logging()
    try:
        asyncio.run(run_once())
    except Exception:
        # Defense-in-depth: any unhandled exception is logged but the
        # process still exits 0 so the unit doesn't enter a crash-loop
        # that hides real signal under the noise of a transient bug.
        # run_once already swallows RedisError + OSError; this catches
        # anything that slipped through (e.g. import-time misconfig).
        _log.exception("ops.queue_monitor.unhandled_exception")
    return 0


if __name__ == "__main__":  # pragma: no cover — invoked by systemd
    sys.exit(main())
