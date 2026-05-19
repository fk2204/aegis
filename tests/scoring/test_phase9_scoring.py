"""Phase 9 scoring tests — paper grade, new hard-decline rules, new soft factors.

Covers the master plan §19 task list:

- Paper grade A/B/C/D computation per §5.8 thresholds.
- New hard-decline rules: ``acceleration_clause_triggered``,
  ``unauthorized_withdrawal_dispute_active``,
  ``bank_statement_tampering_confirmed``.
- New soft factors: counterparty concentration (statement-derived),
  payroll_present detector, ai_generated_statement composite.
"""

from __future__ import annotations

from decimal import Decimal

from aegis.scoring.models import ScoreInput
from aegis.scoring.score import compute_paper_grade, score_deal


# -- paper grade -------------------------------------------------------------


def test_paper_grade_a_clean_prime(clean_deal: ScoreInput) -> None:
    # clean_deal: tib=48, rev=$110k, adb=$12.5k (>15% of $110k? No - 11.4%).
    # Bump adb to qualify for A.
    deal = clean_deal.model_copy(
        update={
            "avg_daily_balance": Decimal("20000.00"),  # 18% of 110k
            "credit_score": 720,
        }
    )
    grade, reasons = compute_paper_grade(deal)
    assert grade == "A"
    assert "tib_24mo+" in reasons
    assert "revenue_25k+" in reasons
    assert "clean_position" in reasons


def test_paper_grade_b_when_one_a_criterion_fails(clean_deal: ScoreInput) -> None:
    # 4 NSF — passes B (≤5), fails A (≤2).
    deal = clean_deal.model_copy(
        update={
            "avg_daily_balance": Decimal("12500.00"),  # 11.4% — fails A, passes B
            "num_nsf": 4,
        }
    )
    grade, _ = compute_paper_grade(deal)
    assert grade == "B"


def test_paper_grade_c_when_b_fails(clean_deal: ScoreInput) -> None:
    deal = clean_deal.model_copy(
        update={
            "avg_daily_balance": Decimal("3000.00"),  # < 8% → fails B
            "num_nsf": 8,  # fails B (≤5) but passes C (≤10)
            "mca_positions": 2,  # passes C (≤2)
            "time_in_business_months": 7,
        }
    )
    grade, _ = compute_paper_grade(deal)
    assert grade == "C"


def test_paper_grade_d_when_below_c_minimums(clean_deal: ScoreInput) -> None:
    deal = clean_deal.model_copy(
        update={
            "monthly_revenue": Decimal("11000.00"),
            "true_revenue": Decimal("11000.00"),
            "num_nsf": 15,  # fails C (≤10)
            "mca_positions": 0,
        }
    )
    grade, _ = compute_paper_grade(deal)
    assert grade == "D"


def test_paper_grade_reasons_explain_downgrades(clean_deal: ScoreInput) -> None:
    deal = clean_deal.model_copy(
        update={
            "num_nsf": 4,  # fails A's ≤2 criterion
        }
    )
    _, reasons = compute_paper_grade(deal)
    # Should explain B-grade path. The codes used at the failing point
    # should describe what was checked.
    assert "nsf_0_5" in reasons


def test_paper_grade_present_on_score_result(clean_deal: ScoreInput) -> None:
    result = score_deal(
        clean_deal.model_copy(
            update={"avg_daily_balance": Decimal("20000.00"), "credit_score": 720}
        )
    )
    assert result.paper_grade in {"A", "B", "C", "D"}
    assert isinstance(result.paper_grade_reasons, list)
    assert len(result.paper_grade_reasons) > 0


# -- new hard-decline rules --------------------------------------------------


def test_acceleration_clause_triggered_is_hard_decline(clean_deal: ScoreInput) -> None:
    deal = clean_deal.model_copy(update={"acceleration_clause_triggered": True})
    result = score_deal(deal)
    assert "acceleration_clause_triggered" in result.hard_decline_reasons
    assert result.recommendation == "decline"


def test_unauthorized_withdrawal_dispute_is_hard_decline(clean_deal: ScoreInput) -> None:
    deal = clean_deal.model_copy(update={"unauthorized_withdrawal_dispute": True})
    result = score_deal(deal)
    assert "unauthorized_withdrawal_dispute_active" in result.hard_decline_reasons


def test_tampering_confirmed_is_hard_decline(clean_deal: ScoreInput) -> None:
    deal = clean_deal.model_copy(update={"tampering_confirmed": True})
    result = score_deal(deal)
    assert "bank_statement_tampering_confirmed" in result.hard_decline_reasons


def test_clean_deal_no_phase9_hard_declines(clean_deal: ScoreInput) -> None:
    result = score_deal(clean_deal)
    new_codes = {
        "acceleration_clause_triggered",
        "unauthorized_withdrawal_dispute_active",
        "bank_statement_tampering_confirmed",
    }
    assert not new_codes & set(result.hard_decline_reasons)


# -- counterparty soft scoring -----------------------------------------------


def test_top_counterparty_concentration_60pct_penalty(clean_deal: ScoreInput) -> None:
    deal = clean_deal.model_copy(update={"top_counterparty_pct": 65})
    penalized = score_deal(deal)
    factors = {f["factor"] for f in penalized.breakdown}
    assert "top_counterparty_60pct+" in factors
    # The penalty delta should be -12 per the scorer's grid.
    delta = next(f["delta"] for f in penalized.breakdown if f["factor"] == "top_counterparty_60pct+")
    assert delta == -12


def test_top_counterparty_30_40_pct_mild_penalty(clean_deal: ScoreInput) -> None:
    deal = clean_deal.model_copy(update={"top_counterparty_pct": 35})
    result = score_deal(deal)
    factors = {f["factor"] for f in result.breakdown}
    assert "top_counterparty_30_40pct" in factors


def test_top_counterparty_below_30pct_no_penalty(clean_deal: ScoreInput) -> None:
    deal = clean_deal.model_copy(update={"top_counterparty_pct": 25})
    result = score_deal(deal)
    factors = {f["factor"] for f in result.breakdown}
    assert "top_counterparty_30_40pct" not in factors
    assert "top_counterparty_60pct+" not in factors


def test_top_5_revenue_share_above_80_penalty(clean_deal: ScoreInput) -> None:
    deal = clean_deal.model_copy(
        update={"top_counterparty_pct": 20, "top_5_revenue_share_pct": 85}
    )
    result = score_deal(deal)
    factors = {f["factor"] for f in result.breakdown}
    assert "top_5_revenue_share_80pct+" in factors


# -- payroll_present soft scoring --------------------------------------------


def test_payroll_present_credit_when_not_in_merchant_row(clean_deal: ScoreInput) -> None:
    # The merchant row has payroll_detected=True; flip it off and let
    # the detector add the credit.
    deal = clean_deal.model_copy(
        update={"payroll_detected": False, "payroll_present": True}
    )
    result = score_deal(deal)
    factors = {f["factor"] for f in result.breakdown}
    assert "payroll_present_detector" in factors


def test_payroll_absent_high_revenue_soft_concern(clean_deal: ScoreInput) -> None:
    deal = clean_deal.model_copy(
        update={
            "payroll_detected": False,
            "payroll_present": False,
            "monthly_revenue": Decimal("80000.00"),
            "true_revenue": Decimal("80000.00"),
        }
    )
    result = score_deal(deal)
    assert "payroll_absent_high_revenue" in result.soft_concerns


# -- ai_generated_statement composite ----------------------------------------


def test_ai_generated_strong_penalty(clean_deal: ScoreInput) -> None:
    deal = clean_deal.model_copy(update={"ai_generated_score": 90})
    result = score_deal(deal)
    factors = {f["factor"] for f in result.breakdown}
    assert "ai_generated_statement_strong" in factors


def test_ai_generated_does_not_auto_decline(clean_deal: ScoreInput) -> None:
    # Per §6.4 — composite scores, never auto-decline.
    deal = clean_deal.model_copy(update={"ai_generated_score": 100})
    result = score_deal(deal)
    assert result.recommendation != "decline"


def test_ai_generated_weak_modest_penalty(clean_deal: ScoreInput) -> None:
    deal = clean_deal.model_copy(update={"ai_generated_score": 60})
    result = score_deal(deal)
    factors = {f["factor"] for f in result.breakdown}
    assert "ai_generated_statement_weak" in factors


def test_ai_generated_below_55_no_penalty(clean_deal: ScoreInput) -> None:
    deal = clean_deal.model_copy(update={"ai_generated_score": 40})
    result = score_deal(deal)
    factors = {f["factor"] for f in result.breakdown}
    assert "ai_generated_statement_weak" not in factors
    assert "ai_generated_statement_medium" not in factors
    assert "ai_generated_statement_strong" not in factors


# -- paper grade on hard-decline path ----------------------------------------


def test_paper_grade_d_when_hard_declined(clean_deal: ScoreInput) -> None:
    deal = clean_deal.model_copy(update={"mca_positions": 5})
    result = score_deal(deal)
    assert result.tier == "F"
    assert result.paper_grade == "D"  # default for hard-decline path
    assert result.paper_grade_reasons == ["hard_decline"]
