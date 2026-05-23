"""Step-9 boot-warning tests: ``warn_if_zoho_env_lingers``.

Scans ``os.environ`` at boot. If any ``ZOHO_*`` keys remain after the
Close cutover, log a structured WARN + write one audit row at
``config.zoho_residue_detected``. The latch is one-shot per process —
subsequent calls return the same residue list silently.

Variable VALUES are never logged or audited; the function only surfaces
the variable NAMES so a stray ``ZOHO_REFRESH_TOKEN`` doesn't leak.
"""

from __future__ import annotations

import logging

import pytest

from aegis.audit import InMemoryAuditLog
from aegis.config import (
    Settings,
    reset_zoho_residue_latch,
    warn_if_zoho_env_lingers,
)


@pytest.fixture(autouse=True)
def _reset_latch() -> None:
    """Reset the module-level one-shot latch before each test so they
    are independent."""
    reset_zoho_residue_latch()


def _strip_zoho_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inherited shells (operator's WSL or the Hetzner box) might have
    ZOHO_* in their environment. Force the residue baseline to be
    clean before each test asserts on it."""
    for key in list(__import__("os").environ.keys()):
        if key.startswith("ZOHO_"):
            monkeypatch.delenv(key, raising=False)


def test_no_zoho_env_no_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _strip_zoho_env(monkeypatch)
    audit = InMemoryAuditLog()
    with caplog.at_level(logging.WARNING, logger="aegis.config"):
        result = warn_if_zoho_env_lingers(audit=audit)
    assert result == []
    assert audit.entries == []
    assert not any(
        "zoho_residue_detected" in r.getMessage() for r in caplog.records
    )


def test_single_zoho_var_fires_warning_and_audit(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _strip_zoho_env(monkeypatch)
    monkeypatch.setenv("ZOHO_TEST_VALUE", "secret-payload-do-not-log")
    audit = InMemoryAuditLog()
    with caplog.at_level(logging.WARNING, logger="aegis.config"):
        result = warn_if_zoho_env_lingers(audit=audit)
    assert result == ["ZOHO_TEST_VALUE"]

    # Audit row written, names only — no values.
    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert entry["actor"] == "config"
    assert entry["action"] == "config.zoho_residue_detected"
    assert entry["details"] == {"env_vars": ["ZOHO_TEST_VALUE"]}

    # Logger WARN row carried the variable name, not the value.
    matching = [
        r for r in caplog.records
        if "zoho_residue_detected" in r.getMessage()
    ]
    assert len(matching) == 1
    assert "ZOHO_TEST_VALUE" in matching[0].getMessage()
    assert "secret-payload-do-not-log" not in matching[0].getMessage()


def test_multiple_zoho_vars_listed_sorted(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _strip_zoho_env(monkeypatch)
    monkeypatch.setenv("ZOHO_REFRESH_TOKEN", "x")
    monkeypatch.setenv("ZOHO_CLIENT_ID", "y")
    monkeypatch.setenv("ZOHO_API_BASE", "z")
    audit = InMemoryAuditLog()
    with caplog.at_level(logging.WARNING, logger="aegis.config"):
        result = warn_if_zoho_env_lingers(audit=audit)
    assert result == ["ZOHO_API_BASE", "ZOHO_CLIENT_ID", "ZOHO_REFRESH_TOKEN"]
    assert audit.entries[0]["details"]["env_vars"] == result


def test_warning_is_one_shot_per_process(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Second call returns the same list silently — no second log row,
    no second audit row."""
    _strip_zoho_env(monkeypatch)
    monkeypatch.setenv("ZOHO_FOO", "x")
    audit = InMemoryAuditLog()
    with caplog.at_level(logging.WARNING, logger="aegis.config"):
        first = warn_if_zoho_env_lingers(audit=audit)
        second = warn_if_zoho_env_lingers(audit=audit)
    assert first == ["ZOHO_FOO"]
    assert second == ["ZOHO_FOO"]
    # Only ONE audit row + ONE log record across two calls.
    assert len(audit.entries) == 1
    matching = [
        r for r in caplog.records
        if "zoho_residue_detected" in r.getMessage()
    ]
    assert len(matching) == 1


def test_no_audit_log_still_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Audit injection is optional — without it, the logger WARN still
    fires so the residue is still operator-visible."""
    _strip_zoho_env(monkeypatch)
    monkeypatch.setenv("ZOHO_BAR", "x")
    with caplog.at_level(logging.WARNING, logger="aegis.config"):
        result = warn_if_zoho_env_lingers(audit=None)
    assert result == ["ZOHO_BAR"]
    matching = [
        r for r in caplog.records
        if "zoho_residue_detected" in r.getMessage()
    ]
    assert len(matching) == 1


def test_audit_write_failure_does_not_propagate(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An exploding audit sink must not crash the boot warning — the
    logger WARN above is the primary signal."""

    class _ExplodingAudit:
        def record(self, **kwargs: object) -> None:
            raise RuntimeError("audit DB down")

    _strip_zoho_env(monkeypatch)
    monkeypatch.setenv("ZOHO_BAZ", "x")
    with caplog.at_level(logging.WARNING, logger="aegis.config"):
        result = warn_if_zoho_env_lingers(audit=_ExplodingAudit())  # type: ignore[arg-type]
    assert result == ["ZOHO_BAZ"]
    # Primary residue WARN + a secondary "audit_write_failed" WARN.
    msgs = [r.getMessage() for r in caplog.records]
    assert any("zoho_residue_detected" in m for m in msgs)
    assert any("zoho_residue_audit_write_failed" in m for m in msgs)


# ----------------------------------------------------------------------
# Confirm all four CLOSE_* settings exist with correct defaults.
# ----------------------------------------------------------------------


def test_all_four_close_settings_present_with_expected_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 4 CLOSE_* settings must be wired: 2 SecretStr | None, 2 str
    with operator-confirmed defaults."""
    monkeypatch.setenv("AEGIS_DATA_RESIDENCY_CONFIRMED", "true")
    monkeypatch.delenv("CLOSE_API_KEY", raising=False)
    monkeypatch.delenv("CLOSE_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("CLOSE_API_BASE", raising=False)
    monkeypatch.delenv("CLOSE_DOCS_IN_PRE_UW_STATUS_ID", raising=False)
    # Settings.__init__ reads .env automatically; we want clean defaults
    # for the assertion, so disable env_file by passing an absent path.
    s = Settings(_env_file=None)
    # SecretStr | None — unset → None
    assert s.close_api_key is None
    assert s.close_webhook_secret is None
    # str with defaults
    assert s.close_api_base == "https://api.close.com"
    assert (
        s.close_docs_in_pre_uw_status_id
        == "stat_1YZuVqdPWC8HLjWWvnXqL3NBJUPSjw3upy9mdBYXRqI"
    )
