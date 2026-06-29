"""Detection-only tests for the tax-return extractor.

The extractor proper (Bedrock vision call) is covered by a separate
fixture-driven test; this module pins the cheap-and-fast filename +
first-page-text routing decision so a regression in either pattern
table fails CI in under a second.
"""

from __future__ import annotations

import pytest

from aegis.parser.tax_return.extract import detect_tax_form_type


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("2024 Acme Co Form 1120-S.pdf", "1120s"),
        ("acme_1120s_return.pdf", "1120s"),
        ("Form 1120 2023.pdf", "1120"),
        ("acme_1120_return.pdf", "1120"),
        ("Partnership Return 1065.pdf", "1065"),
        ("acme_schedule_c.pdf", "schedule_c"),
        ("Schedule C 2024.pdf", "schedule_c"),
        ("scan_001.pdf", None),
        ("bank_statement_march.pdf", None),
        ("", None),
    ],
)
def test_detect_from_filename(filename: str, expected: str | None) -> None:
    assert detect_tax_form_type(filename, "") == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("U.S. Income Tax Return for an S Corporation", "1120s"),
        ("U.S. Return of Partnership Income", "1065"),
        ("Profit or Loss from Business", "schedule_c"),
        ("U.S. Corporation Income Tax Return", "1120"),
        ("Bank of America Monthly Statement", None),
        ("", None),
    ],
)
def test_detect_from_first_page_text(text: str, expected: str | None) -> None:
    assert detect_tax_form_type("scan_001.pdf", text) == expected


def test_filename_takes_precedence_over_text() -> None:
    # Filename says 1120-S, body text would say 1065 — filename wins.
    assert (
        detect_tax_form_type("Acme 1120-S Return.pdf", "U.S. Return of Partnership Income")
        == "1120s"
    )


def test_1120s_pattern_not_shadowed_by_1120() -> None:
    # Regression guard: 1120-S must NOT be classified as 1120 just
    # because "1120" is a substring of "1120-s".
    assert detect_tax_form_type("acme_1120-s.pdf", "") == "1120s"
    assert detect_tax_form_type("acme_1120s.pdf", "") == "1120s"
