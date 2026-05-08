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


def test_skeleton_states_are_all_tier_3() -> None:
    """Initial skeleton: every state starts in Tier 3 with the canonical date."""
    for abbr, reg in STATES.items():
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
    """Pydantic enforces required Tier 1 fields at construction time."""
    with pytest.raises(ValidationError):
        Tier1Regulation(  # type: ignore[call-arg]
            state="CA",
            state_name="California",
            verified_date=date(2026, 5, 7),
            tier=1,
            bill_number="California SB 1235",
            effective_date=date(2018, 9, 30),
            citation_url="https://example.com",
            citation_excerpt="Example excerpt",
            apr_calculation_method="actuarial_reg_z",
            coj_allowed=True,
            coj_citation="—",
            # template_path missing on purpose
        )


# (3) ------------------------------------------------------------------------


def test_validator_rejects_tier1_with_missing_template_file() -> None:
    """Promote CA to Tier 1 referencing a template that doesn't exist on disk."""
    fake = Tier1Regulation(
        state="CA",
        state_name="California",
        verified_date=date(2026, 5, 7),
        tier=1,
        bill_number="California SB 1235",
        effective_date=date(2018, 9, 30),
        citation_url="https://leginfo.legislature.ca.gov/example",
        citation_excerpt="...",
        apr_calculation_method="actuarial_reg_z",
        coj_allowed=False,
        coj_citation="CA DFPI Reg X §yyy",
        template_path="ca_sb1235.html.j2",  # this file does not exist
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
