"""Smoke tests for the ``aegis.product_types`` module.

Pinned constants — if these break, every downstream model that imports
``ProductType`` will break too, so the failure surface should fail loud
here first.
"""

from __future__ import annotations

from typing import get_args

from aegis.product_types import (
    DEFAULT_PRODUCT_TYPE,
    PRODUCT_TYPE_LABELS,
    PRODUCT_TYPE_VALUES,
    ProductType,
    is_valid_product_type,
)


def test_product_type_literal_has_six_members() -> None:
    """The Literal is the source of truth for the ENUM in migration 080.

    A drift between the Python Literal and the Postgres ENUM is a
    silent corruption surface — every row read from prod with a value
    outside the Literal would fail Pydantic strict at the model layer.
    Pin the count here so a 7th product gets a deliberate test update.
    """
    members = get_args(ProductType)
    assert len(members) == 6
    assert set(members) == {
        "revenue_based",
        "business_loan",
        "line_of_credit",
        "equipment",
        "asset_based",
        "receivables",
    }


def test_default_product_type_is_revenue_based() -> None:
    """Pre-migration-080 universal value. Per AEGIS operating-principle 4
    (no fabricated defaults), this reflects pre-080 reality — Commera
    offered exclusively MCA before the multi-product expansion.
    """
    assert DEFAULT_PRODUCT_TYPE == "revenue_based"


def test_product_type_labels_covers_every_member() -> None:
    """Every ProductType Literal value MUST have an operator-facing
    label — the dashboards / narrator framing / CSV exports read the
    label, not the literal. A new product without a label is a UI gap.
    """
    members = set(get_args(ProductType))
    assert set(PRODUCT_TYPE_LABELS.keys()) == members
    for label in PRODUCT_TYPE_LABELS.values():
        assert isinstance(label, str)
        assert label  # non-empty


def test_product_type_values_matches_literal() -> None:
    """``PRODUCT_TYPE_VALUES`` is a runtime-iterable copy of the
    Literal. Keep the two in sync — tests + repositories rely on it.
    """
    assert set(PRODUCT_TYPE_VALUES) == set(get_args(ProductType))


def test_is_valid_product_type_accepts_each_literal() -> None:
    for value in PRODUCT_TYPE_VALUES:
        assert is_valid_product_type(value)


def test_is_valid_product_type_rejects_anything_else() -> None:
    assert not is_valid_product_type("MCA")  # raw Close-side string, not the literal
    assert not is_valid_product_type("revenue based")  # space, not underscore
    assert not is_valid_product_type("")
    assert not is_valid_product_type(None)
    assert not is_valid_product_type(0)
    assert not is_valid_product_type(["revenue_based"])
