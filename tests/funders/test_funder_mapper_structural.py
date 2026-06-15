"""Structural schema-coverage tests for funder mappers (H1 pattern).

Companion to ``tests/funders/test_supabase_serialization.py`` (the
field-by-field round-trip suite that pre-existed). This file owns the
structural guard: introspect ``FunderRow.model_fields`` and
``FunderTier.model_fields`` and assert every declared field on each
model lands non-None on the model when the Supabase-shape row carries
a non-None value.

Why this exists separately:
  * The pre-existing suite asserts ``original.model_dump() ==
    restored.model_dump()`` on a fully-populated funder. That catches
    drops on the round-trip, but does NOT detect a new field that
    defaults to ``None`` / empty on BOTH the input and the output
    (silent symmetric drift) — a future column add that updates the
    model but neither mapper would slip through.
  * The H1 pattern (introspect model_fields + fully-populated row +
    non-None assertion) is structurally stronger: any new field added
    to ``FunderRow`` or ``FunderTier`` without the matching mapper
    write/read pair fails this test.

The mappers themselves are NOT modified by this test file. If the
structural assertion exposes a real bug, the test marks it via
``# REPORT:`` and xfails so the gate stays green.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from aegis.funders.models import FunderRow, FunderTier
from aegis.funders.repository import _funder_to_payload, _row_to_funder


def _fully_populated_funder() -> FunderRow:
    """Mirrors the fixture in ``test_supabase_serialization.py`` but
    kept local so a refactor to that file doesn't silently weaken
    this structural test. Every ``FunderRow`` field is set to a
    non-default value."""
    return FunderRow(
        id=uuid4(),
        name="Logic Advance Group",
        active=True,
        min_monthly_revenue=Decimal("100000.00"),
        min_avg_daily_balance=Decimal("12000.00"),
        min_credit_score=680,
        min_months_in_business=24,
        max_positions=2,
        accepts_stacking=False,
        min_advance=Decimal("25000.00"),
        max_advance=Decimal("1500000.00"),
        max_nsf_tolerance=3,
        requires_coj=True,
        aegis_compensation_disclosure_text="AEGIS receives 8% commission",
        charges_merchant_advance_fees=False,
        typical_factor_low=Decimal("1.2500"),
        typical_factor_high=Decimal("1.3000"),
        typical_holdback_low=Decimal("0.1000"),
        typical_holdback_high=Decimal("0.1500"),
        excluded_industries=("cannabis", "adult"),
        excluded_states=("NV", "SD"),
        deal_types_accepted=("mca", "term_loan"),
        funding_velocity_days=3,
        preferred_states=("CA", "TX"),
        guidelines_extracted_at=datetime(2026, 4, 12, 10, 30, tzinfo=UTC),
        guidelines_source_pdf_hash="abc123",
        contact_name="James Doe",
        contact_phone="555-123-4567",
        contact_email="james@logicadvance.com",
        submission_email="iso@logicadvance.com",
        tiers=(_fully_populated_tier(),),
        auto_decline_conditions=("Restaurants with <12 mo TIB",),
        conditional_requirements=("Trucking: 2 yr MVR clean",),
        notes="Operator: prefer for Tier-A trucking deals.",
        notes_residual="residual prose that did not parse",
        operator_notes="Rep prefers WhatsApp before stacked deals.",
    )


def _fully_populated_tier() -> FunderTier:
    """Every ``FunderTier`` field set to a non-default value."""
    return FunderTier(
        name="Elite",
        buy_rate_low=Decimal("1.2500"),
        buy_rate_high=Decimal("1.3000"),
        min_months_in_business=60,
        min_credit_score=700,
        min_monthly_revenue=Decimal("100000.00"),
        max_positions=1,
        max_advance=Decimal("1500000.00"),
        max_holdback=Decimal("0.15"),
    )


def _payload_to_row_dict(payload: dict[str, Any]) -> dict[str, Any]:
    """Simulate what Supabase returns: same dict shape, no id mutation."""
    return dict(payload)


# ----------------------------------------------------------------------
# FunderRow structural coverage
# ----------------------------------------------------------------------


def test_row_to_funder_hydrates_every_field_on_funder_row() -> None:
    """STRUCTURAL GUARD. Introspects ``FunderRow.model_fields`` and
    asserts every declared field lands non-None on a fully-populated
    round-trip.

    This is the H1 pattern. The existing
    ``test_full_round_trip_preserves_every_field`` test asserts
    ``model_dump() == model_dump()`` which catches asymmetric drops
    but NOT symmetric ones (a field that defaults to None on both
    sides of the round-trip). This test catches both.

    Don't relax this test. Add ignore-fields only with a documented
    reason in this module's docstring."""
    funder = _fully_populated_funder()
    payload = _funder_to_payload(funder)
    row = _payload_to_row_dict(payload)
    restored = _row_to_funder(row)

    declared_fields = set(FunderRow.model_fields.keys())
    for field_name in declared_fields:
        original_value = getattr(funder, field_name)
        restored_value = getattr(restored, field_name)

        if original_value is None or original_value == "" or original_value == ():
            # The fixture didn't carry a meaningful value — default OK.
            continue

        assert restored_value is not None, (
            f"Mapper dropped field {field_name!r}: original carried "
            f"{original_value!r} but FunderRow came out None after "
            "round-trip. Update _row_to_funder and/or _funder_to_payload "
            "in src/aegis/funders/repository.py."
        )

        # Also check the value survived intact (catches truthy-but-empty
        # symmetric drops where e.g. an empty tuple round-trips but a
        # populated tuple is silently truncated).
        assert restored_value == original_value, (
            f"Field {field_name!r} drifted through the round-trip: "
            f"original={original_value!r} restored={restored_value!r}. "
            "Investigate the mapper pair."
        )


def test_funder_row_model_fields_match_known_set() -> None:
    """Tripwire: pin the declared ``FunderRow`` field set. A new field
    added here must be added to both mappers AND to the structural
    test's fixture. This test fails until all three move together."""
    expected = {
        "id",
        "name",
        "active",
        "min_monthly_revenue",
        "min_avg_daily_balance",
        "min_credit_score",
        "min_months_in_business",
        "max_positions",
        "accepts_stacking",
        "min_advance",
        "max_advance",
        "max_nsf_tolerance",
        "requires_coj",
        "aegis_compensation_disclosure_text",
        "charges_merchant_advance_fees",
        "typical_factor_low",
        "typical_factor_high",
        "typical_holdback_low",
        "typical_holdback_high",
        "excluded_industries",
        "excluded_states",
        "deal_types_accepted",
        "funding_velocity_days",
        "preferred_states",
        "guidelines_extracted_at",
        "guidelines_source_pdf_hash",
        "contact_name",
        "contact_phone",
        "contact_email",
        "submission_email",
        "tiers",
        "auto_decline_conditions",
        "conditional_requirements",
        "notes",
        "notes_residual",
        "operator_notes",
    }
    assert set(FunderRow.model_fields.keys()) == expected


def test_funder_to_payload_emits_every_funder_row_field_as_a_payload_key() -> None:
    """Verify the write half: every ``FunderRow`` field has a
    corresponding key in the payload dict. ``updated_at`` is mapper-
    injected and not a model field, so it lives outside the set
    being checked."""
    funder = _fully_populated_funder()
    payload = _funder_to_payload(funder)

    declared = set(FunderRow.model_fields.keys())
    payload_keys = set(payload.keys())
    missing = declared - payload_keys
    assert missing == set(), (
        f"_funder_to_payload omits FunderRow field(s) {missing!r}. "
        "A model field with no payload key cannot survive the round-"
        "trip; update src/aegis/funders/repository.py:_funder_to_payload."
    )
    # And the mapper-injected updated_at is present (not a model
    # field, but a documented payload column).
    assert "updated_at" in payload


# ----------------------------------------------------------------------
# FunderTier structural coverage (the inner mapper)
# ----------------------------------------------------------------------


def test_funder_tier_round_trips_every_field_through_payload() -> None:
    """STRUCTURAL GUARD for the tier sub-mapper.

    ``FunderTier`` round-trips through:
      * write: ``t.model_dump(mode="json")`` inside
        ``_funder_to_payload`` (repository.py:216)
      * read: ``FunderTier.model_validate(t)`` inside
        ``_row_to_funder`` (repository.py:170)

    Both calls are generic Pydantic — a new field added to
    ``FunderTier`` would not silently drop on the round-trip, but
    a future refactor that swaps either side for hand-rolled
    field selection WOULD. Pin every-field-non-None here as
    insurance against that refactor.
    """
    tier = _fully_populated_tier()
    funder = _fully_populated_funder().model_copy(update={"tiers": (tier,)})
    payload = _funder_to_payload(funder)
    restored = _row_to_funder(_payload_to_row_dict(payload))

    assert len(restored.tiers) == 1
    restored_tier = restored.tiers[0]

    declared_tier_fields = set(FunderTier.model_fields.keys())
    for field_name in declared_tier_fields:
        original_value = getattr(tier, field_name)
        restored_value = getattr(restored_tier, field_name)

        if original_value is None:
            continue

        assert restored_value is not None, (
            f"FunderTier round-trip dropped field {field_name!r}: "
            f"original={original_value!r}. Investigate the tier "
            "serialization path in src/aegis/funders/repository.py."
        )
        assert restored_value == original_value, (
            f"FunderTier field {field_name!r} drifted: "
            f"original={original_value!r} restored={restored_value!r}."
        )


def test_funder_tier_model_fields_match_known_set() -> None:
    """Tripwire: pin the declared ``FunderTier`` field set."""
    expected = {
        "name",
        "buy_rate_low",
        "buy_rate_high",
        "min_months_in_business",
        "min_credit_score",
        "min_monthly_revenue",
        "max_positions",
        "max_advance",
        "max_holdback",
    }
    assert set(FunderTier.model_fields.keys()) == expected


# ----------------------------------------------------------------------
# Tier sub-payload shape — confirm json-serialized dicts use the
# documented Pydantic field names (not aliases, not snake_case
# transformations introduced by a future model_config edit).
# ----------------------------------------------------------------------


def test_funder_tier_payload_dict_keys_are_pydantic_field_names() -> None:
    """The funder REST layer JSON-encodes the tiers list verbatim
    (``json.dumps(payload["tiers"])`` per
    ``tests/funders/test_supabase_serialization.py::test_payload_tiers_are_json_serializable``).
    The dict keys inside each tier must therefore be the model's
    declared field names — a future ``alias_generator`` or
    ``populate_by_name=True`` config edit that emits camelCase or
    abbreviated keys would silently break the read side."""
    funder = _fully_populated_funder()
    payload = _funder_to_payload(funder)

    assert isinstance(payload["tiers"], list)
    assert len(payload["tiers"]) == 1
    tier_dict = payload["tiers"][0]
    assert isinstance(tier_dict, dict)

    declared = set(FunderTier.model_fields.keys())
    tier_keys = set(tier_dict.keys())
    # Every model field must appear as a key (model_dump emits all
    # fields by default, including those at their None default).
    missing = declared - tier_keys
    assert missing == set(), (
        f"FunderTier.model_dump(mode='json') is missing keys {missing!r}. "
        "Either the model_config has changed (alias_generator?) or "
        "model_dump default exclusions have shifted. Investigate before "
        "shipping."
    )


# ----------------------------------------------------------------------
# Money / Decimal precision through Supabase JSON round-trip
# ----------------------------------------------------------------------


def test_money_fields_survive_string_round_trip_with_full_precision() -> None:
    """Decimal money fields are stringified by ``_funder_to_payload``
    (e.g. ``min_monthly_revenue -> "100000.00"``) and re-coerced by
    ``_row_to_funder._money`` on the way back. Pin the precision so a
    refactor that drops to ``float(val)`` (which would lose cents on
    larger numbers) trips this test."""
    funder = _fully_populated_funder().model_copy(
        update={
            "min_monthly_revenue": Decimal("123456.78"),
            "max_advance": Decimal("999999999.99"),
            "typical_factor_low": Decimal("1.234567"),
        }
    )
    payload = _funder_to_payload(funder)
    restored = _row_to_funder(_payload_to_row_dict(payload))

    assert restored.min_monthly_revenue == Decimal("123456.78")
    assert restored.max_advance == Decimal("999999999.99")
    assert restored.typical_factor_low == Decimal("1.234567")


def test_datetime_z_suffix_round_trip_for_guidelines_extracted_at() -> None:
    """``guidelines_extracted_at`` round-trips through ``isoformat()``
    on the write side and ``datetime.fromisoformat`` (with ``Z`` ->
    ``+00:00`` swap) on the read side. Confirm UTC preservation."""
    extracted = datetime(2026, 4, 12, 10, 30, 45, tzinfo=UTC)
    funder = _fully_populated_funder().model_copy(update={"guidelines_extracted_at": extracted})
    payload = _funder_to_payload(funder)
    restored = _row_to_funder(_payload_to_row_dict(payload))

    assert restored.guidelines_extracted_at == extracted
    assert restored.guidelines_extracted_at is not None
    assert restored.guidelines_extracted_at.tzinfo is not None


def test_row_to_funder_handles_z_suffix_string_for_guidelines_extracted_at() -> None:
    """A row that comes back from supabase-py with a ``Z``-suffix
    string (not an already-typed datetime) must parse cleanly via
    the ``replace('Z', '+00:00')`` path inside ``_row_to_funder._dt``."""
    funder = _fully_populated_funder()
    payload = _funder_to_payload(funder)
    payload["guidelines_extracted_at"] = "2026-04-12T10:30:45Z"
    restored = _row_to_funder(_payload_to_row_dict(payload))

    assert restored.guidelines_extracted_at == datetime(2026, 4, 12, 10, 30, 45, tzinfo=UTC)
