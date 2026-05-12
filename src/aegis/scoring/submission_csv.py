"""Funder-facing submission CSV.

This is the CSV the operator forwards to a funder. Shape is intentionally
different from ``web/_findings_csv.py``:

  * ``_findings_csv.py`` is the auditor-internal export (every flag,
    every source_id, every breakdown line — compliance evidence).
  * ``submission_csv.py`` is what the funder sees. Same merchant snapshot
    + bank summary + AEGIS verdict, scoped to ONE funder + the match
    notes specific to that funder.

PII rules: business_name, owner_name, state, NAICS, requested terms are
funder-required to evaluate the deal. EIN, SSN, account numbers, email,
phone are NOT included — funders ask for these post-approval through
their own intake forms, and AEGIS isn't licensed to disseminate them
broadly.

Stdlib ``csv`` only.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime

from aegis.scoring.models import FunderMatch, ScoreInput, ScoreResult

_GENERATOR_VERSION = "aegis-submission-1.0"


def build_submission_csv(
    *,
    deal: ScoreInput,
    score: ScoreResult,
    match: FunderMatch,
) -> str:
    """Build a per-funder submission CSV string.

    Sections (blank-line separated for Excel readability):
      1. Meta (generator, generated_at, funder_name)
      2. Merchant snapshot (PII-safe subset)
      3. Bank summary (last statement aggregates)
      4. AEGIS verdict (tier, score, recommended terms)
      5. Match notes (this funder's match score + concerns)
    """
    buf = io.StringIO(newline="")
    w = csv.writer(buf)

    # 1. Meta
    w.writerow(["section", "key", "value"])
    w.writerow(["meta", "generator", _GENERATOR_VERSION])
    w.writerow(
        [
            "meta",
            "generated_at",
            datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ]
    )
    w.writerow(["meta", "funder_name", match.funder_name])
    w.writerow([])

    # 2. Merchant snapshot (funder-required fields only)
    w.writerow(["section", "field", "value"])
    w.writerow(["merchant", "business_name", deal.business_name])
    w.writerow(["merchant", "owner_name", deal.owner_name])
    w.writerow(["merchant", "state", deal.state])
    w.writerow(["merchant", "industry_naics", deal.industry_naics or ""])
    w.writerow(["merchant", "industry_risk_tier", deal.industry_risk_tier or ""])
    w.writerow(
        [
            "merchant",
            "time_in_business_months",
            deal.time_in_business_months if deal.time_in_business_months is not None else "",
        ]
    )
    w.writerow(
        [
            "merchant",
            "credit_score",
            deal.credit_score if deal.credit_score is not None else "",
        ]
    )
    w.writerow(["merchant", "is_renewal", str(deal.is_renewal)])
    w.writerow([])

    # 3. Bank summary
    w.writerow(["section", "field", "value"])
    w.writerow(["bank", "statement_period_start", deal.statement_period_start.isoformat()])
    w.writerow(["bank", "statement_period_end", deal.statement_period_end.isoformat()])
    w.writerow(["bank", "statement_days", deal.statement_days])
    w.writerow(["bank", "monthly_revenue", str(deal.monthly_revenue)])
    w.writerow(["bank", "true_revenue", str(deal.true_revenue)])
    w.writerow(["bank", "avg_daily_balance", str(deal.avg_daily_balance)])
    w.writerow(["bank", "lowest_balance", str(deal.lowest_balance)])
    w.writerow(["bank", "num_nsf", deal.num_nsf])
    w.writerow(["bank", "days_negative", deal.days_negative])
    w.writerow(["bank", "mca_positions", deal.mca_positions])
    w.writerow(["bank", "mca_daily_total", str(deal.mca_daily_total)])
    w.writerow(["bank", "debt_to_revenue", str(deal.debt_to_revenue)])
    w.writerow(["bank", "payroll_detected", str(deal.payroll_detected)])
    w.writerow(["bank", "returned_ach_count", deal.returned_ach_count])
    w.writerow([])

    # 4. AEGIS verdict
    w.writerow(["section", "field", "value"])
    w.writerow(["aegis", "tier", score.tier])
    w.writerow(["aegis", "score", score.score])
    w.writerow(["aegis", "recommendation", score.recommendation])
    w.writerow(["aegis", "suggested_max_advance", str(score.suggested_max_advance)])
    w.writerow(["aegis", "recommended_factor_rate", str(score.recommended_factor_rate)])
    w.writerow(["aegis", "recommended_holdback_pct", str(score.recommended_holdback_pct)])
    w.writerow(
        [
            "aegis",
            "estimated_payback_days",
            score.estimated_payback_days if score.estimated_payback_days is not None else "",
        ]
    )
    w.writerow(["aegis", "apr", str(score.apr) if score.apr is not None else ""])
    w.writerow(["aegis", "hard_decline_reasons", "; ".join(score.hard_decline_reasons)])
    w.writerow(["aegis", "soft_concerns", "; ".join(score.soft_concerns)])
    w.writerow([])

    # 5. Match notes
    w.writerow(["section", "field", "value"])
    w.writerow(["match", "funder_name", match.funder_name])
    w.writerow(["match", "match_score", match.match_score])
    w.writerow(["match", "reasons", "; ".join(match.reasons)])
    w.writerow(["match", "concerns", "; ".join(match.soft_concerns)])

    return buf.getvalue()


__all__ = ["build_submission_csv"]
