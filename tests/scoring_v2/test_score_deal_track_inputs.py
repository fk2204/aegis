"""Tests for ``compute_score_deal_track_inputs`` exception handling.

The function wraps ``build_unified_tracks_view`` with split exception
handlers (F1b in ``docs/track_a_audit_2026-06-12.md``):

* ``pydantic.ValidationError`` → CRITICAL log + ``(None, None)``. A
  constraint violation is a CODE BUG (e.g. a rationale exceeding
  ``max_length=320``) and must surface in the operator's structured-log
  monitoring instead of silently degrading to the legacy engine.
* Any other ``Exception`` → WARNING log + ``(None, None)``. Data
  oddities (transient DB issue, malformed input) keep the original
  warning-level fallback behaviour.

Both branches still return ``(None, None)`` so ``score_deal`` falls
back to the legacy engine — the gate against a verdict-compute crash
breaking scoring is preserved per CLAUDE.md "Decision-boundary
changes — shadow-first".

Logger note: ``aegis.logger.configure_logging`` clears existing
handlers on the root logger (idempotent reset, see logger.py:215). That
strips pytest's ``caplog`` handler unless we attach a dedicated capture
handler directly to the module's logger and read records off it. Hence
the ``_attach_capture_handler`` fixture below — it bypasses ``caplog``
entirely and avoids a flaky test that depends on logger init ordering.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from pydantic import ValidationError

from aegis.logger import get_logger
from aegis.parser.models import ClassifiedTransaction
from aegis.scoring_v2 import score_deal_inputs
from aegis.scoring_v2.score_deal_inputs import compute_score_deal_track_inputs
from aegis.scoring_v2.track_a import IntegrityVerdict

_TARGET_LOGGER_NAME = "aegis.scoring_v2.score_deal_inputs"


def _no_transactions(_doc_id: UUID) -> list[ClassifiedTransaction]:
    """``list_transactions`` stub. Never called because the patched
    ``build_unified_tracks_view`` raises before reaching it, but the
    callable shape must match the type signature."""
    return []


# Surface ``Callable`` and ``ClassifiedTransaction`` usage for static
# analysis (the stub above is the live shape under test).
_LIST_TXN_SIG: Callable[[UUID], list[ClassifiedTransaction]] = _no_transactions


def _build_real_validation_error() -> ValidationError:
    """Trigger an honest Pydantic ``ValidationError`` by calling
    ``IntegrityVerdict.model_validate`` with required fields missing.

    Using a real error (not a hand-constructed one) keeps the test
    aligned with the production failure mode the F1b fix targets — the
    handler must match whatever shape Pydantic actually produces.
    """
    try:
        IntegrityVerdict.model_validate({"verdict": "fail"})
    except ValidationError as exc:
        return exc
    raise AssertionError(
        "IntegrityVerdict.model_validate({'verdict': 'fail'}) was "
        "expected to raise ValidationError but did not."
    )


@pytest.fixture
def capture_logs() -> Iterator[list[logging.LogRecord]]:
    """Attach a record-capturing handler directly to the target logger.

    Sidesteps ``configure_logging``'s ``root.handlers.clear()`` which
    otherwise strips pytest's ``caplog`` handler. Ensures the logger is
    initialised (so ``configure_logging`` has already run) BEFORE we
    attach, so the attach survives the call.
    """
    # Force initial setup so configure_logging() runs and clears root
    # handlers exactly once — before our handler is attached.
    logger = get_logger(_TARGET_LOGGER_NAME)
    records: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _ListHandler(level=logging.DEBUG)
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def test_validation_error_logs_critical_and_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    capture_logs: list[logging.LogRecord],
) -> None:
    """A Pydantic ``ValidationError`` from ``build_unified_tracks_view``
    → CRITICAL log + ``(None, None)`` so the operator's structured-log
    monitoring catches the code bug."""
    real_validation_error = _build_real_validation_error()

    def _raise_validation_error(**_kwargs: object) -> object:
        raise real_validation_error

    monkeypatch.setattr(
        score_deal_inputs,
        "build_unified_tracks_view",
        _raise_validation_error,
    )

    result = compute_score_deal_track_inputs(
        documents=[MagicMock()],
        list_transactions=_no_transactions,
    )

    assert result == (None, None)
    critical_records = [r for r in capture_logs if r.levelno == logging.CRITICAL]
    assert len(critical_records) == 1, (
        f"expected exactly one CRITICAL record, got {len(critical_records)} "
        f"(all levels: {[r.levelname for r in capture_logs]})"
    )
    assert critical_records[0].getMessage().startswith(
        "score_deal_track_inputs.validation_error"
    )


def test_generic_exception_logs_warning_and_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    capture_logs: list[logging.LogRecord],
) -> None:
    """Any non-``ValidationError`` exception → WARNING (not CRITICAL) +
    ``(None, None)``. Preserves the data-oddity fallback semantics."""

    def _raise_runtime_error(**_kwargs: object) -> object:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(
        score_deal_inputs,
        "build_unified_tracks_view",
        _raise_runtime_error,
    )

    result = compute_score_deal_track_inputs(
        documents=[MagicMock()],
        list_transactions=_no_transactions,
    )

    assert result == (None, None)
    warning_records = [r for r in capture_logs if r.levelno == logging.WARNING]
    critical_records = [r for r in capture_logs if r.levelno == logging.CRITICAL]
    assert len(warning_records) == 1, (
        f"expected exactly one WARNING record, got {len(warning_records)} "
        f"(all levels: {[r.levelname for r in capture_logs]})"
    )
    assert warning_records[0].getMessage().startswith(
        "score_deal_track_inputs.compute_failed"
    )
    assert critical_records == [], (
        "generic Exception must not emit a CRITICAL record "
        f"(saw: {[r.getMessage() for r in critical_records]})"
    )
