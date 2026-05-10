"""States table — inventory and boot validator tests.

Six unit tests required by the Phase 4 spec live here and in
`test_disclosure.py`. This file owns:
  (1) all 45 states present
  (2) Tier 1 missing-field rejected (constructor-time check)
  (3) Tier 1 missing-template-file rejected by validator
  (5) non-served state rejected with state_not_served
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from aegis.compliance import states as states_module
from aegis.compliance.states import (
    SKELETON_VERIFIED_DATE,
    STATES,
    CompliancePolicyError,
    StateNotServed,
    Tier1Regulation,
    Tier3Regulation,
    validate_state_served,
    validate_states_table,
)

# (1) ------------------------------------------------------------------------


def test_all_45_states_present() -> None:
    expected = {
        "AL", "AK", "AZ", "AR", "CA", "CO", "DE", "FL", "GA", "HI", "ID", "IL",
        "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MT",
        "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
        "RI", "SC", "SD", "TN", "VT", "WA", "WV", "WI", "WY",
    }
    assert set(STATES.keys()) == expected
    assert len(STATES) == 45


def test_skeleton_states_are_all_tier_3_except_promoted() -> None:
    """Skeleton: every state starts Tier 3; promoted states moved up.

    CA promoted Tier 1 per docs/compliance/01_california.md.
    NY promoted Tier 1 per docs/compliance/02_new_york.md.
    FL promoted Tier 1 per docs/compliance/03_florida.md.
    GA promoted Tier 1 per docs/compliance/04_georgia.md.
    IL promoted Tier 2 per docs/compliance/05_illinois.md.
    """
    promoted_to_tier_1 = {"CA", "NY", "FL", "GA"}
    promoted_to_tier_2 = {"IL"}
    for abbr, reg in STATES.items():
        if abbr in promoted_to_tier_1:
            assert reg.tier == 1, f"{abbr} should be promoted to Tier 1"
        elif abbr in promoted_to_tier_2:
            assert reg.tier == 2, f"{abbr} should be promoted to Tier 2"
        else:
            assert reg.tier == 3, f"{abbr} should default to Tier 3"
        assert reg.verified_date == SKELETON_VERIFIED_DATE


def test_skeleton_table_validates() -> None:
    validate_states_table()  # should not raise


# (5) ------------------------------------------------------------------------


@pytest.mark.parametrize("non_served", ["TX", "VA", "CT", "UT", "MO", "DC", "PR"])
def test_non_served_state_rejected(non_served: str) -> None:
    with pytest.raises(StateNotServed, match=r"state_not_served"):
        validate_state_served(non_served)


def test_served_state_does_not_raise() -> None:
    validate_state_served("CA")
    validate_state_served("ny")  # case-insensitive


# (2) ------------------------------------------------------------------------


def test_tier1_construction_rejects_missing_field() -> None:
    """Pydantic enforces required Tier 1 fields at construction time.

    Build a Tier 1 entry that omits `template_path` (the AEGIS-internal
    routing field the boot validator needs) — Pydantic must reject.
    """
    with pytest.raises(ValidationError):
        Tier1Regulation(  # type: ignore[call-arg]
            state="ZZ",
            state_name="Test State",
            verified_date=date(2026, 5, 7),
            tier=1,
            bill_number="Test SB 1",
            bill_year=2024,
            chapter="Chapter 1",
            sponsor="Tester",
            effective_date_statute=date(2024, 1, 1),
            effective_date_regulations=date(2024, 6, 1),
            statute_citation="Test § 1",
            regulation_citation="Test Reg § 1",
            citation_url_statute="https://example.invalid/statute",
            citation_url_regulation="https://example.invalid/regs",
            prescribed_form_section="Test § 100",
            apr_calculation_method="actuarial_reg_z",
            threshold_amount_usd=Decimal("250000"),
            threshold_test_summary="When offer <= $250K and merchant in ZZ.",
            coj_allowed="allowed",
            coj_citation="—",
            coj_citation_url="https://example.invalid/coj",
            coj_amendment_bill="Test SB 0 (2023)",
            coj_effective_date=date(2024, 1, 1),
            requires_unaltered_disclosure_transmission=False,
            transmission_record_retention_years=3,
            broker_compensation_disclosure_required=False,
            notes="Test entry.",
            # template_path missing on purpose
        )


# (3) ------------------------------------------------------------------------


def test_validator_rejects_tier1_with_missing_template_file() -> None:
    """Boot validator must reject any Tier 1 entry whose template file is absent.

    We construct a Tier 1 entry pointing at a path we know does not exist
    on disk and assign it to a test state slot. The CA promotion is
    untouched — its real template (ca_sb1235.html.j2) does exist.
    """
    fake = Tier1Regulation(
        state="CA",
        state_name="California",
        verified_date=date(2026, 5, 7),
        tier=1,
        bill_number="SB 1235",
        bill_year=2018,
        chapter="Chapter 1011, Statutes of 2018",
        sponsor="Glazer",
        effective_date_statute=date(2018, 9, 30),
        effective_date_regulations=date(2022, 12, 9),
        statute_citation="Cal. Fin. Code § 22800-22805",
        regulation_citation="10 CCR § 900-956",
        citation_url_statute="https://leginfo.legislature.ca.gov/example",
        citation_url_regulation="https://example.invalid/reg",
        prescribed_form_section="10 CCR § 914",
        apr_calculation_method="actuarial_reg_z",
        threshold_amount_usd=Decimal("500000"),
        threshold_test_summary="<= $500K + CA-managed merchant.",
        coj_allowed="banned",
        coj_citation="Cal. Code Civ. Proc. § 1132",
        coj_citation_url="https://example.invalid/coj",
        coj_amendment_bill="SB 688 (2022)",
        coj_effective_date=date(2023, 1, 1),
        requires_unaltered_disclosure_transmission=True,
        transmission_record_retention_years=4,
        broker_compensation_disclosure_required=False,
        notes="Test fixture pointing at a nonexistent template path.",
        template_path="does_not_exist_on_disk.html.j2",
    )
    states_module.STATES["CA"] = fake

    with pytest.raises(CompliancePolicyError, match=r"template file missing"):
        validate_states_table()


def test_validator_detects_drift_extra_state() -> None:
    """If STATES is mutated to include a non-served state, validator complains."""
    states_module.STATES["TX"] = Tier3Regulation(
        state="TX", state_name="Texas", verified_date=date(2026, 5, 7), tier=3
    )
    with pytest.raises(CompliancePolicyError, match=r"not in served-state inventory"):
        validate_states_table()


def test_validator_detects_drift_missing_state() -> None:
    """If a served state is removed from STATES, validator complains."""
    del states_module.STATES["CA"]
    with pytest.raises(CompliancePolicyError, match=r"missing from STATES"):
        validate_states_table()
