"""Tests for boot-time anti-drift checks (mp Phase 3).

Covers the two paths in ``aegis.compliance.anti_drift``:

1. ``_log_matrix_version_and_templates`` — handles the transitional
   "state_matrix module not yet on this branch" case gracefully.
2. ``_warn_overdue_reviews`` — emits a warning for overdue
   ``07_audit_meta.yaml`` entries, skips placeholders + bad YAML
   without raising.

Tests use ``caplog`` to assert structured log events fire (or don't).
No external services; no Bedrock calls; runs under both ``make
test-fast`` and ``make check``.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

import pytest

from aegis.compliance import anti_drift

# ---------------------------------------------------------------------------
# _warn_overdue_reviews
# ---------------------------------------------------------------------------


def _write_meta(states_dir: Path, code: str, due: str | None) -> None:
    """Write a stub ``07_audit_meta.yaml`` for state ``code``.

    ``due`` may be an ISO date string or None. When None, the field is
    written as ``null`` so the loader produces ``None``.
    """
    folder = states_dir / code
    folder.mkdir(parents=True, exist_ok=True)
    due_yaml = "null" if due is None else due
    (folder / "07_audit_meta.yaml").write_text(
        f"state: {code}\n"
        "date_audited: null\n"
        "audited_by: null\n"
        "sources: []\n"
        f"next_review_due: {due_yaml}\n",
        encoding="utf-8",
    )


def test_overdue_review_emits_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(anti_drift, "STATES_DIR", tmp_path)
    _write_meta(tmp_path, "CA", (date.today() - timedelta(days=30)).isoformat())

    with caplog.at_level(logging.WARNING, logger="aegis.compliance.anti_drift"):
        anti_drift._warn_overdue_reviews()

    messages = [r.getMessage() for r in caplog.records]
    assert any("review_overdue" in m for m in messages), messages


def test_future_review_emits_no_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(anti_drift, "STATES_DIR", tmp_path)
    _write_meta(tmp_path, "CA", (date.today() + timedelta(days=30)).isoformat())

    with caplog.at_level(logging.WARNING, logger="aegis.compliance.anti_drift"):
        anti_drift._warn_overdue_reviews()

    messages = [r.getMessage() for r in caplog.records]
    assert not any("review_overdue" in m for m in messages), messages


def test_null_due_date_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(anti_drift, "STATES_DIR", tmp_path)
    _write_meta(tmp_path, "CA", None)  # placeholder skeleton state

    with caplog.at_level(logging.WARNING, logger="aegis.compliance.anti_drift"):
        anti_drift._warn_overdue_reviews()

    messages = [r.getMessage() for r in caplog.records]
    assert not any("review_overdue" in m for m in messages), messages


def test_malformed_yaml_does_not_raise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(anti_drift, "STATES_DIR", tmp_path)
    folder = tmp_path / "CA"
    folder.mkdir()
    (folder / "07_audit_meta.yaml").write_text(": bad: yaml :", encoding="utf-8")

    # Must not raise — bad YAML is a warning, not a boot blocker.
    with caplog.at_level(logging.WARNING, logger="aegis.compliance.anti_drift"):
        anti_drift._warn_overdue_reviews()

    messages = [r.getMessage() for r in caplog.records]
    assert any("audit_meta_parse_failed" in m for m in messages), messages


def test_missing_states_dir_returns_quietly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(anti_drift, "STATES_DIR", tmp_path / "does-not-exist")
    # Must not raise.
    anti_drift._warn_overdue_reviews()


# ---------------------------------------------------------------------------
# _log_matrix_version_and_templates
# ---------------------------------------------------------------------------


def test_matrix_unavailable_logs_and_returns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Pre-1A-merge state: state_matrix import fails, function returns clean."""
    import builtins
    from collections.abc import Mapping, Sequence
    from types import ModuleType

    real_import = builtins.__import__

    def fake_import(
        name: str,
        globals_: Mapping[str, object] | None = None,
        locals_: Mapping[str, object] | None = None,
        fromlist: Sequence[str] = (),
        level: int = 0,
    ) -> ModuleType:
        if name == "aegis.compliance.state_matrix":
            raise ImportError("simulated: 1A not yet merged")
        return real_import(name, globals_, locals_, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with caplog.at_level(logging.INFO, logger="aegis.compliance.anti_drift"):
        anti_drift._log_matrix_version_and_templates()

    messages = [r.getMessage() for r in caplog.records]
    assert any("matrix_not_available" in m for m in messages), messages


def test_run_boot_checks_calls_both_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Top-level entry point invokes both checks and never raises."""
    monkeypatch.setattr(anti_drift, "STATES_DIR", tmp_path)
    # Empty states dir + no matrix module: both checks return quietly.
    anti_drift.run_boot_checks()
