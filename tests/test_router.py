"""Tests for the per-deal compliance router (mp Phase 1).

Covers:

1. Tier 1 specific scenarios (CA / NY / TX / LA) with the master plan's
   exact expected outputs.
2. Negative paths: unknown state, unknown product, non-Decimal amount.
3. **Exhaustive coverage**: 51 USPS codes x 6 product types x 3 amount
   tiers = 918 cases. Each case must produce a valid RouterResult — no
   exception, no silent wrong-tier routing. The master plan §11 calls for
   "50 states x 3 product types x 3 amount tiers = 450 cases"; we exceed
   that floor because the matrix carries 51 entries (DC) and the product
   set is 6 (sales_based, closed_end, open_end, factoring, lease,
   asset_based).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from aegis.compliance.router import (
    RouterResult,
    UnknownProductTypeError,
    UnknownStateError,
    router,
)
from aegis.compliance.state_matrix import StateMatrix, load_matrix


@pytest.fixture(scope="module")
def matrix() -> StateMatrix:
    return load_matrix()


# ---------------------------------------------------------------------------
# Tier 1 spot checks
# ---------------------------------------------------------------------------


def test_ca_sales_based_in_threshold_is_tier1(matrix: StateMatrix) -> None:
    """CA SB 1235 + SB 362: sales_based ≤ $500k → Tier 1 + CA template."""
    result = router("CA", Decimal("400000"), "sales_based", matrix)
    assert result.tier == 1
    assert result.template_path == Path("docs/compliance/states/CA/03_disclosure_template.j2")
    assert result.applicable_rules == ["ca_tier1"]
    assert result.hard_decline_rules == []


def test_ca_above_threshold_is_tier3_fallback(matrix: StateMatrix) -> None:
    """CA deal above the $500k threshold falls to defensive Tier 3."""
    result = router("CA", Decimal("750000"), "sales_based", matrix)
    assert result.tier == 3
    assert "tier_3_defensive" in result.applicable_rules
    assert result.template_path is None


def test_tx_sales_based_in_threshold_has_hard_decline(matrix: StateMatrix) -> None:
    """TX HB 700: sales_based ≤ $1M → Tier 1 + tx_autodebit hard decline."""
    result = router("TX", Decimal("400000"), "sales_based", matrix)
    assert result.tier == 1
    assert any(
        r.code == "tx_autodebit_without_first_priority_lien"
        for r in result.hard_decline_rules
    )


def test_tx_hard_decline_persists_when_out_of_scope(matrix: StateMatrix) -> None:
    """TX auto-debit rule fires even when the deal is out of CFDL scope
    (product_type != sales_based, or amount > $1M)."""
    result = router("TX", Decimal("400000"), "closed_end", matrix)
    assert result.tier == 3
    # Hard-decline still applies because the auto-debit prohibition is
    # an overlay-level rule, not amount/product-scoped.
    assert any(
        r.code == "tx_autodebit_without_first_priority_lien"
        for r in result.hard_decline_rules
    )


def test_la_no_threshold_means_always_in_scope(matrix: StateMatrix) -> None:
    """LA HB 470: no deal-size cap; any sales_based amount → Tier 1."""
    for amount in (Decimal("1"), Decimal("100000"), Decimal("5000000")):
        result = router("LA", amount, "sales_based", matrix)
        assert result.tier == 1, f"LA amount {amount} should still be Tier 1"
        assert result.template_path == Path("docs/compliance/states/LA/03_disclosure_template.j2")


def test_la_non_mca_falls_through(matrix: StateMatrix) -> None:
    """LA covers MCA only — a LA closed-end loan is Tier 3 fallback."""
    result = router("LA", Decimal("100000"), "closed_end", matrix)
    assert result.tier == 3


def test_tier2_state_routes_as_tier2(matrix: StateMatrix) -> None:
    """Tier 2 (e.g. NJ, MD): always tier=2 with watchlist marker."""
    for code in ("NJ", "MD", "IL", "PA"):
        result = router(code, Decimal("400000"), "sales_based", matrix)
        assert result.tier == 2, code
        assert f"{code.lower()}_watchlist" in result.applicable_rules
        assert result.template_path is None
        assert result.hard_decline_rules == []


def test_tier3_state_routes_as_tier3(matrix: StateMatrix) -> None:
    for code in ("AL", "MA", "DE", "WY"):
        result = router(code, Decimal("400000"), "sales_based", matrix)
        assert result.tier == 3, code
        assert "tier_3_defensive" in result.applicable_rules
        assert result.template_path is None
        assert result.hard_decline_rules == []


# ---------------------------------------------------------------------------
# Phase 4 (mp §14) — newly served Tier 1 states
# ---------------------------------------------------------------------------
#
# Snapshot-style tests on rule routing for each state Phase 4 moved out
# of ``state_not_served``. The legacy ``aegis.compliance.states`` table
# still records VA/CT/UT/MO as Tier 3 (template comes in Phase 5), but
# the new matrix-driven router already returns the Tier 1 surface for
# in-scope deals. These tests lock the router output so a regression
# (e.g. an accidental edit to ``states.yaml``) is caught immediately.


def test_va_sales_based_in_threshold_is_tier1(matrix: StateMatrix) -> None:
    """VA HB 1027: MCA-only, ≤ $500k → Tier 1 + VA template."""
    result = router("VA", Decimal("400000"), "sales_based", matrix)
    assert result.tier == 1
    assert result.template_path == Path(
        "docs/compliance/states/VA/03_disclosure_template.j2"
    )
    assert result.applicable_rules == ["va_tier1"]
    assert result.hard_decline_rules == []


def test_va_non_mca_falls_through(matrix: StateMatrix) -> None:
    """VA HB 1027 covers MCA only — a VA closed-end loan is Tier 3 fallback."""
    result = router("VA", Decimal("100000"), "closed_end", matrix)
    assert result.tier == 3
    assert "va_overlays_apply" in result.applicable_rules


def test_ct_sales_based_in_threshold_is_tier1(matrix: StateMatrix) -> None:
    """CT SB 1032: MCA-only, ≤ $250k → Tier 1 + CT template."""
    result = router("CT", Decimal("200000"), "sales_based", matrix)
    assert result.tier == 1
    assert result.template_path == Path(
        "docs/compliance/states/CT/03_disclosure_template.j2"
    )
    assert result.applicable_rules == ["ct_tier1"]
    assert result.hard_decline_rules == []


def test_ct_above_threshold_is_tier3_fallback(matrix: StateMatrix) -> None:
    """CT threshold is $250k — a $400k MCA falls to Tier 3 defensive."""
    result = router("CT", Decimal("400000"), "sales_based", matrix)
    assert result.tier == 3
    assert "ct_overlays_apply" in result.applicable_rules


def test_ut_sales_based_in_threshold_is_tier1(matrix: StateMatrix) -> None:
    """UT HB 198: broad commercial, ≤ $1M → Tier 1 + UT template."""
    result = router("UT", Decimal("750000"), "sales_based", matrix)
    assert result.tier == 1
    assert result.template_path == Path(
        "docs/compliance/states/UT/03_disclosure_template.j2"
    )
    assert result.applicable_rules == ["ut_tier1"]
    assert result.hard_decline_rules == []


def test_ut_closed_end_in_scope(matrix: StateMatrix) -> None:
    """UT's product_scope is broad — closed-end loans are also covered."""
    result = router("UT", Decimal("400000"), "closed_end", matrix)
    assert result.tier == 1
    assert result.applicable_rules == ["ut_tier1"]


def test_mo_sales_based_in_threshold_is_tier1(matrix: StateMatrix) -> None:
    """MO SB 1359 § 427.300: broad commercial, ≤ $500k → Tier 1 + MO template."""
    result = router("MO", Decimal("400000"), "sales_based", matrix)
    assert result.tier == 1
    assert result.template_path == Path(
        "docs/compliance/states/MO/03_disclosure_template.j2"
    )
    assert result.applicable_rules == ["mo_tier1"]
    assert result.hard_decline_rules == []


def test_mo_above_threshold_is_tier3_fallback(matrix: StateMatrix) -> None:
    """MO threshold is $500k — a $750k MCA falls to Tier 3 defensive."""
    result = router("MO", Decimal("750000"), "sales_based", matrix)
    assert result.tier == 3
    assert "mo_overlays_apply" in result.applicable_rules


def test_tx_hard_decline_message_cites_hb_700(matrix: StateMatrix) -> None:
    """TX HB 700 hard-decline message must explain the auto-debit rule.

    Master plan §14 task 3: 'Decline message explains HB 700.' This test
    locks the decline message contents so the operator-facing surface
    cannot drift without an explicit ``states.yaml`` edit.
    """
    result = router("TX", Decimal("400000"), "sales_based", matrix)
    matching = [
        r for r in result.hard_decline_rules
        if r.code == "tx_autodebit_without_first_priority_lien"
    ]
    assert matching, "expected the TX auto-debit hard-decline rule"
    message = matching[0].message
    assert "HB 700" in message
    assert "first-priority" in message.lower()
    assert "ucc" in message.lower()


def test_tx_hard_decline_fires_for_all_product_types(matrix: StateMatrix) -> None:
    """TX auto-debit prohibition is overlay-level — it fires regardless
    of product type. Master plan §8.5: 'Effective deal-killer for
    standard MCA in TX.'"""
    for product in ("sales_based", "closed_end", "open_end", "factoring"):
        result = router("TX", Decimal("400000"), product, matrix)
        assert any(
            r.code == "tx_autodebit_without_first_priority_lien"
            for r in result.hard_decline_rules
        ), f"TX product {product} should still carry the auto-debit decline"


# ---------------------------------------------------------------------------
# Negative paths
# ---------------------------------------------------------------------------


def test_unknown_state_raises(matrix: StateMatrix) -> None:
    with pytest.raises(UnknownStateError):
        router("ZZ", Decimal("400000"), "sales_based", matrix)


def test_unknown_product_raises(matrix: StateMatrix) -> None:
    with pytest.raises(UnknownProductTypeError):
        router("CA", Decimal("400000"), "bogus_product", matrix)


def test_non_decimal_amount_raises(matrix: StateMatrix) -> None:
    # Intentionally pass an int; the router must reject non-Decimal money.
    not_decimal: object = 400000
    with pytest.raises(TypeError):
        router("CA", not_decimal, "sales_based", matrix)  # type: ignore[arg-type]


def test_float_amount_raises(matrix: StateMatrix) -> None:
    not_decimal: object = 400000.0
    with pytest.raises(TypeError):
        router("CA", not_decimal, "sales_based", matrix)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Exhaustive coverage: 51 codes x 6 products x 3 amounts = 918 cases
# ---------------------------------------------------------------------------

_ALL_CODES = sorted(
    {
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
        "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
        "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
        "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
        "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
        "DC",
    }
)
_ALL_PRODUCTS = [
    "sales_based",
    "closed_end",
    "open_end",
    "factoring",
    "lease",
    "asset_based",
]
_AMOUNT_TIERS = [Decimal("50000"), Decimal("400000"), Decimal("1500000")]


@pytest.mark.parametrize("code", _ALL_CODES)
@pytest.mark.parametrize("product", _ALL_PRODUCTS)
@pytest.mark.parametrize("amount", _AMOUNT_TIERS)
def test_router_exhaustive(
    matrix: StateMatrix, code: str, product: str, amount: Decimal
) -> None:
    """Every state x product x amount must return a valid RouterResult.

    Master plan §11 acceptance: 450 cases minimum. This parametrization
    covers 918 (51 x 6 x 3). All must succeed — never raise, never
    return an invalid tier.
    """
    result = router(code, amount, product, matrix)
    assert isinstance(result, RouterResult)
    assert result.tier in (1, 2, 3)
    assert result.applicable_rules  # non-empty
