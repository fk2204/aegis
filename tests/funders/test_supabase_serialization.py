"""Unit tests for the row<->FunderRow transforms used by SupabaseFunderRepository.

We don't hit a real Supabase here — just verify shape preservation,
especially the JSONB `tiers` list and Decimal precision through the
JSON round-trip. The transforms are pure functions; this is sufficient
to cover both directions.

Coverage:
  * every field roundtrips, including the two fields the pre-redesign
    repo silently dropped (aegis_compensation_disclosure_text,
    charges_merchant_advance_fees)
  * tiers JSONB list roundtrips with Decimal precision intact
  * empty/missing keys defensively map to defaults
  * payload tiers are json.dumps-serializable
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from aegis.funders.models import FunderRow, FunderTier
from aegis.funders.repository import _funder_to_payload, _row_to_funder


def _fully_populated_funder() -> FunderRow:
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
        aegis_compensation_disclosure_text="AEGIS receives a commission of …",
        charges_merchant_advance_fees=False,
        typical_factor_low=Decimal("1.2500"),
        typical_factor_high=Decimal("1.3000"),
        typical_holdback_low=Decimal("0.1000"),
        typical_holdback_high=Decimal("0.1500"),
        excluded_industries=("cannabis", "adult"),
        excluded_states=("NV", "SD"),
        guidelines_extracted_at=datetime(2026, 4, 12, 10, 30, tzinfo=UTC),
        guidelines_source_pdf_hash="abc123",
        contact_name="James Doe",
        contact_phone="555-123-4567",
        contact_email="james@logicadvance.com",
        submission_email="iso@logicadvance.com",
        tiers=(
            FunderTier(
                name="Elite",
                buy_rate_low=Decimal("1.2500"),
                buy_rate_high=Decimal("1.3000"),
                min_monthly_revenue=Decimal("100000.00"),
                min_credit_score=700,
                min_months_in_business=60,
                max_positions=1,
                max_advance=Decimal("1500000.00"),
                max_holdback=Decimal("0.15"),
            ),
            FunderTier(name="A", buy_rate_low=Decimal("1.2845")),
        ),
        auto_decline_conditions=(
            "Restaurants with <12 mo TIB",
            "Active tax liens > $25K",
        ),
        conditional_requirements=(
            "Trucking: 2 yr MVR clean",
        ),
        notes="Operator: prefer for Tier-A trucking deals.",
        notes_residual="residual prose that did not parse into structured fields",
        operator_notes="Rep prefers WhatsApp; ping before sending stacked deals.",
    )


def _payload_to_row_dict(payload: dict[str, Any]) -> dict[str, Any]:
    """Simulate what Supabase returns: same dict shape, no id mutation."""
    return dict(payload)


def test_full_round_trip_preserves_every_field() -> None:
    original = _fully_populated_funder()
    payload = _funder_to_payload(original)
    row = _payload_to_row_dict(payload)
    restored = _row_to_funder(row)

    # updated_at is set fresh in the payload — not on FunderRow, so
    # roundtrip equality is on the model fields only.
    assert restored.model_dump() == original.model_dump()


def test_previously_dropped_fields_now_roundtrip() -> None:
    f = _fully_populated_funder()
    payload = _funder_to_payload(f)
    assert payload["aegis_compensation_disclosure_text"] == (
        "AEGIS receives a commission of …"
    )
    assert payload["charges_merchant_advance_fees"] is False
    restored = _row_to_funder(_payload_to_row_dict(payload))
    assert restored.aegis_compensation_disclosure_text == (
        "AEGIS receives a commission of …"
    )
    assert restored.charges_merchant_advance_fees is False


def test_tier_decimal_precision_preserved_through_payload() -> None:
    f = _fully_populated_funder()
    payload = _funder_to_payload(f)
    # Tiers serialize to JSON-safe dicts; Decimal becomes string.
    assert payload["tiers"][1]["buy_rate_low"] == "1.2845"
    restored = _row_to_funder(_payload_to_row_dict(payload))
    assert restored.tiers[1].buy_rate_low == Decimal("1.2845")
    assert restored.tiers[0].max_holdback == Decimal("0.15")


def test_payload_tiers_are_json_serializable() -> None:
    f = _fully_populated_funder()
    payload = _funder_to_payload(f)
    # json.dumps must not raise — Supabase REST layer will JSON-encode
    # the body before POSTing.
    encoded = json.dumps(payload["tiers"])
    assert "Elite" in encoded
    # Decimal must not have leaked through as a non-JSON type.
    decoded = json.loads(encoded)
    assert decoded[0]["buy_rate_low"] == "1.2500"


def test_missing_new_columns_default_cleanly() -> None:
    # Simulate a row that somehow lacks the new columns (e.g. a stale
    # Supabase cache hit pre-migration). _row_to_funder must defaults
    # cleanly rather than KeyError-ing or producing None where the model
    # requires a value.
    row: dict[str, Any] = {
        "id": str(uuid4()),
        "name": "Sparse Funder",
        # all new fields absent
    }
    restored = _row_to_funder(row)
    assert restored.contact_name == ""
    assert restored.contact_phone == ""
    assert restored.contact_email == ""
    assert restored.submission_email == ""
    assert restored.tiers == ()
    assert restored.auto_decline_conditions == ()
    assert restored.conditional_requirements == ()
    assert restored.aegis_compensation_disclosure_text == ""
    assert restored.charges_merchant_advance_fees is False
    assert restored.notes == ""
    assert restored.notes_residual == ""
    assert restored.operator_notes == ""


def test_empty_tiers_payload_is_empty_list() -> None:
    f = _fully_populated_funder().model_copy(update={"tiers": ()})
    payload = _funder_to_payload(f)
    assert payload["tiers"] == []
    restored = _row_to_funder(_payload_to_row_dict(payload))
    assert restored.tiers == ()
