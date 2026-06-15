"""Sprint 4 Feature 2 — historical approval index aggregation.

Covers ``aegis.scoring.historical_approval.build_historical_approval_index``
and the partner lookup helper. The boost arithmetic that consumes the
returned ``Decimal`` lives in ``match_funders._apply_historical_boost``
and has its own test module.

Operator spec (verbatim, condensed):
    Query funder_note_submissions for last 90 days. If approval rate >
    60% for similar deals (same industry tier, same score tier) +5, if
    < 20% -10, cap at 100. Add historical_approval_rate to FunderMatch.
    No history = no adjustment. Insufficient sample (< 5 submissions) =
    no adjustment.

These tests exercise the **index** side of that contract — what the
route hands to the matcher.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from aegis.funder_note_submissions.models import (
    FunderNoteSubmissionRow,
    FunderNoteSubmissionStatus,
)
from aegis.scoring.historical_approval import (
    LOOKBACK_DAYS,
    MIN_SAMPLE_SIZE,
    build_historical_approval_index,
    lookup_historical_approval_rate,
)

_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


def _submission(
    *,
    funder_id: UUID,
    merchant_id: UUID,
    status: FunderNoteSubmissionStatus,
    submitted_at: datetime,
) -> FunderNoteSubmissionRow:
    return FunderNoteSubmissionRow(
        merchant_id=merchant_id,
        funder_id=funder_id,
        submitted_at=submitted_at,
        submitted_by="op@commerafunding.com",
        status=status,
    )


def test_sufficient_sample_above_threshold_emits_rate() -> None:
    funder = uuid4()
    merchants = [uuid4() for _ in range(5)]
    industry_map = {m: "Healthcare — Dental" for m in merchants}
    score_tier_map = {m: "B" for m in merchants}
    # 4 approved + 1 declined = 5 decided, 80% approval (> 60%).
    submissions = [
        _submission(
            funder_id=funder,
            merchant_id=merchants[i],
            status="approved" if i < 4 else "declined",
            submitted_at=_NOW - timedelta(days=10),
        )
        for i in range(5)
    ]
    index = build_historical_approval_index(
        submissions=submissions,
        industry_choice_by_merchant=industry_map,
        score_tier_by_merchant=score_tier_map,
        now=_NOW,
    )
    rate = lookup_historical_approval_rate(
        index,
        funder_id=funder,
        industry_tier="standard",
        score_tier="B",
    )
    assert rate is not None
    assert rate == Decimal("0.8000")


def test_insufficient_sample_dropped() -> None:
    """4 decided submissions = below the MIN_SAMPLE_SIZE floor (5).

    Cell must be ABSENT from the inner dict so callers get ``None`` via
    a plain dict.get without an extra check. Prevents a single-deal
    success or failure from swinging the matcher."""
    funder = uuid4()
    merchants = [uuid4() for _ in range(4)]
    industry_map = {m: "Healthcare — Dental" for m in merchants}
    score_tier_map = {m: "B" for m in merchants}
    submissions = [
        _submission(
            funder_id=funder,
            merchant_id=merchants[i],
            status="approved",
            submitted_at=_NOW - timedelta(days=10),
        )
        for i in range(4)
    ]
    assert len(submissions) < MIN_SAMPLE_SIZE
    index = build_historical_approval_index(
        submissions=submissions,
        industry_choice_by_merchant=industry_map,
        score_tier_by_merchant=score_tier_map,
        now=_NOW,
    )
    rate = lookup_historical_approval_rate(
        index,
        funder_id=funder,
        industry_tier="standard",
        score_tier="B",
    )
    assert rate is None


def test_lookback_filter_excludes_old_submissions() -> None:
    """Submissions outside the 90-day window must not count.

    Inside-window batch alone is below MIN_SAMPLE_SIZE; if the 91-day-
    old rows were counted the cell would have 6 decided. They are not,
    so the cell is dropped."""
    funder = uuid4()
    merchants = [uuid4() for _ in range(6)]
    industry_map = {m: "Healthcare — Dental" for m in merchants}
    score_tier_map = {m: "B" for m in merchants}
    inside = [
        _submission(
            funder_id=funder,
            merchant_id=merchants[i],
            status="approved",
            submitted_at=_NOW - timedelta(days=30),
        )
        for i in range(3)
    ]
    outside = [
        _submission(
            funder_id=funder,
            merchant_id=merchants[3 + i],
            status="approved",
            submitted_at=_NOW - timedelta(days=LOOKBACK_DAYS + 1),
        )
        for i in range(3)
    ]
    index = build_historical_approval_index(
        submissions=inside + outside,
        industry_choice_by_merchant=industry_map,
        score_tier_by_merchant=score_tier_map,
        now=_NOW,
    )
    # 3 inside < 5 = no cell.
    assert (
        lookup_historical_approval_rate(
            index, funder_id=funder, industry_tier="standard", score_tier="B"
        )
        is None
    )


def test_pending_submissions_excluded_from_denominator() -> None:
    """Pending submissions don't dilute the rate.

    5 approved + 10 pending should still read as 100% approval over 5
    decided, not 33% over 15. Matches the funder-performance page's
    denominator so the two surfaces stay consistent."""
    funder = uuid4()
    merchants = [uuid4() for _ in range(15)]
    industry_map = {m: "Healthcare — Dental" for m in merchants}
    score_tier_map = {m: "B" for m in merchants}
    decided = [
        _submission(
            funder_id=funder,
            merchant_id=merchants[i],
            status="approved",
            submitted_at=_NOW - timedelta(days=10),
        )
        for i in range(5)
    ]
    pending = [
        _submission(
            funder_id=funder,
            merchant_id=merchants[5 + i],
            status="pending",
            submitted_at=_NOW - timedelta(days=10),
        )
        for i in range(10)
    ]
    index = build_historical_approval_index(
        submissions=decided + pending,
        industry_choice_by_merchant=industry_map,
        score_tier_by_merchant=score_tier_map,
        now=_NOW,
    )
    rate = lookup_historical_approval_rate(
        index, funder_id=funder, industry_tier="standard", score_tier="B"
    )
    assert rate == Decimal("1.0000")


def test_tier_pairs_keyed_separately() -> None:
    """Submissions in DIFFERENT (industry, score) buckets MUST NOT mix.

    Five submissions split across two tier-pairs would yield two cells
    each with sample < MIN_SAMPLE_SIZE (3 and 2), so neither should be
    populated. This guards against the matcher reading a boost for a
    bucket it doesn't actually have history in."""
    funder = uuid4()
    # 3 submissions in (standard, B), 2 in (elevated, C) — neither hits 5.
    m_std = [uuid4() for _ in range(3)]
    m_elev = [uuid4() for _ in range(2)]
    industry_map: dict[UUID, str | None] = {}
    score_tier_map: dict[UUID, str] = {}
    for m in m_std:
        industry_map[m] = "Healthcare — Dental"
        score_tier_map[m] = "B"
    for m in m_elev:
        industry_map[m] = "Auto Repair / Service"
        score_tier_map[m] = "C"
    submissions = [
        _submission(
            funder_id=funder,
            merchant_id=m,
            status="approved",
            submitted_at=_NOW - timedelta(days=10),
        )
        for m in m_std + m_elev
    ]
    index = build_historical_approval_index(
        submissions=submissions,
        industry_choice_by_merchant=industry_map,
        score_tier_by_merchant=score_tier_map,
        now=_NOW,
    )
    assert (
        lookup_historical_approval_rate(
            index, funder_id=funder, industry_tier="standard", score_tier="B"
        )
        is None
    )
    assert (
        lookup_historical_approval_rate(
            index, funder_id=funder, industry_tier="elevated", score_tier="C"
        )
        is None
    )


def test_countered_counts_as_decided_not_approved() -> None:
    """Countered = the funder offered different terms = decided but
    not approved. Should land in the denominator, not the numerator."""
    funder = uuid4()
    merchants = [uuid4() for _ in range(5)]
    industry_map = {m: "Healthcare — Dental" for m in merchants}
    score_tier_map = {m: "B" for m in merchants}
    submissions = [
        _submission(
            funder_id=funder,
            merchant_id=merchants[i],
            status="approved" if i < 2 else "countered",
            submitted_at=_NOW - timedelta(days=10),
        )
        for i in range(5)
    ]
    index = build_historical_approval_index(
        submissions=submissions,
        industry_choice_by_merchant=industry_map,
        score_tier_by_merchant=score_tier_map,
        now=_NOW,
    )
    rate = lookup_historical_approval_rate(
        index, funder_id=funder, industry_tier="standard", score_tier="B"
    )
    # 2 approved / 5 decided = 0.4.
    assert rate == Decimal("0.4000")


def test_lookup_returns_none_for_unknown_funder() -> None:
    """A funder with zero history must read None, not 0.0 — that's the
    difference between 'no signal' and 'every prior deal was rejected'."""
    index: dict[UUID, dict[tuple[str, str], Decimal]] = {}
    rate = lookup_historical_approval_rate(
        index,  # type: ignore[arg-type]
        funder_id=uuid4(),
        industry_tier="standard",
        score_tier="B",
    )
    assert rate is None


def test_missing_merchant_industry_resolves_to_moderate() -> None:
    """If a submission's merchant isn't in the industry map (deleted
    merchant, race condition), industry_risk_tier(None) returns
    'moderate' — submission still counts, just in the moderate bucket."""
    funder = uuid4()
    merchants = [uuid4() for _ in range(5)]
    # Intentionally empty industry map.
    industry_map: dict[UUID, str | None] = {}
    score_tier_map = {m: "B" for m in merchants}
    submissions = [
        _submission(
            funder_id=funder,
            merchant_id=merchants[i],
            status="approved",
            submitted_at=_NOW - timedelta(days=10),
        )
        for i in range(5)
    ]
    index = build_historical_approval_index(
        submissions=submissions,
        industry_choice_by_merchant=industry_map,
        score_tier_by_merchant=score_tier_map,
        now=_NOW,
    )
    rate = lookup_historical_approval_rate(
        index, funder_id=funder, industry_tier="moderate", score_tier="B"
    )
    assert rate == Decimal("1.0000")
