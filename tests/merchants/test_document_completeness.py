"""Document-completeness checker unit tests.

Pure-function coverage for ``aegis.merchants.document_completeness.check_completeness``:

* Voided check requirement scan — flag false / flag true.
* Driver's license requirement scan — case-insensitive, hyphen and
  apostrophe variants.
* "Last N months bank statements" scan — under-count → warning,
  exact-count → clear, over-count → clear.
* Multi-requirement funder with partial coverage — only the missing
  ones surface.
* Empty ``conditional_requirements`` tuple — no warnings ever.
* Case-insensitive scan ("Voided Check" matches as well as "voided check").
* Multiple mentions of the same requirement collapse to one warning.
"""

from __future__ import annotations

import pytest

from aegis.funders.models import FunderRow
from aegis.merchants.document_completeness import (
    DocumentCompletenessWarning,
    check_completeness,
)
from aegis.merchants.models import MerchantRow


def _merchant(
    *,
    voided_check: bool = False,
    drivers_license: bool = False,
    bank_months: int = 0,
) -> MerchantRow:
    return MerchantRow(
        business_name="Acme Painting LLC",
        state="CA",
        status="finalized",
        voided_check_on_file=voided_check,
        drivers_license_on_file=drivers_license,
        bank_statements_months=bank_months,
    )


def _funder(*requirements: str) -> FunderRow:
    return FunderRow(name="Wide Net Capital", conditional_requirements=requirements)


# ---------------------------------------------------------------------------
# Voided check
# ---------------------------------------------------------------------------


def test_voided_check_requirement_with_flag_false_emits_warning() -> None:
    warnings = check_completeness(
        merchant=_merchant(voided_check=False),
        funder=_funder("Voided check required at funding"),
    )
    assert len(warnings) == 1
    assert warnings[0].requirement_kind == "voided_check"
    assert warnings[0].missing_field == "voided_check_on_file"
    assert "voided check" in warnings[0].requirement_text.lower()


def test_voided_check_requirement_with_flag_true_no_warning() -> None:
    warnings = check_completeness(
        merchant=_merchant(voided_check=True),
        funder=_funder("Voided check required at funding"),
    )
    assert warnings == []


# ---------------------------------------------------------------------------
# Driver's license
# ---------------------------------------------------------------------------


def test_drivers_license_requirement_case_insensitive_apostrophe_variant() -> None:
    """Funders write the requirement multiple ways — operators see them
    all in the same scan."""
    for prose in (
        "Driver's License required",
        "drivers license needed",
        "Copy of Driver License with application",
    ):
        warnings = check_completeness(
            merchant=_merchant(drivers_license=False),
            funder=_funder(prose),
        )
        assert any(w.requirement_kind == "drivers_license" for w in warnings), prose


def test_drivers_license_requirement_with_flag_true_no_warning() -> None:
    warnings = check_completeness(
        merchant=_merchant(drivers_license=True),
        funder=_funder("Driver's License required"),
    )
    assert warnings == []


# ---------------------------------------------------------------------------
# N months of bank statements
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prose", "merchant_months"),
    [
        ("Last 6 months bank statements", 4),
        ("6-month bank statement history", 4),
        ("6 mos bank statements", 4),
    ],
)
def test_six_month_requirement_with_four_months_emits_warning(
    prose: str, merchant_months: int
) -> None:
    warnings = check_completeness(
        merchant=_merchant(bank_months=merchant_months),
        funder=_funder(prose),
    )
    assert len(warnings) == 1
    assert warnings[0].requirement_kind == "bank_statements_months"
    assert warnings[0].missing_field == "bank_statements_months"


def test_six_month_requirement_with_exactly_six_no_warning() -> None:
    warnings = check_completeness(
        merchant=_merchant(bank_months=6),
        funder=_funder("Last 6 months bank statements"),
    )
    assert warnings == []


def test_six_month_requirement_with_more_than_six_no_warning() -> None:
    warnings = check_completeness(
        merchant=_merchant(bank_months=12),
        funder=_funder("Last 6 months bank statements"),
    )
    assert warnings == []


# ---------------------------------------------------------------------------
# Multi-requirement coverage
# ---------------------------------------------------------------------------


def test_multi_requirement_funder_partial_coverage_surfaces_only_missing() -> None:
    """Operator has voided check + 4 months of statements, but missing
    the driver's license. Funder requires all three. Only the DL
    surfaces."""
    warnings = check_completeness(
        merchant=_merchant(voided_check=True, drivers_license=False, bank_months=6),
        funder=_funder(
            "Voided check at funding",
            "Driver's license copy",
            "Last 6 months bank statements",
        ),
    )
    assert len(warnings) == 1
    assert warnings[0].requirement_kind == "drivers_license"


def test_multi_requirement_funder_full_coverage_no_warnings() -> None:
    warnings = check_completeness(
        merchant=_merchant(voided_check=True, drivers_license=True, bank_months=6),
        funder=_funder(
            "Voided check at funding",
            "Driver's license copy",
            "Last 6 months bank statements",
        ),
    )
    assert warnings == []


def test_multi_requirement_funder_nothing_on_file_surfaces_all() -> None:
    warnings = check_completeness(
        merchant=_merchant(voided_check=False, drivers_license=False, bank_months=0),
        funder=_funder(
            "Voided check at funding",
            "Driver's license copy",
            "Last 6 months bank statements",
        ),
    )
    tokens = {w.requirement_kind for w in warnings}
    assert tokens == {"voided_check", "drivers_license", "bank_statements_months"}


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_conditional_requirements_returns_no_warnings_even_when_nothing_on_file() -> None:
    warnings = check_completeness(
        merchant=_merchant(),  # nothing on file
        funder=_funder(),  # no requirements
    )
    assert warnings == []


def test_case_insensitive_scan_voided_check_capitalized() -> None:
    warnings = check_completeness(
        merchant=_merchant(voided_check=False),
        funder=_funder("Voided Check"),
    )
    assert len(warnings) == 1
    assert warnings[0].requirement_kind == "voided_check"


def test_duplicate_voided_check_mentions_collapse_to_one_warning() -> None:
    """A funder whose conditional_requirements has multiple strings each
    mentioning a voided check should still surface a single warning."""
    warnings = check_completeness(
        merchant=_merchant(voided_check=False),
        funder=_funder(
            "Voided check required at funding",
            "Voided check copy must be uploaded",
        ),
    )
    assert len(warnings) == 1
    assert warnings[0].requirement_kind == "voided_check"


def test_unrelated_requirement_strings_are_ignored() -> None:
    """The checker only knows about the three flags. A funder requirement
    about COJ or guarantor status produces no warning even if the
    underlying need is real — operator owns those via dossier review."""
    warnings = check_completeness(
        merchant=_merchant(),
        funder=_funder("Confession of judgment required", "Personal guarantor signed"),
    )
    assert warnings == []


def test_warning_model_is_strict_pydantic_frozen() -> None:
    """``DocumentCompletenessWarning`` is exposed in JSON via the route
    layer; the model needs to stay strict + immutable so downstream
    consumers can rely on its shape."""
    w = DocumentCompletenessWarning(
        requirement_kind="voided_check",
        requirement_text="Voided check",
        missing_field="voided_check_on_file",
    )
    with pytest.raises((ValueError, TypeError)):
        w.requirement_kind = "other"  # type: ignore[misc]
