"""Tests for the TCPA-litigant detector (``aegis.parser.patterns``).

Severity 100 means the underwriter sees a hard decline regardless of
cashflow. The test set pins the three triggers + the "neither fires"
fall-through.
"""

from __future__ import annotations

import pytest

from aegis.parser.patterns import detect_tcpa_litigant


@pytest.mark.parametrize(
    ("business_name", "expected_code"),
    [
        ("TCPA LITIGATOR INC", "tcpa_litigant_suspected"),
        ("Acme TCPA Litigator LLC", "tcpa_litigant_suspected"),
        ("Anything With Litigator In Name", "tcpa_litigant_suspected"),
    ],
)
def test_litigator_in_business_name_fires(business_name: str, expected_code: str) -> None:
    result = detect_tcpa_litigant(business_name, owner_name=None)
    assert result is not None
    assert result.code == expected_code
    assert result.severity == 100


@pytest.mark.parametrize(
    "owner_name",
    [
        "Brandon Callier",
        "John W Simons",
        "MARK W DAVIS",
        "brandon callier dba acme",
    ],
)
def test_known_owner_fires(owner_name: str) -> None:
    result = detect_tcpa_litigant(business_name="Acme Trucking LLC", owner_name=owner_name)
    assert result is not None
    assert result.code == "tcpa_litigant_known"
    assert result.severity == 100
    assert owner_name in result.detail


@pytest.mark.parametrize(
    ("business_name", "owner_name"),
    [
        ("Acme Trucking LLC", "Jane Doe"),
        ("Beta Plumbing", "John Smith"),
        (None, None),
        ("", ""),
        ("Litigation Services LLC", "Jane Doe"),  # 'litigation' != 'litigator'
    ],
)
def test_no_match_returns_none(business_name: str | None, owner_name: str | None) -> None:
    assert detect_tcpa_litigant(business_name, owner_name) is None
