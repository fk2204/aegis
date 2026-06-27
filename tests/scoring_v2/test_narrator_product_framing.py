"""PA A9 — product-specific framing appended to the narrator system prompt.

The composite ``_build_system_prompt(product_type)`` swaps a per-product
framing block into the base prompt. Each product gets a distinct set of
financial-lever keywords so the narrator emphasises the right axes for
the merchant's actual product.

Tests live one level below the integration test (which would need a
real Bedrock stub + a MerchantRow with product_type wired through). The
prompt builder is a pure function — that's the cleanest unit to lock in
the per-product framing contract.
"""

from __future__ import annotations

import pytest

from aegis.product_types import PRODUCT_TYPE_LABELS, ProductType
from aegis.scoring_v2.narrator import _build_system_prompt

_BASE_MARKERS: tuple[str, ...] = (
    # The base prompt's load-bearing sentences — every product must keep
    # them so the action-selection logic + no-hedge rules survive.
    "senior underwriter's verbal handoff",
    "submit_now",
    "do_not_submit",
    "call_first",
    "request_documents",
)


def test_product_type_labels_cover_every_literal() -> None:
    """Exhaustiveness guard — every ProductType has a display label.

    The dossier chip relies on the label dict being complete; an
    unhandled product would render the raw enum string and look like a
    bug. Tests are the only place that catches this — Pydantic Literal
    won't complain at import time.
    """
    expected: set[str] = {
        "revenue_based",
        "business_loan",
        "line_of_credit",
        "equipment",
        "asset_based",
        "receivables",
    }
    assert set(PRODUCT_TYPE_LABELS.keys()) == expected


def test_revenue_based_prompt_keeps_base_markers() -> None:
    """Revenue-based is the default — the base prompt MUST land verbatim
    plus an explicit ``# Revenue-based financing`` header. Regression
    guard against accidentally hollowing out the default path."""
    prompt = _build_system_prompt("revenue_based")
    for marker in _BASE_MARKERS:
        assert marker in prompt
    assert "# Revenue-based financing" in prompt
    assert "holdback" in prompt or "factor rate" in prompt


def test_business_loan_prompt_contains_dscr_and_apr_framing() -> None:
    prompt = _build_system_prompt("business_loan")
    for marker in _BASE_MARKERS:
        assert marker in prompt
    assert "# Term business loan" in prompt
    assert "DSCR" in prompt
    assert "APR" in prompt
    assert "monthly payment" in prompt.lower()
    # MCA-specific terms are explicitly called out as language to AVOID,
    # so the words still appear (in the negative instruction). The
    # contract is "MCA terminology is not the primary frame", which is
    # what the prompt says verbatim.
    assert "Avoid MCA-specific language" in prompt


def test_line_of_credit_prompt_contains_revolving_framing() -> None:
    prompt = _build_system_prompt("line_of_credit")
    assert "# Line of credit" in prompt
    assert "revolving" in prompt.lower()
    assert "draw-and-paydown" in prompt or "draw and paydown" in prompt
    assert "working-capital" in prompt or "working capital" in prompt


def test_equipment_prompt_contains_asset_coverage_framing() -> None:
    prompt = _build_system_prompt("equipment")
    assert "# Equipment finance" in prompt
    assert "asset coverage" in prompt.lower()
    assert "useful-life" in prompt or "useful life" in prompt
    assert "down payment" in prompt.lower()


def test_asset_based_prompt_contains_collateral_and_borrowing_base() -> None:
    prompt = _build_system_prompt("asset_based")
    assert "# Asset-based lending" in prompt
    assert "collateral" in prompt.lower()
    assert "borrowing-base" in prompt or "borrowing base" in prompt
    assert "A/R" in prompt or "accounts-receivable" in prompt.lower()


def test_receivables_prompt_contains_invoice_and_aging_framing() -> None:
    prompt = _build_system_prompt("receivables")
    assert "# Receivables factoring" in prompt
    assert "invoice" in prompt.lower()
    assert "debtor" in prompt.lower()
    assert "aging" in prompt.lower()
    assert "dispute" in prompt.lower()


def test_unknown_product_type_falls_back_to_revenue_based() -> None:
    """Defensive fallback — an unknown string (legacy DB row, typo)
    must land on the revenue_based framing block rather than missing
    the framing block entirely."""
    # mypy: ignore[arg-type] — deliberate runtime pass-through of an
    # invalid Literal value to exercise the ``coerce_product_type``
    # defensive path.
    prompt = _build_system_prompt("totally_made_up")  # type: ignore[arg-type]
    assert "# Revenue-based financing" in prompt
    for marker in _BASE_MARKERS:
        assert marker in prompt


@pytest.mark.parametrize(
    "product_type",
    [
        "revenue_based",
        "business_loan",
        "line_of_credit",
        "equipment",
        "asset_based",
        "receivables",
    ],
)
def test_every_product_prompt_ends_with_trailing_newline(product_type: ProductType) -> None:
    """Trailing newline is part of the prompt contract — the user
    message immediately follows the system prompt at the Bedrock call
    site, and a missing newline would glue them together."""
    prompt = _build_system_prompt(product_type)
    assert prompt.endswith("\n")
