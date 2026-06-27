"""Tests for the migration-080 ``product_type`` column on MerchantRow.

Exercises both the Pydantic model defaulting and the in-memory
repository round-trip (write + read returns the same value). The
Supabase backend's mapper symmetry is covered by the mapper-coverage
test sweep elsewhere (``tests/integration/test_supabase_mappers.py``).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.product_types import DEFAULT_PRODUCT_TYPE


def test_merchant_default_product_type_is_revenue_based() -> None:
    """No explicit product_type → defaults to the project default."""
    merchant = MerchantRow(business_name="Test LLC")
    assert merchant.product_type == DEFAULT_PRODUCT_TYPE
    assert merchant.product_type == "revenue_based"


def test_merchant_accepts_each_supported_product_type() -> None:
    """All six ProductType literals are accepted by the model."""
    for product in (
        "revenue_based",
        "business_loan",
        "line_of_credit",
        "equipment",
        "asset_based",
        "receivables",
    ):
        merchant = MerchantRow(business_name="Test LLC", product_type=product)
        assert merchant.product_type == product


def test_merchant_rejects_unknown_product_type() -> None:
    """Strict Literal narrowing — Pydantic refuses non-literal values."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        MerchantRow(business_name="Test LLC", product_type="MCA")
    with pytest.raises(ValidationError):
        MerchantRow(business_name="Test LLC", product_type="")


def test_in_memory_repo_round_trip_preserves_product_type() -> None:
    """Write a merchant with a non-default product_type, read it back —
    the in-memory backend should preserve the value without mangling.

    Mirrors what the Supabase backend must do; if this assertion ever
    breaks at the in-memory level, the bug is in the model not in the
    mapper.
    """
    repo = InMemoryMerchantRepository()
    merchant_id = uuid4()
    merchant = MerchantRow(
        id=merchant_id,
        business_name="Equipment Buyer LLC",
        product_type="equipment",
    )
    repo.upsert(merchant)

    loaded = repo.get(merchant_id)
    assert loaded.product_type == "equipment"


def test_in_memory_repo_default_round_trip() -> None:
    """A merchant created without specifying product_type round-trips
    as the default — confirms the default is stable across the mapper.
    """
    repo = InMemoryMerchantRepository()
    merchant_id = uuid4()
    merchant = MerchantRow(id=merchant_id, business_name="Default Co")
    repo.upsert(merchant)

    loaded = repo.get(merchant_id)
    assert loaded.product_type == DEFAULT_PRODUCT_TYPE


def test_in_memory_repo_update_changes_product_type() -> None:
    """Operator change-of-product flow: a merchant initially created as
    revenue_based can be updated to business_loan via the same upsert
    path. The new value persists.
    """
    repo = InMemoryMerchantRepository()
    merchant_id = uuid4()
    repo.upsert(MerchantRow(id=merchant_id, business_name="Pivot Inc"))

    loaded = repo.get(merchant_id)
    assert loaded.product_type == "revenue_based"

    updated = loaded.model_copy(update={"product_type": "business_loan"})
    repo.upsert(updated)

    reloaded = repo.get(merchant_id)
    assert reloaded.product_type == "business_loan"
