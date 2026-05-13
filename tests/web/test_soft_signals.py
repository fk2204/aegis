"""Tests for parse_soft_signal_flags.

Covers each known flag-string format the aggregate emits, plus the
non-matching cases:
- non-soft-signal flags (e.g. pattern codes) are silently ignored
- soft-signal-prefixed flags that don't match a known sub-format land
  in ``unmapped``
- ``is_empty`` semantics
"""

from __future__ import annotations

import pytest

from aegis.web._soft_signals import (
    ADBPartialCoverage,
    CustomerConcentration,
    NSFNegativeOverlap,
    PayrollCadence,
    SoftSignalSummary,
    parse_soft_signal_flags,
)


def test_customer_concentration_parses() -> None:
    summary = parse_soft_signal_flags(
        ["top_counterparty_concentration:43%_(acme_corp)"]
    )

    assert summary.customer_concentration == CustomerConcentration(
        top_payee="acme_corp",
        share_pct=43,
    )
    assert summary.payroll_cadence is None
    assert summary.unmapped == []


def test_payroll_with_pct_parses() -> None:
    summary = parse_soft_signal_flags(["payroll_cadence:biweekly_12%_of_revenue"])

    assert summary.payroll_cadence == PayrollCadence(
        cadence="biweekly",
        pct_of_revenue=12,
    )


def test_payroll_irregular_count_1_parses() -> None:
    """Single-payroll-event statements emit the no-pct form."""
    summary = parse_soft_signal_flags(["payroll_cadence:irregular_count_1"])

    assert summary.payroll_cadence == PayrollCadence(
        cadence="irregular_count_1",
        pct_of_revenue=None,
    )


def test_nsf_negative_overlap_parses() -> None:
    summary = parse_soft_signal_flags(["nsf_on_negative_days:4_of_7"])

    assert summary.nsf_negative_overlap == NSFNegativeOverlap(overlap=4, total=7)
    assert summary.nsf_negative_overlap is not None
    assert summary.nsf_negative_overlap.ratio_pct == 57


def test_nsf_negative_overlap_zero_total_safe() -> None:
    """ratio_pct must not divide by zero."""
    n = NSFNegativeOverlap(overlap=0, total=0)
    assert n.ratio_pct == 0


def test_adb_partial_coverage_parses() -> None:
    summary = parse_soft_signal_flags(["adb_partial_coverage:3/30"])

    assert summary.adb_partial_coverage == ADBPartialCoverage(
        covered_days=3,
        total_days=30,
    )


def test_pattern_codes_are_not_soft_signals() -> None:
    """Codes from parser.patterns must not appear in any soft-signal field
    nor in unmapped. They're rendered by pattern cards, not soft-signals."""
    summary = parse_soft_signal_flags(
        [
            "duplicate_deposits_detected",
            "wash_deposit_suspected",
            "mca_stacking",
        ]
    )

    assert summary.is_empty
    assert summary.unmapped == []


def test_soft_signal_prefixed_but_malformed_lands_in_unmapped() -> None:
    """A flag with the right prefix but a regex mismatch is surfaced as
    unmapped so the operator can notice silent degradation."""
    summary = parse_soft_signal_flags(
        ["top_counterparty_concentration:not_a_percentage_format"]
    )

    assert summary.customer_concentration is None
    assert summary.unmapped == ["top_counterparty_concentration:not_a_percentage_format"]


def test_is_empty_semantics() -> None:
    assert SoftSignalSummary().is_empty
    assert not SoftSignalSummary(
        customer_concentration=CustomerConcentration(top_payee="x", share_pct=10)
    ).is_empty
    assert not SoftSignalSummary(unmapped=["something_weird"]).is_empty


def test_multiple_flags_compose_into_one_summary() -> None:
    summary = parse_soft_signal_flags(
        [
            "top_counterparty_concentration:43%_(acme_corp)",
            "payroll_cadence:biweekly_12%_of_revenue",
            "nsf_on_negative_days:4_of_7",
            "adb_partial_coverage:3/30",
            "duplicate_deposits_detected",  # pattern code — must be ignored
        ]
    )

    assert summary.customer_concentration is not None
    assert summary.payroll_cadence is not None
    assert summary.nsf_negative_overlap is not None
    assert summary.adb_partial_coverage is not None
    assert summary.unmapped == []
    assert not summary.is_empty


@pytest.mark.parametrize(
    ("cadence", "flag"),
    [
        ("weekly", "payroll_cadence:weekly_8%_of_revenue"),
        ("biweekly", "payroll_cadence:biweekly_12%_of_revenue"),
        ("monthly", "payroll_cadence:monthly_15%_of_revenue"),
    ],
)
def test_payroll_cadence_variants(cadence: str, flag: str) -> None:
    summary = parse_soft_signal_flags([flag])

    assert summary.payroll_cadence is not None
    assert summary.payroll_cadence.cadence == cadence
    assert summary.payroll_cadence.pct_of_revenue is not None
