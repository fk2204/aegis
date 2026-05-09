"""CoJ rule tests — per-state branching.

CA hard-decline path (``coj_allowed="banned"``)
  Per docs/compliance/01_california.md: Cal. Code Civ. Proc. § 1132.
  Reason ``coj_invalid_in_state``; warning log
  ``funder_requires_coj_blocked_by_state``.

NY soft-concern path (``coj_allowed="conditional"``)
  Per docs/compliance/02_new_york.md: CPLR § 3218 (amended chapter 311
  of 2019, effective 2019-08-30) — CoJs enforceable only against NY-
  resident merchants. Reason ``coj_ny_resident_only``; info log of the
  same name. Operator confirms residency before transmitting agreement.

Tier 3 / non-CoJ-rule states pass through with no CoJ-related entries.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from aegis.funders.models import FunderRow
from aegis.scoring.match_funders import match_funder
from aegis.scoring.models import ScoreInput, ScoreResult


def _ca_score_input() -> ScoreInput:
    return ScoreInput(
        merchant_id=uuid4(),
        business_name="Acme CA",
        owner_name="Jane Doe",
        state="CA",
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


def _ny_score_input() -> ScoreInput:
    return _ca_score_input().model_copy(update={"state": "NY"})


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


def test_ca_merchant_plus_coj_funder_hard_declines() -> None:
    funder = FunderRow(name="Coj Funder LLC", requires_coj=True, max_positions=1)
    match = match_funder(funder, _ca_score_input(), _baseline_score_result())
    assert match is not None
    assert match.match_score == 0
    # Reason carries the dossier's identifier + Cal. Code Civ. Proc. § 1132.
    joined = " | ".join(match.soft_concerns)
    assert "coj_invalid_in_state" in joined
    assert "Cal. Code Civ. Proc. § 1132" in joined


def test_ca_merchant_plus_non_coj_funder_passes_coj_check(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Funder doesn't require CoJ → no CoJ block, even for CA merchant."""
    funder = FunderRow(name="Friendly Funder", requires_coj=False, max_positions=1)
    with caplog.at_level(logging.WARNING, logger="aegis.scoring.match_funders"):
        match = match_funder(funder, _ca_score_input(), _baseline_score_result())
    assert match is not None
    joined = " | ".join(match.soft_concerns)
    assert "coj_invalid_in_state" not in joined
    # And no CoJ-block log line was emitted.
    assert not any("funder_requires_coj_blocked_by_state" in r.message for r in caplog.records)


def test_tier3_state_plus_coj_funder_does_not_block_on_coj() -> None:
    """Tier 3 / non-Tier-1 states pass the CoJ check entirely — no rule.

    Use WY (still Tier 3); FL was previously the example state here but
    promoted to Tier 1 with coj_allowed='banned' per
    docs/compliance/03_florida.md.
    """
    funder = FunderRow(name="Coj Funder LLC", requires_coj=True, max_positions=1)
    deal = _ca_score_input().model_copy(update={"state": "WY"})
    match = match_funder(funder, deal, _baseline_score_result())
    assert match is not None
    joined = " | ".join(match.soft_concerns)
    assert "coj_invalid_in_state" not in joined
    assert "coj_ny_resident_only" not in joined


def test_ny_merchant_plus_coj_funder_soft_warns_residency(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """NY merchant + CoJ-requiring funder → soft concern, NOT hard fail.

    Per docs/compliance/02_new_york.md: NY permits CoJ but only against
    NY-resident merchants since 2019-08-30 (CPLR § 3218). Match score is
    non-zero (qualifies, with residency caveat); soft_concerns includes
    ``coj_ny_resident_only`` and the citation; INFO-level log records
    ``coj_ny_resident_only`` so the dashboard can flag the deal.
    """
    funder = FunderRow(name="Coj Funder LLC", requires_coj=True, max_positions=1)
    with caplog.at_level(logging.INFO, logger="aegis.scoring.match_funders"):
        match = match_funder(funder, _ny_score_input(), _baseline_score_result())
    assert match is not None
    joined = " | ".join(match.soft_concerns)
    assert "coj_invalid_in_state" not in joined  # NOT a hard fail.
    assert "coj_ny_resident_only" in joined
    assert "CPLR § 3218" in joined
    # Match still qualifies (no hard fail); score may be reduced by the soft.
    assert match.match_score > 0
    assert any(
        "coj_ny_resident_only" in r.getMessage() for r in caplog.records
    )


def test_ny_merchant_plus_non_coj_funder_passes_clean() -> None:
    """NY soft warning fires only when funder requires CoJ."""
    funder = FunderRow(name="Friendly Funder", requires_coj=False, max_positions=1)
    match = match_funder(funder, _ny_score_input(), _baseline_score_result())
    assert match is not None
    joined = " | ".join(match.soft_concerns)
    assert "coj_invalid_in_state" not in joined
    assert "coj_ny_resident_only" not in joined


def test_match_log_records_funder_requires_coj_blocked_by_state(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The dossier's match-log identifier appears in WARNING-level logs."""
    funder = FunderRow(
        name="LoggedCoj Funder", requires_coj=True, max_positions=1
    )
    with caplog.at_level(logging.WARNING, logger="aegis.scoring.match_funders"):
        match_funder(funder, _ca_score_input(), _baseline_score_result())
    assert any(
        "funder_requires_coj_blocked_by_state" in r.getMessage()
        for r in caplog.records
    )
