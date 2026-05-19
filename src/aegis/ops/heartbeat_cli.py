"""CLI used by the systemd heartbeat timer (mp Phase 11 task #1).

Invoked once per timer tick by ``aegis-heartbeat-{web,worker}.service``.
The unit fires the URL configured in
``AEGIS_HEALTHCHECK_{WEB,WORKER}_URL``, choosing the ``/fail`` variant
when a local liveness pre-check fails.

Liveness pre-check
------------------
Before pinging the success URL, the CLI runs a minimal health probe:

* ``--component web``    → HTTP GET ``http://127.0.0.1:5555/healthz``
  expects 200. The uvicorn process must be answering /healthz at all.
* ``--component worker`` → ``redis-cli PING`` equivalent (via the
  ``redis`` package). The arq worker depends on Redis being up; if
  Redis is down the worker can't be processing jobs.

A failed pre-check makes the heartbeat ping the ``/fail`` endpoint so
the Healthchecks.io dashboard records the failure timestamp instead
of waiting for the dead-man's-switch to trip — fail-loud is the right
behavior here.

Exit codes
----------
The unit is fired by systemd timer with Type=oneshot, so the exit
code is only consumed by ``systemctl status``. We always exit 0 once
the ping (success or /fail) has been issued so the systemd job log
shows the *intent* succeeded; pre-check failures are surfaced through
the /fail variant of the URL, not the exit code.
"""

from __future__ import annotations

import argparse
import sys

from aegis.logger import configure_logging, get_logger
from aegis.ops.alerting import load_alert_config, ping_healthcheck

_log = get_logger(__name__)


def _web_alive() -> bool:
    """Pre-check: is the local uvicorn answering /healthz on port 5555?"""
    import httpx

    try:
        with httpx.Client(timeout=2.0) as client:
            response = client.get("http://127.0.0.1:5555/healthz")
        return response.status_code == 200
    except httpx.HTTPError:
        return False


def _worker_alive() -> bool:
    """Pre-check: is Redis reachable?

    We don't probe arq directly — its queue depth fluctuates by design
    and saying "depth > 0" is unhealthy is wrong. Redis-up is the
    necessary precondition for the worker to be making progress; the
    queue-depth alert is a separate condition handled by
    ``alert_arq_queue_depth`` in the alerting module.
    """
    import redis

    from aegis.config import get_settings

    try:
        client = redis.Redis.from_url(get_settings().redis_url)
        return bool(client.ping())
    except (redis.RedisError, ConnectionError):
        return False


def main(argv: list[str] | None = None) -> int:
    """Entry point fired by the systemd heartbeat timer."""
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--component",
        required=True,
        choices=("web", "worker"),
        help="Which heartbeat URL to ping.",
    )
    args = parser.parse_args(argv)

    config = load_alert_config()
    alive = _web_alive() if args.component == "web" else _worker_alive()
    _log.info(
        "ops.heartbeat.pre_check component=%s alive=%s",
        args.component,
        alive,
    )
    ping_healthcheck(config, component=args.component, failed=not alive)
    return 0


if __name__ == "__main__":  # pragma: no cover — invoked by systemd
    sys.exit(main())
