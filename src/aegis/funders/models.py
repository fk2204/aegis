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
from typing import Annotated, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aegis.money import Money


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )


class FunderTier(_StrictModel):
    """One tier in a funder's underwriting matrix (Elite / A / B / C-style).

    A funder's `tiers` is an ordered tuple. Convention is most-permissive-
    first → most-restrictive-last (Elite before A before B before C), but
    this is not enforced — some funders publish tiers that don't fit a
    strict ordering.

    Numeric fields are `None` when the tier does NOT constrain on that
    axis. Missing is not the same as zero or unlimited.
    """

    name: str = Field(min_length=1)
    buy_rate_low: Decimal | None = None
    buy_rate_high: Decimal | None = None
    min_months_in_business: Annotated[int, Field(ge=0)] | None = None
    min_credit_score: Annotated[int, Field(ge=300, le=850)] | None = None
    min_monthly_revenue: Money | None = None
    max_positions: Annotated[int, Field(ge=0)] | None = None
    max_advance: Money | None = None
    # Decimal value as fraction, e.g. Decimal("0.15") for 15%.
    # Not Decimal("15") (which would be 1500%).
    max_holdback: Decimal | None = None

    @model_validator(mode="after")
    def _buy_rate_low_le_high(self) -> Self:
        if (
            self.buy_rate_low is not None
            and self.buy_rate_high is not None
            and self.buy_rate_low > self.buy_rate_high
        ):
            raise ValueError(
                f"tier {self.name!r}: buy_rate_low ({self.buy_rate_low}) "
                f"must be <= buy_rate_high ({self.buy_rate_high})"
            )
        return self


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

    # Product / velocity / preference policy (migration 056). All three
    # consumed by ``match_funder``:
    #
    # * ``deal_types_accepted``: e.g. ``("mca",)`` or ``("mca", "loc",
    #   "term_loan")``. Empty tuple = "no constraint" (legacy
    #   behaviour). The matcher hard-fails when this is non-empty AND
    #   the deal's type isn't in the list.
    # * ``funding_velocity_days``: business days from clean submission
    #   to decision. ``None`` = "not published". Drives an
    #   ASAP-urgency soft concern in ``match_funder`` when the
    #   merchant's Close Urgency is ``ASAP (24-48 hours)`` and the
    #   funder takes > 2 business days.
    # * ``preferred_states``: USPS two-letter codes the funder
    #   prefers (informational, not exclusion). Empty = "no
    #   preference". When non-empty AND the merchant's state isn't
    #   in the list, the matcher emits a soft concern. Distinct
    #   from ``excluded_states`` which hard-fails.
    deal_types_accepted: tuple[str, ...] = ()
    funding_velocity_days: Annotated[int, Field(ge=0)] | None = None
    preferred_states: tuple[str, ...] = ()

    # Provenance — when did the latest guideline extraction run, and
    # against which PDF? Lets us re-run extraction only when the funder
    # publishes a new criteria sheet.
    guidelines_extracted_at: datetime | None = None
    guidelines_source_pdf_hash: str | None = None

    # Contact info — surfaced as a card at the top of the funder detail
    # page. Submission_email is the address used for sending deals;
    # contact_email is the relationship address (may be the same person).
    contact_name: str = ""
    contact_phone: str = ""
    contact_email: str = ""
    submission_email: str = ""

    # Underwriting tiers. Empty tuple means this funder has not yet been
    # re-extracted into the structured format — notes prose remains the
    # source of truth until backfilled via per-funder re-extraction.
    tiers: tuple[FunderTier, ...] = ()

    # Bullet-list fields extracted alongside tiers.
    #   auto_decline_conditions: absolute disqualifiers.
    #   conditional_requirements: "OK if these documents/conditions are met".
    auto_decline_conditions: tuple[str, ...] = ()
    conditional_requirements: tuple[str, ...] = ()

    # Operator-authored commentary. Empty after a fresh extraction;
    # populated only when an operator adds context via the review UI.
    notes: str = ""

    # Extraction residual — prose the LLM recognised as relevant but
    # could not slot into a structured field. Written by extract.py;
    # the operator decides whether to promote it to a schema field.
    notes_residual: str = ""

    # Operator-authored commentary. Survives re-extraction (the
    # /ui/funders/{id}/reextract route never touches this field).
    # Distinct from `notes` (legacy field being phased out) and
    # `notes_residual` (extractor-authored prose).
    operator_notes: str = ""


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
    "deal_types_accepted",
    "funding_velocity_days",
    "preferred_states",
    # Added in step C — extraction targets for the redesigned detail page.
    # Tiers is array-level (not per-tier or per-tier-field) for prompt
    # simplicity; operator drills into individual tiers in the review UI.
    "contact_name",
    "contact_phone",
    "contact_email",
    "submission_email",
    "tiers",
    "auto_decline_conditions",
    "conditional_requirements",
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


__all__ = ["FunderGuidelineExtraction", "FunderRow", "FunderTier"]
