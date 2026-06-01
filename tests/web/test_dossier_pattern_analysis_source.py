"""Tests for ``_dossier_pattern_analysis`` — the chunk-3-followup
helper that closes backlog item #6 (dossier reads stored
``pattern_analysis`` instead of recomputing on every render).

Locks down the prefer-stored-with-fallback contract:

* When ``AnalysisRow.pattern_analysis`` is populated (every row
  parsed since stage 2 chunk 2 deployed 2026-05-29), the helper
  returns the DTO converted back to the runtime ``PatternAnalysis``
  dataclass via ``pattern_analysis_from_dto``. No live recomputation.
* When the cache is ``None`` (legacy rows parsed before chunk 2),
  the helper falls back to ``analyze_patterns()`` over the doc's
  classified transactions. Same behavior as the pre-cleanup call
  sites.
* If the fallback recomputation raises (defensive — malformed
  transactions, unexpected date ranges), the helper returns
  ``None`` rather than crashing the dossier render — same
  try/except contract the prior call-site code carried.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from aegis.parser.models import ClassifiedTransaction
from aegis.parser.patterns import (
    PatternAnalysis,
    PatternAnalysisDTO,
    PatternDTO,
)
from aegis.storage import AnalysisRow
from aegis.web.router import _dossier_pattern_analysis


def _analysis(
    *, pattern_analysis_dto: PatternAnalysisDTO | None = None
) -> AnalysisRow:
    """AnalysisRow stub with the minimum fields required to construct
    the model. Only ``pattern_analysis``, ``statement_period_start``,
    and ``statement_period_end`` matter for this helper's behavior."""
    return AnalysisRow(
        id=uuid4(),
        document_id=uuid4(),
        merchant_id=None,
        statement_period_start=date(2026, 4, 1),
        statement_period_end=date(2026, 4, 30),
        statement_days=30,
        beginning_balance=Decimal("1000.00"),
        ending_balance=Decimal("2000.00"),
        avg_daily_balance=Decimal("1500.00"),
        true_revenue=Decimal("5000.00"),
        monthly_revenue=Decimal("5000.00"),
        lowest_balance=Decimal("500.00"),
        num_nsf=0,
        days_negative=0,
        mca_positions=0,
        mca_daily_total=Decimal("0.00"),
        debt_to_revenue=Decimal("0.00"),
        payroll_detected=False,
        returned_ach_count=0,
        pattern_analysis=pattern_analysis_dto,
    )


def test_returns_cache_converted_to_dataclass_when_populated() -> None:
    """When AnalysisRow.pattern_analysis is non-None, the helper
    returns the cached DTO converted to the runtime PatternAnalysis
    dataclass — NO live recomputation. Tested with an empty
    transactions list to prove the cache, not the transactions, drove
    the result."""
    src_id = uuid4()
    dto = PatternAnalysisDTO(
        patterns=[
            PatternDTO(
                code="wash_deposit_suspected",
                severity=35,
                detail="2 round-trip pairs within 5 days",
                source_ids=[src_id],
            ),
        ],
    )
    analysis = _analysis(pattern_analysis_dto=dto)

    result = _dossier_pattern_analysis(analysis, latest_transactions=[])

    assert isinstance(result, PatternAnalysis)
    assert [p.code for p in result.patterns] == ["wash_deposit_suspected"]
    assert result.patterns[0].severity == 35
    assert result.patterns[0].source_ids == [src_id]


def test_falls_back_to_recompute_when_cache_is_none() -> None:
    """Legacy AnalysisRow rows (parsed before stage 2 chunk 2) carry
    pattern_analysis=None. The helper falls back to analyze_patterns
    over the doc's classified transactions. Empty transactions yield
    an empty patterns list — proves the recompute path ran without
    crashing."""
    analysis = _analysis(pattern_analysis_dto=None)

    result = _dossier_pattern_analysis(analysis, latest_transactions=[])

    assert isinstance(result, PatternAnalysis)
    assert result.patterns == []


def test_cache_path_does_not_match_transactions_used() -> None:
    """Sanity that cache-vs-recompute split is real: a populated cache
    surfaces the cached patterns even when the transactions would
    produce a different (empty) set on recomputation. Without this
    test, a regression that accidentally always recomputed would still
    pass the prior test (both paths would return empty for empty
    transactions)."""
    dto = PatternAnalysisDTO(
        patterns=[
            PatternDTO(
                code="round_number_deposits",
                severity=15,
                detail="cached pattern that recomputation wouldn't find",
                source_ids=[uuid4()],
            ),
        ],
    )
    analysis = _analysis(pattern_analysis_dto=dto)
    # Pass real-ish transactions that wouldn't trigger
    # round_number_deposits when recomputed (empty would work too;
    # the point is the cache wins).
    transactions = [
        ClassifiedTransaction(
            id=uuid4(),
            posted_date=date(2026, 4, 5),
            description="LIVE TX",
            amount=Decimal("37.42"),  # Not a round-number multiple of 100
            running_balance=None,
            source_page=1,
            source_line=10,
            category="deposit",
            classification_confidence=95,
        ),
    ]

    result = _dossier_pattern_analysis(analysis, latest_transactions=transactions)

    assert result is not None
    assert [p.code for p in result.patterns] == ["round_number_deposits"]
    assert result.patterns[0].detail == (
        "cached pattern that recomputation wouldn't find"
    )
