"""DealRow + deal_id round-trip tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from aegis.deals.models import DealRow, format_deal_id, parse_deal_id


def test_format_then_parse_roundtrips() -> None:
    merchant_id = uuid4()
    document_id = uuid4()
    deal_id = format_deal_id(merchant_id, document_id)

    parsed_merchant, parsed_document = parse_deal_id(deal_id)
    assert parsed_merchant == merchant_id
    assert parsed_document == document_id


def test_format_uses_canonical_uuid_form() -> None:
    merchant_id = UUID("11111111-2222-3333-4444-555555555555")
    document_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    deal_id = format_deal_id(merchant_id, document_id)
    assert deal_id == (
        "11111111-2222-3333-4444-555555555555:"
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    )
    # The composite is exactly 73 chars (36 + 1 + 36).
    assert len(deal_id) == 73


def test_parse_rejects_too_few_parts() -> None:
    with pytest.raises(ValueError, match="merchant_id:document_id"):
        parse_deal_id("just-one-part")


def test_parse_rejects_too_many_parts() -> None:
    m = str(uuid4())
    d = str(uuid4())
    with pytest.raises(ValueError, match="merchant_id:document_id"):
        parse_deal_id(f"{m}:{d}:extra")


def test_parse_rejects_malformed_uuid() -> None:
    with pytest.raises(ValueError, match="malformed UUID"):
        parse_deal_id(f"not-a-uuid:{uuid4()}")


def test_deal_row_validates_deal_id_format() -> None:
    """DealRow with a deal_id whose components don't match should fail.

    Construction-time validation catches the case where a caller
    hand-builds a DealRow with mismatched ids.
    """
    merchant_id = uuid4()
    document_id = uuid4()
    deal_id = format_deal_id(merchant_id, document_id)

    row = DealRow(
        deal_id=deal_id,
        merchant_id=merchant_id,
        document_id=document_id,
        created_at=datetime.now(UTC),
        business_name="Acme",
        state="CA",
        parse_status="proceed",
        fraud_score=15,
    )
    assert row.deal_id == deal_id
    # state normalized to upper
    assert row.state == "CA"


def test_deal_row_rejects_bad_deal_id_length() -> None:
    """Pydantic's min_length / max_length on deal_id rejects garbage early."""
    with pytest.raises(ValidationError):
        DealRow(
            deal_id="too-short",
            merchant_id=uuid4(),
            document_id=uuid4(),
            created_at=datetime.now(UTC),
            business_name="Acme",
            state="CA",
            parse_status="proceed",
        )


def test_deal_row_rejects_unknown_parse_status() -> None:
    merchant_id = uuid4()
    document_id = uuid4()
    with pytest.raises(ValidationError):
        DealRow(
            deal_id=format_deal_id(merchant_id, document_id),
            merchant_id=merchant_id,
            document_id=document_id,
            created_at=datetime.now(UTC),
            business_name="Acme",
            state="CA",
            parse_status="unrecognized",
        )


def test_deal_row_normalizes_state_upper() -> None:
    merchant_id = uuid4()
    document_id = uuid4()
    row = DealRow(
        deal_id=format_deal_id(merchant_id, document_id),
        merchant_id=merchant_id,
        document_id=document_id,
        created_at=datetime.now(UTC),
        business_name="Acme",
        state="ca",
        parse_status="proceed",
    )
    assert row.state == "CA"
