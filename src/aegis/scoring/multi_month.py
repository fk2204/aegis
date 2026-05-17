"""Aggregate N monthly analyses into a single ScoreInput.

Single-month scoring lets a clean recent month mask trouble in prior
months (verified empirically with the KYC merchant: March 2026 scored
Tier A=100, but earlier months showed wash-deposit pairs, counterparty
concentration >100%, and preloan spikes). Industry-standard funder
underwriting reads trailing 3 statements; this module mirrors that.

Per-metric reduction rules — picked to match how an underwriter reads
each metric, not arithmetic convenience:

  * **summed over the window** — ``num_nsf``, ``days_negative``,
    ``returned_ach_count``, ``true_revenue`` (period becomes the whole
    window so ``monthly_revenue`` normalizes correctly)
  * **mean across months** — ``avg_daily_balance``, ``lowest_balance``,
    ``debt_to_revenue`` — average financial state
  * **max across months** — ``mca_positions``, ``fraud_score`` — worst
    observed risk should never be diluted by a quiet recent month
  * **latest only** — ``mca_daily_total``, ``payroll_detected`` —
    current obligation / employment state, not historical
  * **span** — start = earliest, end = latest, days = sum

A single-item list returns a ScoreInput functionally identical to the
single-month builder, so callers can use this unconditionally.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from aegis.merchants.models import MerchantRow
from aegis.scoring.models import ScoreInput
from aegis.storage import AnalysisRow, DocumentRow


def _project_monthly(period_revenue: Decimal, statement_days: int) -> Decimal:
    if statement_days <= 0:
        return Decimal("0.00")
    return (period_revenue / Decimal(statement_days) * Decimal(30)).quantize(
        Decimal("0.01")
    )


def score_input_multi_month(
    merchant: MerchantRow,
    items: list[tuple[DocumentRow, AnalysisRow]],
) -> ScoreInput:
    """Build a multi-month ScoreInput. ``items`` ordered newest-first."""
    if not items:
        raise ValueError("score_input_multi_month requires at least one analysis")

    _latest_doc, latest_analysis = items[0]
    analyses = [a for _, a in items]
    docs_list = [d for d, _ in items]
    n = Decimal(len(items))

    summed_revenue = sum((a.true_revenue for a in analyses), start=Decimal("0"))
    summed_days = sum(a.statement_days for a in analyses)

    mean_adb = (
        sum((a.avg_daily_balance for a in analyses), start=Decimal("0")) / n
    ).quantize(Decimal("0.01"))
    mean_lowest = (
        sum((a.lowest_balance for a in analyses), start=Decimal("0")) / n
    ).quantize(Decimal("0.01"))
    mean_dtr = (
        sum((a.debt_to_revenue for a in analyses), start=Decimal("0")) / n
    ).quantize(Decimal("0.0001"))

    return ScoreInput(
        merchant_id=merchant.id,
        business_name=merchant.business_name,
        owner_name=merchant.owner_name,
        state=merchant.state.upper(),
        industry_naics=merchant.industry_naics,
        industry_risk_tier=merchant.industry_risk_tier,
        time_in_business_months=merchant.time_in_business_months,
        credit_score=merchant.credit_score,
        avg_daily_balance=mean_adb,
        true_revenue=summed_revenue.quantize(Decimal("0.01")),
        monthly_revenue=_project_monthly(summed_revenue, summed_days),
        lowest_balance=mean_lowest,
        num_nsf=sum(a.num_nsf for a in analyses),
        days_negative=sum(a.days_negative for a in analyses),
        mca_positions=max(a.mca_positions for a in analyses),
        mca_daily_total=latest_analysis.mca_daily_total,
        debt_to_revenue=mean_dtr,
        payroll_detected=latest_analysis.payroll_detected,
        returned_ach_count=sum(a.returned_ach_count for a in analyses),
        statement_period_start=min(a.statement_period_start for a in analyses),
        statement_period_end=max(a.statement_period_end for a in analyses),
        statement_days=summed_days,
        fraud_score=max((d.fraud_score or 0) for d in docs_list),
        eof_markers=1,
        validation_passed=all(
            d.parse_status != "manual_review" for d in docs_list
        ),
        extraction_confidence=100,
        requested_amount=Decimal("50000.00"),
        requested_factor=Decimal("1.30"),
        requested_term_days=120,
    )


def detect_missing_months(
    items: list[tuple[DocumentRow, AnalysisRow]],
) -> list[str]:
    """Return calendar months absent from the bundle's coverage.

    Walks every month from ``min(statement_period_start)`` to
    ``max(statement_period_end)`` and returns those that don't appear in
    any analysis's ``monthly_breakdown``. Output is sorted "YYYY-MM"
    strings.

    Examples
    --------
    Three statements covering Jan/Feb/Mar 2026 — returns ``[]``.
    Jan + Mar statements (Feb missing) — returns ``["2026-02"]``.
    A single statement covering one month — returns ``[]``.
    An empty bundle — returns ``[]``.
    """
    if not items:
        return []

    earliest = min(a.statement_period_start for _, a in items)
    latest = max(a.statement_period_end for _, a in items)

    expected: list[str] = []
    cursor = date(earliest.year, earliest.month, 1)
    last_month = date(latest.year, latest.month, 1)
    while cursor <= last_month:
        expected.append(f"{cursor.year:04d}-{cursor.month:02d}")
        cursor = _next_month(cursor)

    actual: set[str] = set()
    for _, analysis in items:
        for entry in analysis.monthly_breakdown:
            month = entry.get("month")
            if month:
                actual.add(month)

    return [m for m in expected if m not in actual]


def _next_month(d: date) -> date:
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


__all__ = ["detect_missing_months", "score_input_multi_month"]
