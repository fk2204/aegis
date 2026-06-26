"""Unit tests for ``aegis.scoring.weight_calibration``.

Strategy: build inline fake-row factories that mirror the Supabase
``deal_outcomes`` + ``decisions(score_factors)`` joined response, inject a
fake client that returns those rows, and assert the engine's empirical
ratios.

No real DB; no real outcomes. Pure structural fixtures — the calibration
report is a derivation over the per-flag breakdown JSON the scorer
writes, so the data shape (not the data values) is what's load-bearing.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

import pytest

from aegis.parser.pipeline import FRAUD_WEIGHTS
from aegis.scoring.weight_calibration import (
    WeightDriftReport,
    compute_weight_drift,
)

# ---------------------------------------------------------------------------
# Fake Supabase client
# ---------------------------------------------------------------------------


class _FakeExecuteResult:
    __slots__ = ("data",)

    def __init__(self, data: list[dict[str, Any]]) -> None:
        self.data = data


class _FakeQuery:
    """Mirrors the supabase-py builder chain we use in
    ``_load_outcome_rows``: ``.select().gte().in_().execute()``.
    Each method returns self; ``execute()`` returns the canned rows.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def select(self, _cols: str) -> _FakeQuery:
        return self

    def gte(self, _col: str, _value: str) -> _FakeQuery:
        return self

    def in_(self, _col: str, _values: list[str]) -> _FakeQuery:
        return self

    def execute(self) -> _FakeExecuteResult:
        return _FakeExecuteResult(self._rows)


class _FakeClient:
    """Supabase-shape Protocol implementation backed by an in-memory
    row list."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def table(self, _name: str) -> _FakeQuery:
        return _FakeQuery(self._rows)


def _outcome_row(
    *,
    outcome: str,
    breakdown: dict[str, float],
) -> dict[str, Any]:
    """One ``deal_outcomes`` row with embedded ``decisions(score_factors)``.

    Mirrors the post-flatten shape ``_load_outcome_rows`` returns
    (``score_factors`` lifted up to row level).
    """
    return {
        "id": str(uuid4()),
        "merchant_id": str(uuid4()),
        "decision_id": str(uuid4()),
        "submitted_at": "2026-06-01T00:00:00+00:00",
        "outcome": outcome,
        "decisions": {
            "score_factors": {
                "breakdown": dict(breakdown),
                "tier": "B",
                "soft_concerns": [],
            },
            "decision_reason_codes": [],
            "score": 50,
        },
    }


def _run(report_coro: Any) -> WeightDriftReport:
    """Run a single async coroutine to completion (asyncio.run is fine
    in a unit test — no surrounding loop)."""
    return cast(WeightDriftReport, asyncio.run(report_coro))


# ---------------------------------------------------------------------------
# Empirical-comparison fixtures
# ---------------------------------------------------------------------------


def test_flag_firing_on_chargeoffs_yields_high_ratio() -> None:
    """5 charge-offs all WITH ``patterns`` fired (non-zero breakdown),
    5 paid-in-full all WITHOUT — charge_off_ratio should be very high,
    suggested_weight should be current_weight * ratio."""
    rows: list[dict[str, Any]] = []
    for _ in range(5):
        rows.append(
            _outcome_row(
                outcome="charged_off",
                breakdown={"patterns": 12.0, "metadata": 0.0, "math": 0.0},
            )
        )
    for _ in range(5):
        rows.append(
            _outcome_row(
                outcome="paid_in_full",
                breakdown={"patterns": 0.0, "metadata": 0.0, "math": 0.0},
            )
        )

    report = _run(compute_weight_drift(db=_FakeClient(rows), lookback_days=180))

    patterns_entry = next(e for e in report.entries if e.flag_code == "patterns")
    # All 5 charge-offs had patterns fired → fired_charged_off_rate = 1.0
    assert patterns_entry.fired_charged_off_rate == Decimal("1.0000")
    # All 5 not-fired were paid → not_fired_charged_off_rate = 0.0,
    # clamped to 0.001 for the ratio. Ratio = 1.0 / 0.001 = 1000.
    assert patterns_entry.charge_off_ratio >= Decimal("999")
    # Suggested weight = current_weight * very_large_ratio → also large.
    current = Decimal(str(FRAUD_WEIGHTS["patterns"])).quantize(Decimal("0.01"))
    assert patterns_entry.suggested_weight > current * Decimal("100")


def test_flag_firing_equally_yields_ratio_one() -> None:
    """When a flag fires equally on charge-offs and paid-in-full, the
    ratio is ~1.0 and the suggested_weight ~= current_weight."""
    rows: list[dict[str, Any]] = []
    # 5 charge-offs with metadata fired (1 chgoff-with-fired in 5 fired)
    # vs 5 paid-in-full with metadata fired (0 chgoff in 5 fired) →
    # that's NOT equal. We need both cohorts to have the same charge-off
    # rate among fired vs not-fired. Easiest: half of fired charged off
    # AND half of not-fired charged off.
    for _ in range(10):
        rows.append(
            _outcome_row(
                outcome="charged_off",
                breakdown={"metadata": 5.0},
            )
        )
    for _ in range(10):
        rows.append(
            _outcome_row(
                outcome="paid_in_full",
                breakdown={"metadata": 5.0},
            )
        )
    for _ in range(10):
        rows.append(
            _outcome_row(
                outcome="charged_off",
                breakdown={"metadata": 0.0},
            )
        )
    for _ in range(10):
        rows.append(
            _outcome_row(
                outcome="paid_in_full",
                breakdown={"metadata": 0.0},
            )
        )

    report = _run(compute_weight_drift(db=_FakeClient(rows), lookback_days=180))
    metadata = next(e for e in report.entries if e.flag_code == "metadata")
    # 10 fired charge-offs / 20 fired total = 0.5
    # 10 not-fired charge-offs / 20 not-fired total = 0.5
    # Ratio = 0.5 / 0.5 = 1.0
    assert metadata.charge_off_ratio == Decimal("1.0000")
    current = Decimal(str(FRAUD_WEIGHTS["metadata"])).quantize(Decimal("0.01"))
    # suggested = current * 1.0 == current (within Decimal quantize).
    assert metadata.suggested_weight == (current * Decimal("1.0000")).quantize(Decimal("0.0001"))


# ---------------------------------------------------------------------------
# Confidence-band thresholds
# ---------------------------------------------------------------------------


def test_confidence_low_below_30() -> None:
    rows = [_outcome_row(outcome="charged_off", breakdown={"metadata": 1.0}) for _ in range(5)]
    report = _run(compute_weight_drift(db=_FakeClient(rows), lookback_days=180))
    metadata = next(e for e in report.entries if e.flag_code == "metadata")
    assert metadata.sample_size == 5
    assert metadata.confidence == "low"


def test_confidence_medium_at_50() -> None:
    rows = [_outcome_row(outcome="paid_in_full", breakdown={"metadata": 1.0}) for _ in range(50)]
    report = _run(compute_weight_drift(db=_FakeClient(rows), lookback_days=180))
    metadata = next(e for e in report.entries if e.flag_code == "metadata")
    assert metadata.sample_size == 50
    assert metadata.confidence == "medium"


def test_confidence_high_at_250() -> None:
    # Mix to keep variety even though confidence is sample-size only.
    rows = [_outcome_row(outcome="paid_in_full", breakdown={"metadata": 1.0}) for _ in range(250)]
    report = _run(compute_weight_drift(db=_FakeClient(rows), lookback_days=180))
    metadata = next(e for e in report.entries if e.flag_code == "metadata")
    assert metadata.sample_size == 250
    assert metadata.confidence == "high"


# ---------------------------------------------------------------------------
# Zero-baseline clamp
# ---------------------------------------------------------------------------


def test_zero_not_fired_baseline_does_not_blow_up() -> None:
    """Every outcome has the flag fired → ``not_fired_count`` is 0, so
    the denominator clamp activates. The engine must NOT crash with
    DivisionByZero / InvalidOperation."""
    rows = [_outcome_row(outcome="charged_off", breakdown={"patterns": 10.0}) for _ in range(10)]
    report = _run(compute_weight_drift(db=_FakeClient(rows), lookback_days=180))
    patterns_entry = next(e for e in report.entries if e.flag_code == "patterns")
    assert patterns_entry.not_fired_count == 0
    # Charge-off ratio is finite (clamped denominator).
    assert patterns_entry.charge_off_ratio.is_finite()


# ---------------------------------------------------------------------------
# Decimal precision round-trip
# ---------------------------------------------------------------------------


def test_decimal_round_trip_through_pydantic_preserves_four_places() -> None:
    """Pydantic v2 with Decimal preserves the quantize precision. The
    calibration report is what the dossier renders — drifting precision
    breaks the audit trail."""
    rows = [_outcome_row(outcome="paid_in_full", breakdown={"metadata": 1.0}) for _ in range(40)]
    report = _run(compute_weight_drift(db=_FakeClient(rows), lookback_days=180))
    json_str = report.model_dump_json()
    # Re-parse and confirm decimal fields stay precise.
    rehydrated = WeightDriftReport.model_validate_json(json_str)
    for orig, new in zip(report.entries, rehydrated.entries, strict=True):
        assert orig.current_weight == new.current_weight
        assert orig.fired_charged_off_rate == new.fired_charged_off_rate
        assert orig.not_fired_charged_off_rate == new.not_fired_charged_off_rate
        assert orig.charge_off_ratio == new.charge_off_ratio
        assert orig.suggested_weight == new.suggested_weight


# ---------------------------------------------------------------------------
# Boundary conditions
# ---------------------------------------------------------------------------


def test_empty_outcomes_returns_empty_report() -> None:
    report = _run(compute_weight_drift(db=_FakeClient([]), lookback_days=180))
    assert report.total_outcomes == 0
    assert report.entries == []


def test_lookback_days_must_be_positive() -> None:
    with pytest.raises(ValueError, match="lookback_days"):
        _run(compute_weight_drift(db=_FakeClient([]), lookback_days=0))


def test_pending_and_paying_excluded_from_calibration() -> None:
    """Pending + paying outcomes are still in-flight and contribute no
    signal. The engine must skip them so the empirical ratio reflects
    only terminal outcomes."""
    rows = [
        _outcome_row(outcome="pending", breakdown={"metadata": 5.0}),
        _outcome_row(outcome="paying", breakdown={"metadata": 5.0}),
    ]
    report = _run(compute_weight_drift(db=_FakeClient(rows), lookback_days=180))
    # No terminal rows in the loader output (TERMINAL_OUTCOMES filter
    # at the .in_() call), so the report has no entries.
    assert report.entries == []


def test_one_entry_per_fraud_weights_key_with_sample() -> None:
    """Every FRAUD_WEIGHTS key with at least one observed outcome gets
    an entry. Keys with zero observations are suppressed."""
    # Single row with breakdown that only covers `metadata` — other keys
    # have no signal so they're treated as not-fired (which still gives
    # them a sample_size).
    rows = [_outcome_row(outcome="paid_in_full", breakdown={"metadata": 1.0})]
    report = _run(compute_weight_drift(db=_FakeClient(rows), lookback_days=180))
    keys_in_report = {e.flag_code for e in report.entries}
    # ``metadata`` is in the breakdown so it's clearly observed.
    assert "metadata" in keys_in_report
    # Other FRAUD_WEIGHTS keys also have sample_size = 1 (not-fired),
    # so they appear too — engine treats absence-from-breakdown as
    # not-fired evidence, not missing-evidence.
    assert "math" in keys_in_report
    assert "patterns" in keys_in_report
