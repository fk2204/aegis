"""Tests for ``aegis.scoring_v2.stips.evaluate_stips``.

Coverage per the Sprint 6 Track A spec:

* Structured kinds — voided check / driver's license / N-month bank
  statements — populate the ``required`` + ``on_file`` / ``missing``
  buckets and always carry ``is_hard=True``.
* Unknown bullets — anything outside the three keyword families — land
  in ``unknown`` and never in ``on_file`` / ``missing``. ``is_hard``
  flips based on the "must"/"required"/"mandatory" substring rule.
* Empty bullets are silently skipped.
* Duplicate bullets for the same structured family collapse to one
  item.
* Multiple unknown bullets each get their own item.
* Empty ``conditional_requirements`` returns four empty buckets.
"""

from __future__ import annotations

from aegis.funders.models import FunderRow
from aegis.merchants.models import MerchantRow
from aegis.scoring_v2.stips import StipItem, StipsResult, evaluate_stips


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
# Voided check — required + on file vs required + missing
# ---------------------------------------------------------------------------


def test_voided_check_required_and_on_file() -> None:
    result = evaluate_stips(
        funder=_funder("Voided check required at funding"),
        merchant=_merchant(voided_check=True),
    )

    assert len(result.required) == 1
    assert len(result.on_file) == 1
    assert result.missing == []
    assert result.unknown == []

    item = result.required[0]
    assert isinstance(item, StipItem)
    assert item.kind == "voided_check"
    assert item.on_file is True
    assert item.is_hard is True
    assert item.missing_field == "voided_check_on_file"
    assert "voided check" in item.requirement_text.lower()
    assert result.on_file[0] is item


def test_voided_check_required_and_missing() -> None:
    result = evaluate_stips(
        funder=_funder("Voided check required at funding"),
        merchant=_merchant(voided_check=False),
    )

    assert len(result.required) == 1
    assert len(result.missing) == 1
    assert result.on_file == []
    assert result.unknown == []

    item = result.missing[0]
    assert item.kind == "voided_check"
    assert item.on_file is False
    assert item.is_hard is True
    assert item.missing_field == "voided_check_on_file"


# ---------------------------------------------------------------------------
# Unknown bullets — hard / soft variants
# ---------------------------------------------------------------------------


def test_unknown_requirement_with_must_is_hard() -> None:
    result = evaluate_stips(
        funder=_funder("Must provide P&L for last 2 years"),
        merchant=_merchant(),
    )

    assert len(result.required) == 1
    assert len(result.unknown) == 1
    assert result.on_file == []
    assert result.missing == []

    item = result.unknown[0]
    assert item.kind == "unknown"
    assert item.on_file is False
    assert item.missing_field is None
    assert item.is_hard is True
    assert "P&L" in item.requirement_text


def test_unknown_requirement_without_hard_wording_is_soft() -> None:
    result = evaluate_stips(
        funder=_funder("Nice-to-have ACH proof"),
        merchant=_merchant(),
    )

    assert len(result.required) == 1
    assert len(result.unknown) == 1

    item = result.unknown[0]
    assert item.kind == "unknown"
    assert item.is_hard is False
    assert item.missing_field is None


# ---------------------------------------------------------------------------
# N-month bank statements — met vs unmet, sanity edges
# ---------------------------------------------------------------------------


def test_four_month_requirement_met_when_merchant_has_six() -> None:
    result = evaluate_stips(
        funder=_funder("Last 4 months bank statements"),
        merchant=_merchant(bank_months=6),
    )

    assert len(result.required) == 1
    assert len(result.on_file) == 1
    assert result.missing == []

    item = result.on_file[0]
    assert item.kind == "bank_statements_months"
    assert item.on_file is True
    assert item.is_hard is True
    assert item.missing_field == "bank_statements_months"


def test_four_month_requirement_unmet_when_merchant_has_two() -> None:
    result = evaluate_stips(
        funder=_funder("Last 4 months bank statements"),
        merchant=_merchant(bank_months=2),
    )

    assert len(result.required) == 1
    assert len(result.missing) == 1
    assert result.on_file == []

    item = result.missing[0]
    assert item.kind == "bank_statements_months"
    assert item.on_file is False
    assert item.is_hard is True


# ---------------------------------------------------------------------------
# De-duplication — structured kinds collapse, unknowns don't
# ---------------------------------------------------------------------------


def test_duplicate_voided_check_bullets_collapse_to_one_item() -> None:
    """The funder repeats themselves; the operator sees one chip."""
    result = evaluate_stips(
        funder=_funder("Voided check required", "Void check on funding"),
        merchant=_merchant(voided_check=False),
    )

    voided = [i for i in result.required if i.kind == "voided_check"]
    assert len(voided) == 1
    assert len(result.missing) == 1


def test_multiple_distinct_unknowns_each_get_their_own_item() -> None:
    result = evaluate_stips(
        funder=_funder(
            "Must provide P&L for last 2 years",
            "Must provide signed YTD financials",
        ),
        merchant=_merchant(),
    )

    assert len(result.required) == 2
    assert len(result.unknown) == 2
    assert all(i.is_hard for i in result.unknown)
    texts = {i.requirement_text for i in result.unknown}
    assert "Must provide P&L for last 2 years" in texts
    assert "Must provide signed YTD financials" in texts


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_conditional_requirements_returns_empty_buckets() -> None:
    result = evaluate_stips(
        funder=_funder(),
        merchant=_merchant(),
    )

    assert isinstance(result, StipsResult)
    assert result.required == []
    assert result.on_file == []
    assert result.missing == []
    assert result.unknown == []


def test_whitespace_only_bullets_are_skipped() -> None:
    result = evaluate_stips(
        funder=_funder("  ", "\t", "Voided check required"),
        merchant=_merchant(voided_check=False),
    )

    assert len(result.required) == 1
    assert result.required[0].kind == "voided_check"


def test_mixed_bucket_render_full_chip_grid() -> None:
    """Operator sees one green chip (statements), one red chip (voided
    check) and one yellow chip (P&L) on a realistic funder bullet
    list."""
    result = evaluate_stips(
        funder=_funder(
            "Last 4 months bank statements",
            "Voided check required",
            "Must provide P&L",
        ),
        merchant=_merchant(bank_months=6, voided_check=False),
    )

    assert len(result.required) == 3
    assert len(result.on_file) == 1
    assert len(result.missing) == 1
    assert len(result.unknown) == 1
    assert result.on_file[0].kind == "bank_statements_months"
    assert result.missing[0].kind == "voided_check"
    assert result.unknown[0].kind == "unknown"
    assert result.unknown[0].is_hard is True


def test_stip_item_is_frozen_and_strict() -> None:
    item = StipItem(
        kind="voided_check",
        requirement_text="Voided check required",
        on_file=False,
        missing_field="voided_check_on_file",
        is_hard=True,
    )
    # Frozen — assignment raises (Pydantic emits a ValidationError on
    # frozen=True; cover both possible exception types for forward
    # compat with the strict-config patterns elsewhere in the codebase).
    try:
        item.kind = "drivers_license"  # type: ignore[misc]
    except (ValueError, TypeError):
        return
    raise AssertionError("StipItem should be immutable")
