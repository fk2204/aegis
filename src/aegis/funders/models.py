"""Funder data shapes.

`FunderRow` is the authoritative funder model — Pydantic-strict, used by
the matcher, the repository, and the guideline extractor. It replaces the
local dataclass that `scoring/match_funders.py` declared during Phase 3.

`FunderGuidelineExtraction` is the LLM-side output: a draft `FunderRow`
plus per-field confidence and any unparseable fragments. The operator
reviews low-confidence fields before persisting.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from aegis.money import Money


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )


class FunderRow(_StrictModel):
    """One funder's underwriting criteria. Mirrors the funders Postgres table.

    Optional fields with `None` mean "no policy specified" — a missing
    policy is NOT the same as a permissive one. Match_funder treats
    missing criteria as "this funder did not constrain on that axis".
    """

    id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1)
    active: bool = True

    # Hard gates
    min_monthly_revenue: Money | None = None
    min_avg_daily_balance: Money | None = None
    min_credit_score: Annotated[int, Field(ge=300, le=850)] | None = None
    min_months_in_business: Annotated[int, Field(ge=0)] | None = None
    max_positions: Annotated[int, Field(ge=0)] | None = None
    accepts_stacking: bool = False
    min_advance: Money | None = None
    max_advance: Money | None = None
    max_nsf_tolerance: Annotated[int, Field(ge=0)] | None = None
    # True = funder agreement requires a confession of judgment (CoJ).
    # Triggers a hard-decline at match time for merchants in any state
    # whose StateRegulation.coj_allowed="banned" (e.g. California per
    # Cal. Code Civ. Proc. § 1132 — see docs/compliance/01_california.md).
    # NY (coj_allowed="conditional") permits CoJ only against NY-resident
    # merchants — soft warning rather than hard decline.
    requires_coj: bool = False

    # Per docs/compliance/02_new_york.md: 23 NYCRR § 600.21(f) requires the
    # provider (funder) to inform the recipient in writing how, and by
    # whom, the broker is compensated. AEGIS is the broker. AEGIS supplies
    # the funder with a standard text block describing AEGIS's
    # compensation arrangement with that funder; the funder includes it
    # when transmitting the disclosure. This text is per-funder because
    # the arrangement (commission %, ISO fee structure) varies by funder.
    # Empty string means "not yet supplied" — disclosure generation for an
    # NY merchant requires this to be non-empty.
    aegis_compensation_disclosure_text: str = ""

    # Per docs/compliance/03_florida.md: Fla. Stat. § 559.9614(1)(a)
    # prohibits brokers from assessing, collecting, or soliciting an
    # advance fee from a merchant for brokering services (with a narrow
    # carve-out for actual third-party services like credit checks paid
    # to an independent third party). AEGIS's standard practice is broker
    # commission paid by the funder, never advance fees from merchants —
    # default False. A funder whose ISO contract requires merchant-side
    # advance fees must have this flipped True; the matcher then hard-
    # fails any pairing with an FL merchant (or any state with
    # broker_advance_fees_prohibited=True), parallel to the CoJ rule.
    charges_merchant_advance_fees: bool = False

    # Pricing envelope (informational; matcher doesn't enforce)
    typical_factor_low: Decimal | None = None
    typical_factor_high: Decimal | None = None
    typical_holdback_low: Decimal | None = None
    typical_holdback_high: Decimal | None = None

    # Exclusions
    excluded_industries: tuple[str, ...] = ()
    excluded_states: tuple[str, ...] = ()

    # Provenance — when did the latest guideline extraction run, and
    # against which PDF? Lets us re-run extraction only when the funder
    # publishes a new criteria sheet.
    guidelines_extracted_at: datetime | None = None
    guidelines_source_pdf_hash: str | None = None

    # Free-form notes (not used by the matcher).
    notes: str = ""


_FIELD_CONFIDENCE_KEYS: tuple[str, ...] = (
    "min_monthly_revenue",
    "min_avg_daily_balance",
    "min_credit_score",
    "min_months_in_business",
    "max_positions",
    "accepts_stacking",
    "min_advance",
    "max_advance",
    "max_nsf_tolerance",
    "typical_factor_low",
    "typical_factor_high",
    "typical_holdback_low",
    "typical_holdback_high",
    "excluded_industries",
    "excluded_states",
)


class FunderGuidelineExtraction(_StrictModel):
    """LLM-side output of `extract_funder_guidelines`.

    `draft` is a partially-populated FunderRow. `confidence_by_field` maps
    every field name in `_FIELD_CONFIDENCE_KEYS` to a 0..100 score; the
    operator UI sorts low-confidence fields to the top for review.
    `unparseable_fragments` captures text the LLM could not categorize —
    operators decide whether to add new schema fields.
    """

    draft: FunderRow
    confidence_by_field: dict[str, int] = Field(default_factory=dict)
    unparseable_fragments: list[str] = Field(default_factory=list)
    overall_confidence: Annotated[int, Field(ge=0, le=100)] = 0

    @classmethod
    def confidence_keys(cls) -> tuple[str, ...]:
        return _FIELD_CONFIDENCE_KEYS


__all__ = ["FunderGuidelineExtraction", "FunderRow"]
