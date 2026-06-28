"""Tests for ``aegis.business_intel.license_checker`` — trade-licensing gate.

The gate replaces the dossier "Submit to funders" button when:
  * the merchant's NAICS is in ``LICENSED_INDUSTRIES_BY_NAICS``, AND
  * the (state, industry_key) pair has a verified portal URL, AND
  * no prior ``merchant.license_verified_manually`` audit row exists.

When any precondition fails the gate is skipped (``required=False``)
and the original Submit button renders unchanged. The skip-on-unknown
posture is deliberate per AEGIS operating-principle 4 — block with a
verified link or don't block at all.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from aegis.audit import InMemoryAuditLog
from aegis.business_intel.license_checker import (
    LICENSE_INDUSTRY_LABELS,
    LICENSE_PORTALS,
    LICENSED_INDUSTRIES_BY_NAICS,
    STATE_NAMES,
    LicenseGateContext,
    evaluate_license_gate,
    record_license_verification,
)


def _evaluate(
    *,
    state: str | None = "FL",
    industry_naics: str | None = "238220",
    audit: InMemoryAuditLog | None = None,
    merchant_id: UUID | None = None,
) -> LicenseGateContext:
    return evaluate_license_gate(
        merchant_id=merchant_id or uuid4(),
        state=state,
        industry_naics=industry_naics,
        audit=audit or InMemoryAuditLog(),
    )


# ---------------------------------------------------------------------------
# Coverage assertions on the mapping dicts
# ---------------------------------------------------------------------------


def test_industry_labels_cover_every_key_in_naics_map() -> None:
    """Every industry_key emitted by the NAICS map must have a display
    label — missing labels would render as None in the dossier banner."""
    industry_keys = set(LICENSED_INDUSTRIES_BY_NAICS.values())
    for key in industry_keys:
        assert key in LICENSE_INDUSTRY_LABELS, f"missing label for industry_key={key!r}"


def test_top_five_states_cover_top_five_trades() -> None:
    """FL, TX, CA, NY, GA crossed with {GC, HVAC/plumbing, electrical,
    roofing, cosmetology} must have at least four portal URLs each --
    these are Commera's highest-volume state+trade combos. NY trades
    are jurisdiction-specific (no statewide portal), so NY has fewer
    entries by design."""
    must_cover_per_state = {
        "FL": 4,
        "TX": 2,
        "CA": 4,
        "GA": 3,
        "NY": 1,  # statewide trades are city/county; only pro-license entries
    }
    for state, min_count in must_cover_per_state.items():
        actual = sum(1 for (s, _) in LICENSE_PORTALS if s == state)
        assert actual >= min_count, (
            f"{state} has only {actual} portal entries — expected >= {min_count}"
        )


# ---------------------------------------------------------------------------
# Gate evaluation
# ---------------------------------------------------------------------------


def test_fl_hvac_merchant_triggers_gate_with_portal_url() -> None:
    ctx = _evaluate(state="FL", industry_naics="238220")
    assert ctx.required is True
    assert ctx.industry_key == "hvac_plumbing_contractor"
    assert ctx.industry_label == "HVAC / Plumbing Contractor"
    assert ctx.portal_url is not None
    assert "myfloridalicense.com" in ctx.portal_url
    assert ctx.state_name == "Florida"
    assert ctx.already_verified is False


def test_unknown_naics_skips_gate() -> None:
    """Most industries don't require licenses; the gate must skip silently."""
    ctx = _evaluate(state="FL", industry_naics="999999")
    assert ctx.required is False
    assert ctx.industry_key is None
    assert ctx.portal_url is None


def test_missing_naics_skips_gate() -> None:
    ctx = _evaluate(state="FL", industry_naics=None)
    assert ctx.required is False


def test_tx_general_contractor_has_no_portal_so_skips() -> None:
    """TX has no state-level residential GC portal (it's municipal).
    The gate must NOT block — better to NOT-block than to block with
    a wrong URL (operating-principle 4)."""
    ctx = _evaluate(state="TX", industry_naics="236118")  # residential remodeler -> GC
    # Industry is recognized but no portal entry exists for TX GC.
    assert ctx.industry_key == "general_contractor"
    assert ctx.portal_url is None
    assert ctx.required is False


def test_ca_general_contractor_uses_cslb_portal() -> None:
    ctx = _evaluate(state="CA", industry_naics="236220")
    assert ctx.required is True
    assert ctx.portal_url is not None
    assert "cslb.ca.gov" in ctx.portal_url
    assert ctx.state_name == "California"


def test_state_without_portal_for_industry_skips_gate() -> None:
    """A state we don't have a portal for falls through cleanly — same
    posture as the TX GC case: known industry, unknown verification path,
    skip rather than block."""
    ctx = _evaluate(state="VT", industry_naics="238220")  # VT not in LICENSE_PORTALS
    assert ctx.required is False
    assert ctx.industry_key == "hvac_plumbing_contractor"  # still surfaced for diagnostics
    assert ctx.portal_url is None


def test_missing_state_skips_gate() -> None:
    ctx = _evaluate(state=None, industry_naics="238220")
    assert ctx.required is False


def test_already_verified_merchant_bypasses_gate() -> None:
    """After ``record_license_verification`` writes the audit row, the
    next evaluate call must see ``already_verified=True`` and skip
    the gate (the Submit button renders unchanged)."""
    audit = InMemoryAuditLog()
    merchant_id = uuid4()
    # First render: gate should fire
    ctx_before = _evaluate(
        state="FL", industry_naics="238220", audit=audit, merchant_id=merchant_id
    )
    assert ctx_before.required is True
    # Operator clicks "Mark license verified"
    record_license_verification(
        merchant_id=merchant_id,
        state="FL",
        industry_naics="238220",
        actor="operator:alice@aegis.example",
        actor_email="alice@aegis.example",
        audit=audit,
    )
    # Second render: gate stands down
    ctx_after = _evaluate(state="FL", industry_naics="238220", audit=audit, merchant_id=merchant_id)
    assert ctx_after.required is False
    assert ctx_after.already_verified is True


def test_verification_writes_audit_row_with_pii_safe_details() -> None:
    """The verification audit row carries NAICS + state + industry_key
    in details — operator-facing public fields, no merchant PII."""
    audit = InMemoryAuditLog()
    merchant_id = uuid4()
    record_license_verification(
        merchant_id=merchant_id,
        state="ca",  # lowercased input — verify normalization
        industry_naics="238210",
        actor="operator:bob@aegis.example",
        actor_email="bob@aegis.example",
        audit=audit,
    )
    rows = audit.list_for_subject(subject_type="merchant", subject_id=merchant_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == "merchant.license_verified_manually"
    assert row["actor"] == "operator:bob@aegis.example"
    assert row["actor_email"] == "bob@aegis.example"
    details = row["details"]
    assert details["industry_naics"] == "238210"
    assert details["industry_key"] == "electrician"
    assert details["industry_label"] == "Electrician"
    assert details["state"] == "CA"  # uppercased on write


def test_verification_audit_row_filtered_by_action_on_lookup() -> None:
    """Other merchant audit rows in the same subject scope must not be
    mistaken for license verification. The lookup filters by action."""
    audit = InMemoryAuditLog()
    merchant_id = uuid4()
    # Pollute the subject with unrelated audit rows
    audit.record(
        actor="system",
        action="merchant.created",
        subject_type="merchant",
        subject_id=merchant_id,
    )
    audit.record(
        actor="system",
        action="merchant.assigned",
        subject_type="merchant",
        subject_id=merchant_id,
    )
    ctx = _evaluate(state="FL", industry_naics="238220", audit=audit, merchant_id=merchant_id)
    assert ctx.required is True
    assert ctx.already_verified is False


def test_state_name_falls_back_to_state_code_when_unknown() -> None:
    """States not in STATE_NAMES (e.g. a Caribbean territory the gate
    doesn't have a portal for) still render — the banner copy uses
    the raw state code rather than crashing."""
    # AS = American Samoa, intentionally unmapped
    audit = InMemoryAuditLog()
    merchant_id = uuid4()
    ctx = evaluate_license_gate(
        merchant_id=merchant_id, state="AS", industry_naics="238220", audit=audit
    )
    # Industry recognized, but no portal entry → gate skipped
    assert ctx.required is False
    assert ctx.industry_key == "hvac_plumbing_contractor"
    # state_name None when state not in STATE_NAMES
    assert "AS" not in STATE_NAMES
