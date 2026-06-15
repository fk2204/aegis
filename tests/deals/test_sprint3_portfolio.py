"""Tests for ``aegis.deals.sprint3_portfolio.compute_sprint3_metrics``.

Six metrics, one pure function:

* monthly comparison (this month vs last, with %% delta when both
  populated and ``None`` when last_month=0)
* per-funder approval rate inside the 90-day window
* tier breakdown from latest-decision-tier-per-merchant
* top-3 industries by volume + by decline rate (with min-volume floor)
* stale merchants (no activity in 30+ days)

All cases use the in-memory model layer — no DB, no LLM.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from aegis.deals.sprint3_portfolio import (
    APPROVAL_LOOKBACK_DAYS,
    STALE_LOOKBACK_DAYS,
    compute_sprint3_metrics,
)
from aegis.funder_note_submissions.models import (
    FunderNoteSubmissionRow,
    FunderNoteSubmissionStatus,
)
from aegis.merchants.models import MerchantRow
from aegis.storage import DocumentRow

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


def _sub(
    *,
    merchant_id: UUID,
    funder_id: UUID,
    status: FunderNoteSubmissionStatus = "pending",
    submitted_at: datetime | None = None,
) -> FunderNoteSubmissionRow:
    when = submitted_at or _NOW
    return FunderNoteSubmissionRow(
        merchant_id=merchant_id,
        funder_id=funder_id,
        submitted_at=when,
        submitted_by="dashboard",
        status=status,
        funder_note="x",
        created_at=when,
        updated_at=when,
    )


def _merchant(
    *,
    industry_choice: str | None = None,
    state: str | None = "CA",
    business_name: str = "Acme LLC",
    status: str = "finalized",
) -> MerchantRow:
    return MerchantRow(
        id=uuid4(),
        status=status,
        business_name=business_name,
        state=state,
        industry_choice=industry_choice,
    )


def _doc(*, merchant_id: UUID | None, uploaded_at: datetime) -> DocumentRow:
    return DocumentRow(
        id=uuid4(),
        file_hash=f"sha256-{uuid4().hex}",
        byte_size=1024,
        original_filename="stmt.pdf",
        merchant_id=merchant_id,
        parse_status="proceed",
        uploaded_at=uploaded_at,
    )


# ---------------------------------------------------------------------------
# Monthly comparison
# ---------------------------------------------------------------------------


def test_monthly_comparison_counts_this_vs_last() -> None:
    m1 = _merchant()
    f1 = uuid4()
    # 3 submissions this month (June 2026), 2 last month (May 2026)
    subs = [
        _sub(merchant_id=m1.id, funder_id=f1, submitted_at=datetime(2026, 6, 1, tzinfo=UTC)),
        _sub(merchant_id=m1.id, funder_id=f1, submitted_at=datetime(2026, 6, 10, tzinfo=UTC)),
        _sub(merchant_id=m1.id, funder_id=f1, submitted_at=datetime(2026, 6, 14, tzinfo=UTC)),
        _sub(merchant_id=m1.id, funder_id=f1, submitted_at=datetime(2026, 5, 12, tzinfo=UTC)),
        _sub(merchant_id=m1.id, funder_id=f1, submitted_at=datetime(2026, 5, 28, tzinfo=UTC)),
    ]
    out = compute_sprint3_metrics(
        submissions=subs,
        merchants=[m1],
        documents=[],
        latest_decision_tier_by_merchant={},
        now=_NOW,
    )
    assert out.deals_this_month == 3
    assert out.deals_last_month == 2
    # +50% delta
    assert out.deals_month_delta_pct == Decimal("50.0")


def test_monthly_delta_none_when_last_month_is_zero() -> None:
    """0→N can't be expressed as a percentage; the field returns
    ``None`` so the UI surfaces an em-dash instead of a fake number."""
    m1 = _merchant()
    f1 = uuid4()
    out = compute_sprint3_metrics(
        submissions=[
            _sub(merchant_id=m1.id, funder_id=f1, submitted_at=datetime(2026, 6, 5, tzinfo=UTC)),
        ],
        merchants=[m1],
        documents=[],
        latest_decision_tier_by_merchant={},
        now=_NOW,
    )
    assert out.deals_this_month == 1
    assert out.deals_last_month == 0
    assert out.deals_month_delta_pct is None


def test_monthly_january_boundary_wraps_to_december_prior_year() -> None:
    """Calendar-month wraparound: Jan 2027 → Dec 2026 as last_month."""
    m1 = _merchant()
    f1 = uuid4()
    subs = [
        _sub(merchant_id=m1.id, funder_id=f1, submitted_at=datetime(2026, 12, 31, tzinfo=UTC)),
        _sub(merchant_id=m1.id, funder_id=f1, submitted_at=datetime(2027, 1, 5, tzinfo=UTC)),
    ]
    out = compute_sprint3_metrics(
        submissions=subs,
        merchants=[m1],
        documents=[],
        latest_decision_tier_by_merchant={},
        now=datetime(2027, 1, 15, tzinfo=UTC),
    )
    assert out.deals_this_month == 1
    assert out.deals_last_month == 1


# ---------------------------------------------------------------------------
# Per-funder approval rate (90-day window)
# ---------------------------------------------------------------------------


def test_approval_rate_per_funder_basic() -> None:
    """Two funders, one with 2-of-3 approved, one with 1-of-1 declined."""
    m1 = _merchant()
    f1 = uuid4()
    f2 = uuid4()
    subs = [
        _sub(merchant_id=m1.id, funder_id=f1, status="approved"),
        _sub(merchant_id=m1.id, funder_id=f1, status="approved"),
        _sub(merchant_id=m1.id, funder_id=f1, status="declined"),
        _sub(merchant_id=m1.id, funder_id=f2, status="declined"),
    ]
    out = compute_sprint3_metrics(
        submissions=subs,
        merchants=[m1],
        documents=[],
        latest_decision_tier_by_merchant={},
        now=_NOW,
    )
    f1_row = next(r for r in out.approval_rate_per_funder if r.funder_id == f1)
    assert f1_row.submitted == 3
    assert f1_row.approved == 2
    assert f1_row.declined == 1
    assert f1_row.approval_rate_pct == Decimal("66.7")
    f2_row = next(r for r in out.approval_rate_per_funder if r.funder_id == f2)
    assert f2_row.submitted == 1
    assert f2_row.approval_rate_pct == Decimal("0.0")


def test_approval_rate_excludes_submissions_outside_90_day_window() -> None:
    """Old submissions (>90 days back) don't contribute to the per-
    funder approval-rate table even though they appear in the monthly
    counters."""
    m1 = _merchant()
    f1 = uuid4()
    old = _NOW - timedelta(days=APPROVAL_LOOKBACK_DAYS + 5)
    subs = [
        _sub(merchant_id=m1.id, funder_id=f1, status="approved", submitted_at=old),
        _sub(merchant_id=m1.id, funder_id=f1, status="approved"),
    ]
    out = compute_sprint3_metrics(
        submissions=subs,
        merchants=[m1],
        documents=[],
        latest_decision_tier_by_merchant={},
        now=_NOW,
    )
    f1_row = next(r for r in out.approval_rate_per_funder if r.funder_id == f1)
    assert f1_row.submitted == 1  # only the in-window one


def test_pending_count_derived_from_total_minus_decided() -> None:
    m1 = _merchant()
    f1 = uuid4()
    subs = [
        _sub(merchant_id=m1.id, funder_id=f1, status="pending"),
        _sub(merchant_id=m1.id, funder_id=f1, status="pending"),
        _sub(merchant_id=m1.id, funder_id=f1, status="approved"),
        _sub(merchant_id=m1.id, funder_id=f1, status="countered"),
    ]
    out = compute_sprint3_metrics(
        submissions=subs,
        merchants=[m1],
        documents=[],
        latest_decision_tier_by_merchant={},
        now=_NOW,
    )
    row = next(r for r in out.approval_rate_per_funder if r.funder_id == f1)
    assert row.pending == 2


# ---------------------------------------------------------------------------
# Tier breakdown
# ---------------------------------------------------------------------------


def test_tier_breakdown_counts_by_latest_decision() -> None:
    m_a = _merchant()
    m_b = _merchant()
    m_c = _merchant()
    f1 = uuid4()
    subs = [
        _sub(merchant_id=m_a.id, funder_id=f1),
        _sub(merchant_id=m_a.id, funder_id=f1),  # same merchant — counts twice
        _sub(merchant_id=m_b.id, funder_id=f1),
        _sub(merchant_id=m_c.id, funder_id=f1),  # no decision -> unknown
    ]
    out = compute_sprint3_metrics(
        submissions=subs,
        merchants=[m_a, m_b, m_c],
        documents=[],
        latest_decision_tier_by_merchant={m_a.id: "A", m_b.id: "C"},
        now=_NOW,
    )
    assert out.tier_breakdown == {"A": 2, "C": 1, "unknown": 1}


# ---------------------------------------------------------------------------
# Industries
# ---------------------------------------------------------------------------


def test_top_industries_by_volume_top_3_only() -> None:
    f1 = uuid4()
    # 4 industries with submission counts 5/4/3/2 — top 3 surface
    merchants = []
    subs = []
    for industry, count in [("Healthcare", 5), ("Retail", 4), ("Trucking", 3), ("Legal", 2)]:
        m = _merchant(industry_choice=industry)
        merchants.append(m)
        for _ in range(count):
            subs.append(_sub(merchant_id=m.id, funder_id=f1))
    out = compute_sprint3_metrics(
        submissions=subs,
        merchants=merchants,
        documents=[],
        latest_decision_tier_by_merchant={},
        now=_NOW,
    )
    assert [r.industry for r in out.top_industries_by_volume] == [
        "Healthcare",
        "Retail",
        "Trucking",
    ]


def test_top_industries_by_decline_rate_floors_single_submission() -> None:
    """Decline-rate ranking excludes industries below the minimum
    volume floor so a single declined submission doesn't pin a
    one-deal industry at 100%."""
    f1 = uuid4()
    one_decline = _merchant(industry_choice="One-Off")
    three_split = _merchant(industry_choice="Big Industry")
    subs = [
        _sub(merchant_id=one_decline.id, funder_id=f1, status="declined"),
        _sub(merchant_id=three_split.id, funder_id=f1, status="declined"),
        _sub(merchant_id=three_split.id, funder_id=f1, status="approved"),
        _sub(merchant_id=three_split.id, funder_id=f1, status="approved"),
    ]
    out = compute_sprint3_metrics(
        submissions=subs,
        merchants=[one_decline, three_split],
        documents=[],
        latest_decision_tier_by_merchant={},
        now=_NOW,
    )
    industries = [r.industry for r in out.top_industries_by_decline_rate]
    assert "One-Off" not in industries
    assert "Big Industry" in industries


def test_industry_unknown_when_choice_missing() -> None:
    """Merchants without industry_choice land in an ``unknown`` bucket
    so the operator sees the data gap rather than crashing the page."""
    f1 = uuid4()
    m_none = _merchant(industry_choice=None)
    out = compute_sprint3_metrics(
        submissions=[_sub(merchant_id=m_none.id, funder_id=f1)],
        merchants=[m_none],
        documents=[],
        latest_decision_tier_by_merchant={},
        now=_NOW,
    )
    industries = [r.industry for r in out.top_industries_by_volume]
    assert industries == ["unknown"]


# ---------------------------------------------------------------------------
# Stale merchants
# ---------------------------------------------------------------------------


def test_stale_merchants_surface_when_no_recent_activity() -> None:
    m_active = _merchant(business_name="Active Inc")
    m_stale = _merchant(business_name="Stale LLC")
    f1 = uuid4()
    # m_active had a submission 10 days ago — fresh
    # m_stale's latest doc was uploaded 45 days ago — stale
    fresh_dt = _NOW - timedelta(days=10)
    old_dt = _NOW - timedelta(days=45)
    out = compute_sprint3_metrics(
        submissions=[
            _sub(merchant_id=m_active.id, funder_id=f1, submitted_at=fresh_dt),
        ],
        merchants=[m_active, m_stale],
        documents=[
            _doc(merchant_id=m_stale.id, uploaded_at=old_dt),
        ],
        latest_decision_tier_by_merchant={},
        now=_NOW,
    )
    stale_names = [r.business_name for r in out.stale_merchants]
    assert "Stale LLC" in stale_names
    assert "Active Inc" not in stale_names
    stale_row = next(r for r in out.stale_merchants if r.business_name == "Stale LLC")
    assert stale_row.days_since_activity == 45


def test_stale_merchants_excludes_non_finalized() -> None:
    """A provisional merchant — the auto-created placeholder upload
    state — shouldn't surface as stale; it hasn't been named yet, so
    no operator follow-up is owed."""
    m = _merchant(business_name="Provisional", status="provisional")
    out = compute_sprint3_metrics(
        submissions=[],
        merchants=[m],
        documents=[],
        latest_decision_tier_by_merchant={},
        now=_NOW,
    )
    assert not out.stale_merchants


def test_stale_merchants_includes_never_active_at_bottom() -> None:
    """A finalized merchant with no submission AND no document ever
    appears (never-active is still stale), but sorts last so
    long-stale active merchants surface first."""
    m_never = _merchant(business_name="Never Active LLC")
    m_long_stale = _merchant(business_name="Long Stale LLC")
    out = compute_sprint3_metrics(
        submissions=[],
        merchants=[m_never, m_long_stale],
        documents=[
            _doc(
                merchant_id=m_long_stale.id,
                uploaded_at=_NOW - timedelta(days=120),
            ),
        ],
        latest_decision_tier_by_merchant={},
        now=_NOW,
    )
    names = [r.business_name for r in out.stale_merchants]
    assert names == ["Long Stale LLC", "Never Active LLC"]


def test_stale_threshold_exact_boundary() -> None:
    """A merchant whose last activity is exactly STALE_LOOKBACK_DAYS-1
    days old is NOT stale; one day older IS."""
    m_fresh = _merchant(business_name="Day29 LLC")
    m_stale = _merchant(business_name="Day31 LLC")
    out = compute_sprint3_metrics(
        submissions=[],
        merchants=[m_fresh, m_stale],
        documents=[
            _doc(
                merchant_id=m_fresh.id,
                uploaded_at=_NOW - timedelta(days=STALE_LOOKBACK_DAYS - 1),
            ),
            _doc(
                merchant_id=m_stale.id,
                uploaded_at=_NOW - timedelta(days=STALE_LOOKBACK_DAYS + 1),
            ),
        ],
        latest_decision_tier_by_merchant={},
        now=_NOW,
    )
    names = [r.business_name for r in out.stale_merchants]
    assert names == ["Day31 LLC"]
