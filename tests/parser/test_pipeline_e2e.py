"""End-to-end pipeline tests demonstrating Phase 2 review criteria.

(a) parsed PDF produces transactions with non-null source_page/source_line
(b) aggregates have non-empty _source_ids tracing back to real transactions
(c) validation gate fires on a deliberately math-broken statement

LLM is stubbed via fixtures in conftest.py — these tests exercise the
deterministic pipeline (metadata → extract → validate → classify →
patterns → aggregate), not Bedrock.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from aegis.parser.pipeline import run_pipeline

# (a) and (b)


def test_clean_pipeline_produces_sourced_transactions_and_aggregates(
    clean_pdf_path: Path,
    clean_llm: object,
) -> None:
    result = run_pipeline(str(clean_pdf_path), clean_llm, today=date(2026, 2, 15))  # type: ignore[arg-type]

    # Status: clean PDF + tied-out totals should not go to manual_review.
    assert result.parse_status in {"proceed", "review"}, (
        f"clean stmt unexpectedly went to manual_review; flags={result.all_flags}"
    )

    # (a) every transaction has a real source_page + source_line.
    assert result.classified, "no classified transactions"
    for txn in result.classified:
        assert txn.source_page >= 1, f"txn {txn.id} missing source_page"
        assert txn.source_line >= 1, f"txn {txn.id} missing source_line"

    # source_page / source_line variety: rows on page 2 exist (we built the
    # fixture with page 2 transactions) and source_lines are unique within page.
    pages_seen = {t.source_page for t in result.classified}
    assert pages_seen == {1, 2}, f"expected pages 1 and 2, saw {pages_seen}"
    page1_lines = [t.source_line for t in result.classified if t.source_page == 1]
    assert len(page1_lines) == len(set(page1_lines)), "duplicate source_line on page 1"

    # (b) aggregates carry source_ids that map back to real transactions.
    aggregates = result.aggregates
    assert aggregates is not None

    classified_ids = {t.id for t in result.classified}

    # avg_daily_balance includes every day's contributing transactions.
    assert aggregates.avg_daily_balance.source_ids, "avg_daily_balance has no sources"
    for sid in aggregates.avg_daily_balance.source_ids:
        assert sid in classified_ids, "avg_daily_balance source_id not in classified set"

    # true_revenue source_ids point at deposit-category rows.
    assert aggregates.true_revenue.source_ids, "true_revenue has no sources"
    deposit_ids = {t.id for t in result.classified if t.category == "deposit"}
    for sid in aggregates.true_revenue.source_ids:
        assert sid in deposit_ids, "true_revenue source_id is not a deposit"

    # mca_daily_total source_ids point at mca_debit rows.
    assert aggregates.mca_daily_total.source_ids, "mca_daily_total has no sources"
    mca_ids = {t.id for t in result.classified if t.category == "mca_debit"}
    for sid in aggregates.mca_daily_total.source_ids:
        assert sid in mca_ids, "mca_daily_total source_id is not an mca_debit"


# (c)


def test_math_broken_statement_fires_validation_gate(
    broken_pdf_path: Path,
    broken_llm: object,
) -> None:
    result = run_pipeline(str(broken_pdf_path), broken_llm, today=date(2026, 2, 15))  # type: ignore[arg-type]

    # Pipeline must short-circuit to manual_review without classifying / aggregating.
    assert result.parse_status == "manual_review", (
        f"math-broken statement should go to manual_review; got {result.parse_status} "
        f"flags={result.all_flags}"
    )
    assert result.aggregates is None, "aggregates must NOT be computed when validation fails"
    assert result.classified == [], "classification must NOT run when validation fails"

    # Specific failure code must appear so the operator knows why.
    assert any(
        f.startswith("reconciliation_failed_deposit_total")
        for f in result.validation.failures
    ), f"expected reconciliation_failed_deposit_total; saw {result.validation.failures}"
