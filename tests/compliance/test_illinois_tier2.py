"""Illinois Tier 2 promotion tests.

Asserts the dossier-driven facts from
``docs/compliance/05_illinois.md`` are reflected in the STATES table
and the Tier 2 generic-acknowledgment renders correctly. IL is the
first Tier 2 entry that exercises the full Tier 2 metadata schema
(CoJ posture, loan-broker registration, pending legislation,
quarterly review) — this file also locks the schema additions.

Critical: Illinois has NOT enacted an MCA-specific disclosure law.
HB 3477 (2025-2026) is pending; SB 2234 (103rd GA) died sine die
2025-01-07. AEGIS treats IL as Tier 2 until enactment.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from aegis.compliance.disclosure import render_disclosure
from aegis.compliance.states import (
    STATES,
    PendingLegislation,
    Tier2Regulation,
)
from aegis.scoring.models import ScoreInput, ScoreResult

# --- States table -----------------------------------------------------------


def test_illinois_is_tier2() -> None:
    il = STATES["IL"]
    assert isinstance(il, Tier2Regulation)
    assert il.tier == 2


def test_illinois_disclosure_not_required() -> None:
    """Tier 2 invariant: no MCA-specific disclosure obligation."""
    il = STATES["IL"]
    assert isinstance(il, Tier2Regulation)
    assert il.disclosure_required is False


def test_illinois_general_law_citation_includes_three_statutes() -> None:
    """Per dossier: Consumer Fraud Act, Loan Brokers Act, CoJ statute."""
    il = STATES["IL"]
    assert isinstance(il, Tier2Regulation)
    cites = il.general_law_citation
    assert "815 ILCS 505" in cites           # Consumer Fraud Act
    assert "815 ILCS 175" in cites           # Loan Brokers Act
    assert "735 ILCS 5/2-1301" in cites      # CoJ statute


def test_illinois_coj_allowed_for_commercial_with_consumer_ban() -> None:
    """735 ILCS 5/2-1301(c) permits commercial CoJ; bans consumer CoJ since 1979."""
    il = STATES["IL"]
    assert isinstance(il, Tier2Regulation)
    assert il.coj_allowed == "allowed"
    assert il.coj_citation == "735 ILCS 5/2-1301(c)"
    assert il.coj_consumer_ban is True


def test_illinois_loan_broker_registration_metadata() -> None:
    """Loan Brokers Act of 1995 may apply — operator must verify before first deal."""
    il = STATES["IL"]
    assert isinstance(il, Tier2Regulation)
    assert il.loan_broker_registration_authority == "Illinois Secretary of State"
    assert il.loan_broker_registration_required == "verify_before_first_deal"
    assert il.loan_broker_bond_required_usd == Decimal("25000")


def test_illinois_pending_hb3477_tracked() -> None:
    """HB 3477 (2025-2026) would create IL's first MCA disclosure law."""
    il = STATES["IL"]
    assert isinstance(il, Tier2Regulation)
    bills = {pl.bill_number: pl for pl in il.pending_legislation}
    assert "HB 3477" in bills
    hb3477 = bills["HB 3477"]
    assert isinstance(hb3477, PendingLegislation)
    assert hb3477.year == 2025
    assert hb3477.session == "2025-2026"
    assert hb3477.status == "introduced"
    assert hb3477.would_promote_to_tier == 1
    assert hb3477.common_name == "Small Business Financing Transparency Act"


def test_illinois_dead_predecessor_sb2234_recorded() -> None:
    """SB 2234 died sine die at end of 103rd GA — kept for institutional memory."""
    il = STATES["IL"]
    assert isinstance(il, Tier2Regulation)
    bills = {pl.bill_number: pl for pl in il.pending_legislation}
    assert "SB 2234" in bills
    sb2234 = bills["SB 2234"]
    assert sb2234.status == "died"
    assert sb2234.session == "103rd GA"


def test_illinois_quarterly_review_required() -> None:
    """Active pending legislation → operator must re-check status quarterly."""
    il = STATES["IL"]
    assert isinstance(il, Tier2Regulation)
    assert il.quarterly_review_required is True


def test_illinois_notes_flag_no_enacted_disclosure_law() -> None:
    il = STATES["IL"]
    assert isinstance(il, Tier2Regulation)
    assert "NOT enacted" in il.notes
    assert "HB 3477" in il.notes
    assert "SB 2234" in il.notes


# --- Tier 2 disclosure rendering -------------------------------------------


def _il_score_input() -> ScoreInput:
    return ScoreInput(
        merchant_id=uuid4(),
        business_name="Acme IL",
        owner_name="Jane Doe",
        state="IL",
        avg_daily_balance=Decimal("12500.00"),
        true_revenue=Decimal("100000.00"),
        monthly_revenue=Decimal("100000.00"),
        lowest_balance=Decimal("3000.00"),
        num_nsf=0,
        days_negative=0,
        mca_positions=0,
        mca_daily_total=Decimal("0.00"),
        debt_to_revenue=Decimal("0.00"),
        fraud_score=10,
        statement_period_start=date(2026, 4, 1),
        statement_period_end=date(2026, 4, 30),
        statement_days=30,
        requested_amount=Decimal("50000.00"),
        requested_factor=Decimal("1.30"),
        requested_term_days=120,
    )


def _baseline_score_result() -> ScoreResult:
    return ScoreResult(
        score=72,
        tier="B",
        recommendation="approve",
        suggested_max_advance=Decimal("100000.00"),
        recommended_factor_rate=Decimal("1.29"),
        recommended_holdback_pct=Decimal("0.12"),
        estimated_payback_days=180,
    )


def test_il_renders_via_generic_tier2_acknowledgment() -> None:
    """Disclosure router routes IL through the Tier 2 generic template."""
    rendered = render_disclosure("IL", _il_score_input(), _baseline_score_result())
    assert rendered.tier == 2
    assert rendered.state == "IL"
    # Citation comes from general_law_citation, not a bill_number.
    assert "815 ILCS 505" in rendered.citation
    # Generic acknowledgment always names the state.
    assert "Illinois" in rendered.html
    # And cites the general law verbatim from the table.
    assert "735 ILCS 5/2-1301" in rendered.html


def test_il_disclosure_does_not_pretend_to_be_state_prescribed() -> None:
    """Generic template explicitly disclaims being a prescribed form."""
    rendered = render_disclosure("IL", _il_score_input(), _baseline_score_result())
    assert "not a state-prescribed" in rendered.html


# --- PendingLegislation model -----------------------------------------------


def test_pending_legislation_rejects_unknown_status() -> None:
    """Status is a Literal — typos must fail loudly."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PendingLegislation(
            bill_number="X 1",
            year=2025,
            status="enacted_but_not_signed",
            citation_url="https://example.invalid/x1",
        )


def test_pending_legislation_status_died_is_valid() -> None:
    """SB 2234 needs `died` to be a valid status — institutional memory."""
    pl = PendingLegislation(
        bill_number="SB 2234",
        year=2023,
        session="103rd GA",
        status="died",
        citation_url="https://example.invalid/sb2234",
    )
    assert pl.status == "died"


def test_pending_legislation_optional_fields_default_none() -> None:
    pl = PendingLegislation(
        bill_number="HB 1",
        year=2026,
        citation_url="https://example.invalid/hb1",
    )
    assert pl.session is None
    assert pl.common_name is None
    assert pl.would_promote_to_tier is None
    assert pl.status == "introduced"  # default
