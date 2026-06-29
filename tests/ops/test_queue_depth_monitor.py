"""Tests for the Redis queue depth monitor (build plan §10.2).

The monitor is fail-quiet by design — see
``src/aegis/ops/queue_depth_monitor.py`` module docstring. These tests
pin the three behaviors that distinguish "useful alert" from "noise":

1. Depth at or below threshold → no audit row, exit 0 (silent healthy
   tick).
2. Depth above threshold → ``system.queue_depth_alert`` audit row +
   WARNING log + exit 0.
3. Redis unreachable → ``system.queue_monitor_error`` audit row + exit
   0 (the monitor never fails its own unit).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest
from arq.constants import default_queue_name
from redis.exceptions import ConnectionError as RedisConnectionError

from aegis.audit import InMemoryAuditLog
from aegis.ops import queue_depth_monitor
from aegis.ops.queue_depth_monitor import (
    _ACTION_ALERT,
    _ACTION_ERROR,
    _ACTOR,
    QUEUE_DEPTH_THRESHOLD_ENV,
    main,
    run_once,
)


def _install_pool(monkeypatch: pytest.MonkeyPatch, *, llen_return: int | Exception) -> AsyncMock:
    """Replace ``arq.create_pool`` with a stub returning a mock pool.

    The stub's ``llen`` either returns ``llen_return`` or raises it if
    it's an Exception. ``close`` is a no-op AsyncMock so the
    finally-close path executes without errors.
    """
    pool = AsyncMock()
    if isinstance(llen_return, Exception):
        pool.llen = AsyncMock(side_effect=llen_return)
    else:
        pool.llen = AsyncMock(return_value=llen_return)
    pool.close = AsyncMock(return_value=None)

    async def _create_pool(*_args: Any, **_kwargs: Any) -> AsyncMock:
        return pool

    monkeypatch.setattr(queue_depth_monitor, "create_pool", _create_pool)
    monkeypatch.setattr(
        queue_depth_monitor,
        "build_redis_settings",
        lambda: object(),  # opaque sentinel; create_pool stub ignores it
    )
    return pool


def test_depth_below_threshold_writes_no_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Healthy tick: depth=10 (threshold default 20) → silent, no audit row."""
    _install_pool(monkeypatch, llen_return=10)
    audit = InMemoryAuditLog()

    depth = asyncio.run(run_once(audit=audit))

    assert depth == 10
    assert audit.entries == [], (
        "Healthy tick must NOT write an audit row — that's noise, not signal."
    )


def test_depth_above_threshold_writes_alert_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Alert tick: depth=25 → audit row + WARNING log + still returns the depth."""
    _install_pool(monkeypatch, llen_return=25)
    audit = InMemoryAuditLog()
    caplog.set_level(logging.WARNING, logger="aegis.ops.queue_depth_monitor")

    depth = asyncio.run(run_once(audit=audit))

    assert depth == 25
    assert len(audit.entries) == 1
    row = audit.entries[0]
    assert row["actor"] == _ACTOR
    assert row["action"] == _ACTION_ALERT
    assert row["details"]["queue_depth"] == 25
    assert row["details"]["threshold"] == 20
    assert row["details"]["queue_name"] == default_queue_name
    assert any(
        "queue_monitor.depth_alert" in record.getMessage() and record.levelno >= logging.WARNING
        for record in caplog.records
    ), "Expected a WARNING-level depth_alert log line"


def test_depth_at_threshold_writes_no_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Boundary: depth == threshold → no alert (the contract is `> threshold`)."""
    _install_pool(monkeypatch, llen_return=20)
    audit = InMemoryAuditLog()

    depth = asyncio.run(run_once(audit=audit))

    assert depth == 20
    assert audit.entries == []


def test_redis_unreachable_writes_monitor_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis down: writes ``system.queue_monitor_error`` audit row, returns -1."""
    _install_pool(
        monkeypatch,
        llen_return=RedisConnectionError("simulated: ECONNREFUSED"),
    )
    audit = InMemoryAuditLog()

    depth = asyncio.run(run_once(audit=audit))

    assert depth == -1
    assert len(audit.entries) == 1
    row = audit.entries[0]
    assert row["actor"] == _ACTOR
    assert row["action"] == _ACTION_ERROR
    assert "simulated" in row["details"]["error"]
    assert row["details"]["queue_name"] == default_queue_name


def test_threshold_env_override_is_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env var ``AEGIS_QUEUE_DEPTH_ALERT_THRESHOLD`` raises the bar."""
    monkeypatch.setenv(QUEUE_DEPTH_THRESHOLD_ENV, "100")
    _install_pool(monkeypatch, llen_return=25)
    audit = InMemoryAuditLog()

    depth = asyncio.run(run_once(audit=audit))

    assert depth == 25
    assert audit.entries == [], "depth=25 must NOT alert when the threshold is raised to 100"


def test_threshold_env_override_invalid_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed override is logged and ignored; default (20) wins."""
    monkeypatch.setenv(QUEUE_DEPTH_THRESHOLD_ENV, "not-an-int")
    _install_pool(monkeypatch, llen_return=25)
    audit = InMemoryAuditLog()

    depth = asyncio.run(run_once(audit=audit))

    assert depth == 25
    assert len(audit.entries) == 1
    assert audit.entries[0]["action"] == _ACTION_ALERT
    assert audit.entries[0]["details"]["threshold"] == 20


def test_main_always_returns_zero_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``main()`` exits 0 even when an alert fired."""
    _install_pool(monkeypatch, llen_return=25)
    audit = InMemoryAuditLog()
    monkeypatch.setattr(queue_depth_monitor, "_get_audit", lambda: audit)

    assert main() == 0
    assert any(e["action"] == _ACTION_ALERT for e in audit.entries)


def test_main_always_returns_zero_on_redis_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``main()`` exits 0 even when Redis is unreachable."""
    _install_pool(
        monkeypatch,
        llen_return=RedisConnectionError("simulated: down"),
    )
    audit = InMemoryAuditLog()
    monkeypatch.setattr(queue_depth_monitor, "_get_audit", lambda: audit)

    assert main() == 0
    assert any(e["action"] == _ACTION_ERROR for e in audit.entries)
