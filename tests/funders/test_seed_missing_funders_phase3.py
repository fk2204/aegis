"""Phase 3 / section 8.2 — seed_missing_funders.py contract.

Section 8.2 of ``docs/AEGIS_COMPLETE_BUILD_PLAN.md`` calls out six
funders that must land in the catalog:

  * Rapid Finance
  * Kapitus
  * Fora Financial
  * Credibly
  * Expansion Capital Group
  * Forward Financing

The seed source is ``scripts/seed_missing_funders.py``, which is
idempotent via name-based skip and writes through
``FunderRepository.upsert``. This test pins the section 8.2 contract
to that script's data so the six entries:

  * stay present (no accidental removal),
  * parse cleanly into ``FunderRow`` (Pydantic-strict, no
    ValidationError), and
  * carry the underwriting criteria the matcher reads
    (``min_monthly_revenue``, ``min_credit_score``,
    ``min_months_in_business``, ``max_positions``,
    ``accepts_stacking``, ``deal_types_accepted``) with the values
    the operator specified in the section 8.2 brief.

Distinct from ``test_seed_migration_present.py`` (validates the
SQL seed migration 035, a different surface).
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from typing import cast

from scripts.seed_missing_funders import _MISSING_FUNDERS

from aegis.funders.models import FunderRow

# Brief-locked contract. Each entry is the canonical name plus the
# criteria the matcher reads. ``deal_types_accepted`` is a SET
# (operator-curated tokens; matcher maps via ``_DEAL_TYPE_TO_PRODUCT``)
# so the test treats order as insignificant.
_SECTION_8_2_CONTRACT: tuple[dict[str, object], ...] = (
    {
        "name": "Rapid Finance",
        "min_monthly_revenue": Decimal("10000"),
        "min_credit_score": 550,
        "min_months_in_business": 12,
        "max_positions": 3,
        "accepts_stacking": True,
        "deal_types_accepted": frozenset({"mca", "term_loan", "loc"}),
    },
    {
        "name": "Kapitus",
        "min_monthly_revenue": Decimal("25000"),
        "min_credit_score": 625,
        "min_months_in_business": 24,
        "max_positions": 2,
        "accepts_stacking": True,
        "deal_types_accepted": frozenset({"mca", "term_loan", "equipment_financing"}),
    },
    {
        "name": "Fora Financial",
        "min_monthly_revenue": Decimal("12500"),
        "min_credit_score": 500,
        "min_months_in_business": 6,
        "max_positions": 4,
        "accepts_stacking": True,
        "deal_types_accepted": frozenset({"mca"}),
    },
    {
        "name": "Credibly",
        "min_monthly_revenue": Decimal("15000"),
        "min_credit_score": 500,
        "min_months_in_business": 6,
        "max_positions": 3,
        "accepts_stacking": True,
        "deal_types_accepted": frozenset({"mca", "term_loan", "loc"}),
    },
    {
        "name": "Expansion Capital Group",
        "min_monthly_revenue": Decimal("10000"),
        "min_credit_score": 500,
        "min_months_in_business": 6,
        "max_positions": 5,
        "accepts_stacking": True,
        "deal_types_accepted": frozenset({"mca"}),
    },
    {
        "name": "Forward Financing",
        "min_monthly_revenue": Decimal("10000"),
        "min_credit_score": 500,
        "min_months_in_business": 6,
        "max_positions": 3,
        "accepts_stacking": True,
        "deal_types_accepted": frozenset({"mca"}),
    },
)


def _spec_by_name(name: str) -> dict[str, object]:
    """Return the ``_MISSING_FUNDERS`` spec dict matching ``name``."""
    matches = [s for s in _MISSING_FUNDERS if s["name"] == name]
    assert len(matches) == 1, (
        f"Expected exactly one seed entry for {name!r}; "
        f"got {len(matches)}. Section 8.2 names must be unique."
    )
    return matches[0]


def test_all_six_section_8_2_funders_are_seeded() -> None:
    """Every funder named in section 8.2 must appear in the seed list."""
    seeded_names = {str(s["name"]) for s in _MISSING_FUNDERS}
    expected_names = {str(row["name"]) for row in _SECTION_8_2_CONTRACT}
    missing = expected_names - seeded_names
    assert not missing, (
        f"Section 8.2 funders missing from seed_missing_funders.py: "
        f"{sorted(missing)}. Add them or update the contract."
    )


def test_section_8_2_funders_parse_as_funder_row() -> None:
    """Each of the six entries must instantiate FunderRow cleanly.

    No Pydantic ValidationError, no extra-forbidden fields, no money
    field coerced from a float (Decimal-only per CLAUDE.md).
    """
    for expected in _SECTION_8_2_CONTRACT:
        name = str(expected["name"])
        spec = _spec_by_name(name)
        funder = FunderRow.model_validate(spec)
        assert funder.name == name
        assert funder.active is True


def test_section_8_2_criteria_match_brief() -> None:
    """Field-by-field criteria check against the section 8.2 brief.

    Bound this tight so any future edit to a brief-listed funder's
    floor parameters trips this test and forces a deliberate review
    (matches CLAUDE.md "decision-boundary changes — deliberate"
    discipline).
    """
    for expected in _SECTION_8_2_CONTRACT:
        name = str(expected["name"])
        spec = _spec_by_name(name)

        assert spec["min_monthly_revenue"] == expected["min_monthly_revenue"], (
            f"{name}: min_monthly_revenue {spec['min_monthly_revenue']!r} "
            f"differs from brief {expected['min_monthly_revenue']!r}"
        )
        assert spec["min_credit_score"] == expected["min_credit_score"], (
            f"{name}: min_credit_score {spec['min_credit_score']!r} "
            f"differs from brief {expected['min_credit_score']!r}"
        )
        assert spec["min_months_in_business"] == expected["min_months_in_business"], (
            f"{name}: min_months_in_business "
            f"{spec['min_months_in_business']!r} differs from brief "
            f"{expected['min_months_in_business']!r}"
        )
        assert spec["max_positions"] == expected["max_positions"], (
            f"{name}: max_positions {spec['max_positions']!r} differs "
            f"from brief {expected['max_positions']!r}"
        )
        assert spec["accepts_stacking"] == expected["accepts_stacking"], (
            f"{name}: accepts_stacking {spec['accepts_stacking']!r} "
            f"differs from brief {expected['accepts_stacking']!r}"
        )

        spec_deal_types = frozenset(
            t.lower() for t in cast(Iterable[str], spec["deal_types_accepted"])
        )
        expected_deal_types = expected["deal_types_accepted"]
        assert isinstance(expected_deal_types, frozenset)
        assert spec_deal_types == expected_deal_types, (
            f"{name}: deal_types_accepted {sorted(spec_deal_types)} differs "
            f"from brief {sorted(cast(frozenset[str], expected_deal_types))}"
        )


def test_money_fields_are_decimal_not_float() -> None:
    """Per CLAUDE.md: money is Decimal, never float."""
    for expected in _SECTION_8_2_CONTRACT:
        name = str(expected["name"])
        spec = _spec_by_name(name)
        rev = spec["min_monthly_revenue"]
        assert isinstance(rev, Decimal), (
            f"{name}: min_monthly_revenue {rev!r} must be Decimal (got {type(rev).__name__})"
        )


def test_seed_remains_idempotent_by_name() -> None:
    """The six section 8.2 names must be unique within the seed list.

    The script's idempotency contract (skip-if-name-exists) requires
    distinct canonical names; a duplicate would defeat the skip path.
    """
    counts: dict[str, int] = {}
    for spec in _MISSING_FUNDERS:
        canonical = str(spec["name"]).strip().lower()
        counts[canonical] = counts.get(canonical, 0) + 1
    dupes = {name: n for name, n in counts.items() if n > 1}
    assert not dupes, (
        f"seed_missing_funders.py has duplicate canonical names: "
        f"{dupes}. Idempotency relies on unique names."
    )
