"""Synthetic end-to-end test for the 3 real funders.

NOT a production script. Builds a representative deal (FL bakery,
$85K MRR, 680 FICO, 24 mo TIB, 1 stacked position), runs the matcher
against every active funder in the prod repo, and prints the match
result + submission-package preview for any non-red card.

Read-only: no DB writes, no CRM calls. Safe to run any time with the
funder repository bound to either backend.

Run on the box:
    set -a; source /etc/aegis/aegis.env; set +a
    cd /opt/aegis
    .venv/bin/python scripts/audit/test_real_funders_match.py
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from aegis.api.deps import get_funder_repository
from aegis.scoring.match_funders import match_funder
from aegis.scoring.models import ScoreInput, ScoreResult
from aegis.scoring.submission_package import build_submission_files


def main() -> None:
    deal = ScoreInput(
        merchant_id=uuid4(),
        business_name="Test Bakery LLC",
        owner_name="Test Owner",
        state="FL",
        industry_naics="722515",
        industry_risk_tier="moderate",
        time_in_business_months=24,
        credit_score=680,
        avg_daily_balance=Decimal("18000.00"),
        true_revenue=Decimal("85000.00"),
        monthly_revenue=Decimal("85000.00"),
        lowest_balance=Decimal("4200.00"),
        num_nsf=1,
        days_negative=0,
        mca_positions=1,
        mca_daily_total=Decimal("220.00"),
        debt_to_revenue=Decimal("0.08"),
        payroll_detected=True,
        returned_ach_count=0,
        statement_period_start=date(2026, 4, 1),
        statement_period_end=date(2026, 4, 30),
        statement_days=30,
        fraud_score=12,
        eof_markers=1,
        validation_passed=True,
        extraction_confidence=92,
        requested_amount=Decimal("60000.00"),
        requested_factor=Decimal("1.30"),
        requested_term_days=120,
    )

    score = ScoreResult(
        score=72,
        tier="B",
        recommendation="approve",
        hard_decline_reasons=[],
        soft_concerns=[],
        suggested_max_advance=Decimal("70000.00"),
        recommended_factor_rate=Decimal("1.29"),
        recommended_holdback_pct=Decimal("0.12"),
        estimated_payback_days=120,
    )

    print(
        f"Merchant: {deal.business_name} | state={deal.state} | "
        f"MRR=${deal.monthly_revenue} | FICO={deal.credit_score} | "
        f"TIB={deal.time_in_business_months}mo | positions={deal.mca_positions}"
    )
    print(f"Score: tier={score.tier} score={score.score} rec={score.recommendation}")
    print("-" * 80)

    repo = get_funder_repository()
    matches = []
    for f in repo.list_active():
        m = match_funder(f, deal, score)
        if m is None:
            print(f"SKIP   {f.name:30} (no configured criteria for this deal)")
            continue
        if m.match_score == 0:
            color = "RED"
        elif m.soft_concerns:
            color = "YELLOW"
        else:
            color = "GREEN"
        concerns = m.soft_concerns if m.soft_concerns else ["-"]
        print(
            f"{color:6} {f.name:30} score={m.match_score:3} "
            f"concerns={concerns}"
        )
        if m.match_score > 0:
            matches.append(m)

    print("-" * 80)
    print(f"Eligible (non-red) matches: {len(matches)}")

    if matches:
        files = build_submission_files(deal, score, matches)
        print(f"Submission package: {len(files)} CSV(s)")
        for sub in files:
            size = len(sub.csv_bytes)
            lines = sub.csv_bytes.decode().splitlines()
            preview = " | ".join(lines[:3])
            print(f"  {sub.filename} ({size}B)  preview: {preview}")


if __name__ == "__main__":
    main()
