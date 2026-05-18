"""Tests for the state compliance matrix loader (mp Phase 1).

Three concerns:

1. Coverage: states.yaml has all 51 USPS codes (50 states + DC), in the
   correct tier per master plan §8.1/§8.3/§8.4.
2. Schema: Tier 1 entries carry the full CFDL surface (statute, effective,
   overlays, penalties). Tier 2 carries the watch-list shape. Tier 3 carries
   the defensive-disclosure default.
3. Boot fail-closed: a mutated states.yaml (typo / missing field / invalid
   enum) raises StateMatrixError, never silently loads.

The 450-case exhaustive router coverage lives in ``test_router.py`` to
keep this file focused on the matrix loader itself.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from aegis.compliance.state_matrix import (
    StateMatrix,
    StateMatrixError,
    Tier1Regulation,
    Tier2Regulation,
    Tier3Regulation,
    load_matrix,
)

# Expected tier per master plan §8.1/§8.3/§8.4. Frozen here as the
# regression contract — if these change, states.yaml has drifted from
# the master plan and the change needs a compliance-review annotation.
TIER1_STATES: frozenset[str] = frozenset(
    {"CA", "NY", "UT", "VA", "FL", "GA", "CT", "KS", "MO", "LA", "TX"}
)
TIER2_STATES: frozenset[str] = frozenset(
    {"NJ", "MD", "IL", "MS", "NC", "PA", "HI", "NH", "OH"}
)
ALL_USPS_CODES: frozenset[str] = frozenset(
    {
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
        "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
        "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
        "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
        "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
        "DC",
    }
)
TIER3_STATES: frozenset[str] = ALL_USPS_CODES - TIER1_STATES - TIER2_STATES


@pytest.fixture(scope="module")
def matrix() -> StateMatrix:
    """The production-loaded matrix. Reused across tests for speed."""
    return load_matrix()


# ---------------------------------------------------------------------------
# Coverage: every state present, no extras, correct tier
# ---------------------------------------------------------------------------


def test_loads_51_states(matrix: StateMatrix) -> None:
    assert len(matrix.states) == 51


def test_all_usps_codes_present(matrix: StateMatrix) -> None:
    assert set(matrix.states.keys()) == ALL_USPS_CODES


def test_tier_partition_counts(matrix: StateMatrix) -> None:
    tiers = {1: 0, 2: 0, 3: 0}
    for reg in matrix.states.values():
        tiers[reg.tier] += 1
    assert tiers == {1: 11, 2: 9, 3: 31}


@pytest.mark.parametrize("code", sorted(ALL_USPS_CODES))
def test_state_tier_matches_master_plan(matrix: StateMatrix, code: str) -> None:
    """One assertion per state (51 total). Bound to master plan §8."""
    reg = matrix.states[code]
    if code in TIER1_STATES:
        assert isinstance(reg, Tier1Regulation), f"{code} should be Tier 1"
        assert reg.tier == 1
    elif code in TIER2_STATES:
        assert isinstance(reg, Tier2Regulation), f"{code} should be Tier 2"
        assert reg.tier == 2
    else:
        assert isinstance(reg, Tier3Regulation), f"{code} should be Tier 3"
        assert reg.tier == 3


# ---------------------------------------------------------------------------
# Schema: Tier 1 entries carry every required field
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(TIER1_STATES))
def test_tier1_carries_full_cfdl(matrix: StateMatrix, code: str) -> None:
    """Every Tier 1 state must have statute, effective date, product_scope,
    apr_required, retention_years, overlays, penalties, last_reviewed.
    """
    reg = matrix.states[code]
    assert isinstance(reg, Tier1Regulation)
    assert reg.name
    assert reg.cfdl.statute
    assert isinstance(reg.cfdl.effective, date)
    assert reg.cfdl.product_scope
    assert isinstance(reg.cfdl.apr_required, bool)
    assert reg.cfdl.retention_years >= 0
    assert reg.overlays.coj
    assert reg.overlays.autodebit
    assert reg.penalties.enforcement_authority
    assert isinstance(reg.last_reviewed, date)


def test_tx_hard_decline_rule_present(matrix: StateMatrix) -> None:
    """TX HB 700 must carry tx_autodebit_without_first_priority_lien."""
    reg = matrix.states["TX"]
    assert isinstance(reg, Tier1Regulation)
    codes = [r.code for r in reg.hard_decline_rules]
    assert "tx_autodebit_without_first_priority_lien" in codes


def test_la_no_threshold_true(matrix: StateMatrix) -> None:
    """LA HB 470 applies regardless of deal size."""
    reg = matrix.states["LA"]
    assert isinstance(reg, Tier1Regulation)
    assert reg.cfdl.no_threshold is True
    assert reg.cfdl.threshold_usd is None


def test_tx_autodebit_overlay_prohibited(matrix: StateMatrix) -> None:
    reg = matrix.states["TX"]
    assert isinstance(reg, Tier1Regulation)
    assert reg.overlays.autodebit == "prohibited_without_first_priority_lien"


def test_va_forum_restriction_mandatory(matrix: StateMatrix) -> None:
    reg = matrix.states["VA"]
    assert isinstance(reg, Tier1Regulation)
    assert reg.overlays.forum_restriction == "mandatory_in_state"


def test_fl_ga_broker_advance_fee_prohibited(matrix: StateMatrix) -> None:
    for code in ("FL", "GA"):
        reg = matrix.states[code]
        assert isinstance(reg, Tier1Regulation)
        assert reg.overlays.broker_advance_fee == "prohibited", code


def test_money_fields_are_decimal(matrix: StateMatrix) -> None:
    """threshold_usd and max_per_violation_usd must be Decimal instances
    after loader runs — never int/float/str (the Money coercer enforces
    this).
    """
    ca = matrix.states["CA"]
    assert isinstance(ca, Tier1Regulation)
    assert isinstance(ca.cfdl.threshold_usd, Decimal)
    ny = matrix.states["NY"]
    assert isinstance(ny, Tier1Regulation)
    assert isinstance(ny.penalties.max_per_violation_usd, Decimal)


# ---------------------------------------------------------------------------
# Boot fail-closed: invalid states.yaml raises
# ---------------------------------------------------------------------------


def test_missing_file_raises(tmp_path: Path) -> None:
    bogus = tmp_path / "absent.yaml"
    with pytest.raises(StateMatrixError, match="not found"):
        load_matrix(bogus)


def test_invalid_yaml_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text(": bad: yaml :", encoding="utf-8")
    with pytest.raises(StateMatrixError):
        load_matrix(p)


def test_non_mapping_root_raises(tmp_path: Path) -> None:
    p = tmp_path / "list.yaml"
    p.write_text("- not a mapping\n", encoding="utf-8")
    with pytest.raises(StateMatrixError, match="root must be a mapping"):
        load_matrix(p)


def test_extra_field_rejected(tmp_path: Path) -> None:
    """Strict mode forbids extra fields anywhere in the matrix."""
    p = tmp_path / "extra.yaml"
    p.write_text(
        "version: '2026.05.18'\n"
        "tier_3_default:\n"
        "  generate_defensive_disclosure: true\n"
        "  persist_decision_snapshot: true\n"
        "  flag_ag_enforcement_states: []\n"
        "  unknown_field: oops\n"  # extra → reject
        "states:\n",
        encoding="utf-8",
    )
    with pytest.raises(StateMatrixError):
        load_matrix(p)


def test_missing_required_state_raises(tmp_path: Path) -> None:
    """If states.yaml is missing any USPS code, coverage check raises."""
    p = tmp_path / "incomplete.yaml"
    # Minimal valid-looking matrix with only one state.
    p.write_text(
        "version: '2026.05.18'\n"
        "tier_3_default:\n"
        "  generate_defensive_disclosure: true\n"
        "  persist_decision_snapshot: true\n"
        "  flag_ag_enforcement_states: []\n"
        "states:\n"
        "  CA: {tier: 3, name: California, posture: defensive_disclosure_only,\n"
        "       ag_enforcement_risk: low, last_reviewed: 2026-05-17}\n",
        encoding="utf-8",
    )
    with pytest.raises(StateMatrixError, match="missing state"):
        load_matrix(p)


def test_money_field_rejects_float(tmp_path: Path) -> None:
    """Float for money is banned per CLAUDE.md / master plan §2."""
    # Build a minimal matrix where one Tier 1 has threshold_usd = 500000.50
    # parsed as a YAML float. The Money BeforeValidator must reject.
    # We test the coercer directly to avoid the whole-matrix overhead.
    from aegis.compliance.state_matrix import _coerce_to_decimal

    with pytest.raises(TypeError):
        _coerce_to_decimal(500000.5)
    with pytest.raises(TypeError):
        _coerce_to_decimal(True)  # bool subclass guard


def test_money_field_accepts_int_str_decimal() -> None:
    from aegis.compliance.state_matrix import _coerce_to_decimal

    assert _coerce_to_decimal(500000) == Decimal("500000")
    assert _coerce_to_decimal("500000.00") == Decimal("500000.00")
    assert _coerce_to_decimal(Decimal("500000.00")) == Decimal("500000.00")
