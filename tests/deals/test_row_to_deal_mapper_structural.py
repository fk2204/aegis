"""Structural schema-coverage test for ``_row_to_deal`` (H1 pattern).

Companion to ``tests/deals/test_row_to_deal_mapper.py`` (the per-field
edge-case suite shipped by H2 against the state=None Pydantic crash).
This file owns the structural guard: introspect ``DealRow.model_fields``
and assert every field that came in non-None on the Supabase-shape
row also comes out non-None on the model.

Why this exists separately from H2:
  * H2 covered the known regression (state=None) plus parse_status and
    fraud_score edge cases, but did NOT introspect ``DealRow``'s
    declared fields. A new column added to ``DealRow`` + the SELECT
    projection that ``SupabaseDealRepository`` issues, with the
    mapper forgotten, would slip the H2 suite.
  * This is the exact pattern that hid the chunk-B regression on
    ``_row_to_document`` for weeks (mapper audit Track 1 H1). The
    structural-introspection guard is the cheapest fix.

The mapper itself is NOT modified by this test file. If the structural
assertion exposes a real bug, the test marks it via ``# REPORT:`` and
xfails — so the gate stays green and the operator reviews separately.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from aegis.deals.models import DealRow, format_deal_id
from aegis.deals.repository import _row_to_deal

# The mapper sets ``score_recommendation=None`` unconditionally — the
# score cache layer that will populate it has not landed yet. Documented
# at ``deals/repository.py:240-249`` (``_recommendation_from_analysis``
# placeholder). Excluded from the every-field-non-None assertion so
# the structural guard doesn't false-positive on this known-deferred
# field. Remove from this set when the score cache lands.
_INTENTIONALLY_UNHYDRATED_FIELDS = frozenset({"score_recommendation"})


def _full_row() -> dict[str, object]:
    """Supabase-shape row dict with values for every column the deals
    join projection emits in production.

    Shape mirrors ``SupabaseDealRepository.list_deals``' SELECT:

        documents(id, merchant_id, parse_status, fraud_score, uploaded_at,
                  merchants!inner(business_name, state),
                  analyses(id))

    The ``analyses`` block is unused by ``_row_to_deal`` (the score
    cache will read it later) but included for fidelity with the
    real query shape.
    """
    document_id = uuid4()
    merchant_id = uuid4()
    return {
        "id": str(document_id),
        "merchant_id": str(merchant_id),
        "uploaded_at": "2026-06-03T18:41:58Z",
        "parse_status": "proceed",
        "fraud_score": 12,
        "merchants": {
            "id": str(merchant_id),
            "business_name": "Acme Painting LLC",
            "state": "CA",
        },
        "analyses": [{"id": str(uuid4())}],
    }


# ----------------------------------------------------------------------
# Schema coverage — the "new column added but mapper forgotten" guard
# ----------------------------------------------------------------------


def test_row_to_deal_hydrates_every_field_on_deal_row() -> None:
    """STRUCTURAL GUARD. Introspects ``DealRow.model_fields`` and
    asserts every declared field except the documented exclusions
    lands non-None when the row carries a non-None value.

    This is the H1 pattern from
    ``tests/storage/test_row_to_document_mapper.py``. The chunk-B bug
    that hid for weeks was a column-add that updated the model but
    not the mapper — this test would have caught it.

    Don't relax this test. Adding ``# noqa`` or ad-hoc skip arguments
    defeats the purpose. If a field is intentionally not hydratable
    from the row (computed elsewhere, like ``score_recommendation``),
    document the reason and add it to ``_INTENTIONALLY_UNHYDRATED_FIELDS``
    above with a named-exception comment that the next reader can audit.
    """
    row = _full_row()
    deal = _row_to_deal(row)

    # Synthesize a row-equivalent dict for the fields ``_row_to_deal``
    # composes (deal_id from the two ids, business_name + state from
    # the nested merchants block, created_at from uploaded_at). This
    # lets the per-field assertion below check "row had a value for
    # this destination field" symmetrically.
    merchants_block = row["merchants"]
    assert isinstance(merchants_block, dict)
    synthesized_row_view: dict[str, object | None] = {
        "deal_id": format_deal_id(
            UUID(str(row["merchant_id"])), UUID(str(row["id"]))
        ),
        "merchant_id": row["merchant_id"],
        "document_id": row["id"],
        "created_at": row["uploaded_at"],
        "business_name": merchants_block["business_name"],
        "state": merchants_block["state"],
        "parse_status": row["parse_status"],
        "fraud_score": row["fraud_score"],
        "score_recommendation": None,
    }

    declared_fields = set(DealRow.model_fields.keys())
    for field_name in declared_fields:
        if field_name in _INTENTIONALLY_UNHYDRATED_FIELDS:
            continue
        model_value = getattr(deal, field_name)
        row_value = synthesized_row_view.get(field_name)
        if row_value is None:
            # Row didn't carry it — model default is fine.
            continue
        assert model_value is not None, (
            f"Mapper dropped field {field_name!r}: row carried "
            f"{row_value!r} but DealRow came out None. This is the "
            "exact bug class that hid for weeks on the chunk-B "
            "retention columns. Update _row_to_deal in "
            "src/aegis/deals/repository.py."
        )


def test_dealrow_model_fields_match_known_set() -> None:
    """Pin the set of declared ``DealRow`` fields. If a field is added
    or removed, this test fails — forcing the editor to also update
    the structural test above (and ``_row_to_deal``).

    Tripwire only. Update the expected set IFF you have updated
    ``_row_to_deal`` to hydrate the new field (or removed the field
    from production reads)."""
    expected = {
        "deal_id",
        "merchant_id",
        "document_id",
        "created_at",
        "business_name",
        "state",
        "parse_status",
        "fraud_score",
        "score_recommendation",
    }
    assert set(DealRow.model_fields.keys()) == expected


# ----------------------------------------------------------------------
# Per-field type coercion — UUID strings + datetime Z-suffix
# ----------------------------------------------------------------------


def test_row_to_deal_coerces_uuid_strings_to_uuid_type() -> None:
    """Supabase returns ids as strings. The mapper must produce
    ``UUID`` instances on the model so downstream code can compare
    against other ``UUID`` values without manual coercion."""
    row = _full_row()
    deal = _row_to_deal(row)

    assert isinstance(deal.merchant_id, UUID)
    assert isinstance(deal.document_id, UUID)
    assert str(deal.merchant_id) == row["merchant_id"]
    assert str(deal.document_id) == row["id"]


def test_row_to_deal_deal_id_round_trips_through_format_deal_id() -> None:
    """The composite deal_id format is ``merchant_id:document_id``
    per ``deals/models.py:format_deal_id``. The mapper must emit
    exactly that shape so ``parse_deal_id`` is the symmetric inverse."""
    row = _full_row()
    deal = _row_to_deal(row)

    merchant_str = str(row["merchant_id"])
    document_str = str(row["id"])
    assert deal.deal_id == f"{merchant_str}:{document_str}"
    assert len(deal.deal_id) == 73  # 36 + 1 + 36


def test_row_to_deal_parses_z_suffix_datetime() -> None:
    """Supabase emits ISO-8601 strings with a trailing ``Z`` for UTC.
    ``_parse_dt`` strips the ``Z`` -> ``+00:00`` before parsing.
    Verify the timezone landed as UTC, not naive."""
    row = _full_row()
    deal = _row_to_deal(row)

    assert deal.created_at == datetime(2026, 6, 3, 18, 41, 58, tzinfo=UTC)
    assert deal.created_at.tzinfo is not None


def test_row_to_deal_accepts_already_typed_datetime() -> None:
    """``_parse_dt`` short-circuits when given a ``datetime`` (some
    supabase-py adapters deserialize timestamptz columns to
    ``datetime`` directly). Mapper must accept both shapes."""
    row = _full_row()
    typed_dt = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
    row["uploaded_at"] = typed_dt
    deal = _row_to_deal(row)

    assert deal.created_at == typed_dt


def test_row_to_deal_business_name_round_trips_verbatim() -> None:
    """business_name is read from the nested merchants block. The
    mapper must NOT trim, uppercase, or otherwise transform it —
    the merchant page is the single source of truth for the canonical
    spelling and the dashboard list must match it character-for-char."""
    row = _full_row()
    merchants_block = row["merchants"]
    assert isinstance(merchants_block, dict)
    merchants_block["business_name"] = "Río Grande Logistics, LLC"
    deal = _row_to_deal(row)

    assert deal.business_name == "Río Grande Logistics, LLC"


def test_row_to_deal_score_recommendation_is_none_until_score_cache_lands() -> None:
    """Documented intentional behavior at ``deals/repository.py:277``.
    The mapper hard-codes ``score_recommendation=None`` because the
    score cache layer hasn't been built — scoring re-runs on demand
    via ``POST /deals/score``. Pin the current behavior so a future
    contributor doesn't silently change it without updating
    ``_INTENTIONALLY_UNHYDRATED_FIELDS`` above."""
    row = _full_row()
    deal = _row_to_deal(row)

    assert deal.score_recommendation is None


# ----------------------------------------------------------------------
# Missing-key defensiveness
# ----------------------------------------------------------------------


def test_row_to_deal_raises_on_missing_id() -> None:
    """``id`` (the document_id) has no default — if the column is
    missing from the row, the SELECT is broken and the mapper must
    raise rather than silently constructing an invalid DealRow."""
    row = _full_row()
    del row["id"]
    with pytest.raises(KeyError):
        _row_to_deal(row)


def test_row_to_deal_raises_on_missing_uploaded_at() -> None:
    """``uploaded_at`` is the only timestamp source — without it the
    mapper cannot produce ``created_at``. KeyError is the intended
    hard failure (the column is NOT NULL on the documents table)."""
    row = _full_row()
    del row["uploaded_at"]
    with pytest.raises(KeyError):
        _row_to_deal(row)


def test_row_to_deal_missing_merchants_block_treats_as_empty() -> None:
    """If the nested ``merchants`` block is missing entirely
    (e.g. a future SELECT variant), the mapper's
    ``row.get("merchants") or {}`` evaluates the block as empty and
    will then KeyError on the ``business_name`` lookup — which is
    the correct hard failure (we cannot construct a DealRow without
    a business_name). Pin the failure mode so a refactor doesn't
    silently swap KeyError for a Pydantic ValidationError."""
    row = _full_row()
    del row["merchants"]
    with pytest.raises(KeyError):
        _row_to_deal(row)
