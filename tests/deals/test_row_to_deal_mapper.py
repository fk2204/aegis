"""Round-trip tests for the Supabase-shape ``_row_to_deal`` mapper.

The in-memory ``InMemoryDealRepository`` bypasses ``_row_to_deal``
entirely — it constructs ``DealRow`` from already-typed
``MerchantRow`` + ``DocumentRow`` objects. Tests that drive only the
in-memory repo (``tests/deals/test_repository.py``) cannot surface
field-drop or type-coercion bugs in the mapper.

This file feeds Supabase-shape row dicts (the dict-of-strings shape
``supabase-py`` returns from ``select("*, merchants(*)")``) DIRECTLY
through ``_row_to_deal`` and asserts the result. Closes the test
gap that hid the state=None Pydantic crash from 2026-06-04 (mapper
audit Track 1 H2).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from aegis.deals.repository import _row_to_deal


def _shaped_row(
    *,
    state: str | None,
    parse_status: str = "proceed",
    fraud_score: int | None = 5,
) -> dict[str, object]:
    """A row dict shaped the way ``supabase-py`` returns
    ``documents`` joined to ``merchants``. Mirrors the column names
    + nested-block shape ``SupabaseDealRepository.list_deals`` queries
    against in production."""
    document_id = uuid4()
    merchant_id = uuid4()
    return {
        "id": str(document_id),
        "merchant_id": str(merchant_id),
        "uploaded_at": datetime.now(UTC).isoformat(),
        "parse_status": parse_status,
        "fraud_score": fraud_score,
        "merchants": {
            "id": str(merchant_id),
            "business_name": "Acme LLC",
            "state": state,
        },
    }


def test_row_to_deal_preserves_uppercase_state() -> None:
    """Happy path: a real state code round-trips through the mapper
    and lands uppercased on the model."""
    row = _shaped_row(state="ca")
    deal = _row_to_deal(row)
    assert deal.state == "CA"


def test_row_to_deal_does_not_crash_on_none_state() -> None:
    """REGRESSION: state=None on a finalized auto-created merchant
    must not 500.

    Pre-fix the mapper called ``str(merchant_block["state"]).upper()``
    on a ``None`` value — producing the literal string ``"NONE"`` —
    which then failed the Pydantic 2-char state validator with a
    ValidationError. Every Deals list / detail render that touched a
    state-less merchant 500'd in prod even though the in-memory test
    suite was green (the in-memory variant has its own None-guard at
    ``repository.py:117``)."""
    row = _shaped_row(state=None)
    deal = _row_to_deal(row)
    assert deal.state is None


def test_row_to_deal_treats_empty_string_state_as_none() -> None:
    """Empty-string state should not produce ``""`` on the model —
    both shapes mean ``no extracted address yet`` and should map to
    ``None`` for the downstream dossier + match views. Mirrors the
    in-memory truthy-check at line 117."""
    row = _shaped_row(state="")
    deal = _row_to_deal(row)
    assert deal.state is None


def test_row_to_deal_preserves_fraud_score() -> None:
    row = _shaped_row(state="NV", fraud_score=42)
    deal = _row_to_deal(row)
    assert deal.fraud_score == 42


def test_row_to_deal_allows_null_fraud_score() -> None:
    """Documents in ``pending`` parse_status have no fraud_score yet.
    The mapper uses ``.get("fraud_score")`` so ``None`` round-trips."""
    row = _shaped_row(state="NV", fraud_score=None)
    deal = _row_to_deal(row)
    assert deal.fraud_score is None


def test_row_to_deal_carries_through_parse_status() -> None:
    for status in ("pending", "proceed", "review", "manual_review", "error"):
        row = _shaped_row(state="NV", parse_status=status)
        deal = _row_to_deal(row)
        assert deal.parse_status == status


def test_row_to_deal_missing_state_key_treated_as_none() -> None:
    """Defensive: if the join projection ever drops the ``state`` key
    (e.g. a future ``select(...)`` clause forgets it), the mapper
    treats it the same as an explicit ``None`` — does not KeyError."""
    row = _shaped_row(state="NV")
    # Remove the key entirely.
    merchants_block = row["merchants"]
    assert isinstance(merchants_block, dict)
    del merchants_block["state"]
    deal = _row_to_deal(row)
    assert deal.state is None


def test_row_to_deal_state_uppercase_for_lowercase_input() -> None:
    """Statement extraction occasionally lowercases state codes; the
    mapper normalizes."""
    row = _shaped_row(state="nv")
    deal = _row_to_deal(row)
    assert deal.state == "NV"


def test_row_to_deal_raises_on_missing_merchant_id() -> None:
    """If the join produced a document row without a merchant_id, the
    mapper has nothing to construct a deal from — KeyError is the
    intended hard failure (callers filter merchant_id=None upstream)."""
    row = _shaped_row(state="NV")
    del row["merchant_id"]
    with pytest.raises(KeyError):
        _row_to_deal(row)
