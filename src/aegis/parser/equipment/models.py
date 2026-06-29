"""Pydantic models for the equipment invoice / quote parser.

Money fields use ``Annotated[Decimal, Field(max_digits=14, decimal_places=2)]``
so the model rejects ``float`` at the boundary (CLAUDE.md money rule).
The Bedrock tool-use call returns ``total_cost`` as a Decimal-safe
string; the extractor parses it through ``Decimal`` before constructing
the model so a malformed value fails fast rather than silently
degrading the dossier.

Column shape mirrors the migration-095 ``merchants.equipment_details``
JSONB column — the merchant repository serialises this Pydantic model
into that JSONB blob via ``model_dump(mode="json")``. Adding a new
optional field here requires no migration; the JSONB column carries
whatever the model emits.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# Condition discriminator. Matches the funder-facing equipment-financing
# convention (Maxim Commercial Capital / Pawnee / etc. all price new
# vs. used vs. refurbished separately). ``None`` when the quote does
# not state the condition — the operator can fill it in manually on the
# dossier, but the extractor never guesses (CLAUDE.md operating
# principle 4: empty is better than wrong).
EquipmentCondition = Literal["new", "used", "refurbished"]


# Money columns mirror the merchants table convention. ``total_cost`` is
# NOT NULL on the extraction (an equipment quote without a price is not
# extractable — the extractor returns no result rather than a zero) so
# this is a plain ``Decimal`` field, not ``Decimal | None``.
_MoneyField = Annotated[Decimal, Field(max_digits=14, decimal_places=2)]


class EquipmentInvoiceResult(BaseModel):
    """Validated extraction payload for one equipment quote / invoice PDF.

    Carries every field the migration-095 ``equipment_details`` JSONB
    column holds. ``description`` + ``total_cost`` are the only
    required fields; the rest are ``None`` when the quote / invoice
    doesn't state them.

    VIN validation: a 17-character VIN with no I / O / Q is the
    canonical North American standard. The extractor's sanitiser drops
    any VIN that doesn't match — see ``aegis.parser.equipment.extract.
    _coerce_vin``.

    Serialisation: ``model_dump(mode="json")`` produces a JSON-safe
    dict (Decimal -> str via the field validator + json mode,
    datetime -> ISO 8601 string) suitable for direct insertion into
    the JSONB column via supabase-py.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    description: str = Field(min_length=1)
    make: str | None = None
    model: str | None = None
    year: int | None = Field(default=None, ge=1900, le=2100)
    condition: EquipmentCondition | None = None
    serial_number: str | None = None
    vin: str | None = None
    vendor_name: str | None = None
    total_cost: _MoneyField
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


__all__ = ["EquipmentCondition", "EquipmentInvoiceResult"]
