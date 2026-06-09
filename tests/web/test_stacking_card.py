"""Tests for ``aegis.web._stacking_card`` — display payload for the
MCA stacking section of the merchant findings dossier.

R1.2 (audit remediation): the card exposes ``mca_pct_of_deposits`` so an
underwriter sees the MCA drain as a fraction of monthly revenue, not
just a dollar/day figure.

Convention covered by these tests:

* Monthly burden = ``mca_daily_total * 22`` (business days per month).
* Percent = ``monthly_burden / monthly_revenue * 100``, quantized to
  two decimal places (``Decimal("0.01")``).
* Values above 100% are NOT capped — a burden that exceeds revenue is
  the underwriting signal and must surface unclipped.
* ``monthly_revenue <= 0`` → ``mca_pct_of_deposits = None`` and the
  template omits the row.
* ``mca_daily_total == 0`` with non-zero revenue → ``Decimal("0.00")``
  (the card itself is only built when stacking OR daily-total is
  non-zero, so this branch is exercised through ``mca_positions > 0``).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from aegis.storage import AnalysisRow
from aegis.web._stacking_card import build_stacking_card


def _analysis(
    *,
    monthly_revenue: Decimal,
    mca_daily_total: Decimal,
    mca_positions: int = 1,
) -> AnalysisRow:
    """Minimal AnalysisRow stub for stacking-card math tests.

    Only the fields ``build_stacking_card`` touches are interesting;
    every other column gets a benign zero-ish value so Pydantic
    validation passes.
    """
    return AnalysisRow(
        id=uuid4(),
        document_id=uuid4(),
        merchant_id=None,
        statement_period_start=date(2026, 4, 1),
        statement_period_end=date(2026, 4, 30),
        statement_days=30,
        beginning_balance=Decimal("0.00"),
        ending_balance=Decimal("0.00"),
        avg_daily_balance=Decimal("0.00"),
        true_revenue=monthly_revenue,
        monthly_revenue=monthly_revenue,
        lowest_balance=Decimal("0.00"),
        num_nsf=0,
        days_negative=0,
        mca_positions=mca_positions,
        mca_daily_total=mca_daily_total,
        debt_to_revenue=Decimal("0.00"),
        payroll_detected=False,
        returned_ach_count=0,
    )


# ---------------------------------------------------------------------------
# mca_pct_of_deposits — core math + edge cases
# ---------------------------------------------------------------------------


def test_heavy_burden_exceeds_revenue_is_not_capped() -> None:
    """$4K monthly revenue, $1,240/day MCA → monthly burden $27,280 →
    ~682.00% of revenue. The audit-flagged use case: a burden well
    above revenue must surface as a >100% signal, NOT be clamped.
    """
    analysis = _analysis(
        monthly_revenue=Decimal("4000.00"),
        mca_daily_total=Decimal("1240.00"),
    )

    card = build_stacking_card(analysis, transactions=[])

    assert card is not None
    # 1240 * 22 = 27280; 27280 / 4000 * 100 = 682.00
    assert card.mca_pct_of_deposits == Decimal("682.00")
    # Sanity: monthly_burden agrees with the percent's numerator.
    assert card.monthly_burden == "27280.00"


def test_moderate_burden_renders_two_decimals() -> None:
    """$50K monthly revenue, $200/day MCA → monthly burden $4,400 →
    exactly 8.80% of revenue. Validates quantize-to-cents rendering.
    """
    analysis = _analysis(
        monthly_revenue=Decimal("50000.00"),
        mca_daily_total=Decimal("200.00"),
    )

    card = build_stacking_card(analysis, transactions=[])

    assert card is not None
    assert card.mca_pct_of_deposits == Decimal("8.80")
    assert card.monthly_burden == "4400.00"


def test_zero_mca_with_positive_revenue_renders_zero_percent() -> None:
    """$50K revenue, $0/day MCA, but mca_positions=1 (a pattern
    detector flagged a single MCA without a debit landing in-period).
    Convention: render 0.00%, not None. None is reserved for the
    no-revenue case where the percent is mathematically undefined.
    """
    analysis = _analysis(
        monthly_revenue=Decimal("50000.00"),
        mca_daily_total=Decimal("0.00"),
        mca_positions=1,
    )

    card = build_stacking_card(analysis, transactions=[])

    assert card is not None
    assert card.mca_pct_of_deposits == Decimal("0.00")


def test_zero_revenue_yields_none_so_template_omits_row() -> None:
    """$0 monthly revenue → field is None. The dossier template's
    ``{% if stacking.mca_pct_of_deposits is not none %}`` guard then
    suppresses the row so the operator never sees a misleading
    0.00% / divide-by-zero artifact.
    """
    analysis = _analysis(
        monthly_revenue=Decimal("0.00"),
        mca_daily_total=Decimal("500.00"),
    )

    card = build_stacking_card(analysis, transactions=[])

    assert card is not None
    assert card.mca_pct_of_deposits is None
