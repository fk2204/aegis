"""Sprint-3 portfolio metrics — operator-facing aggregates over
``funder_note_submissions`` (migration 057).

Six metrics composed for the ``/ui/portfolio`` page:

* ``deals_this_month`` vs ``deals_last_month`` — submission count for
  the calendar-month containing ``now`` vs the prior calendar month.
* ``approval_rate_per_funder`` — for each funder with submissions in
  the last 90 days, approved-count / total-count.
* ``tier_breakdown`` — number of submissions whose merchant's latest
  decision (per ``DecisionSnapshot``) carries each tier letter
  (``A``..``F``). Submissions without a decision land in ``"unknown"``.
* ``top_industries_by_volume`` — three top ``industry_choice`` strings
  by submission count, last 90 days.
* ``top_industries_by_decline_rate`` — three top ``industry_choice``
  strings by declined-count / total-count, last 90 days, with a
  minimum-volume floor so a single-submission industry doesn't
  dominate at 100%.
* ``stale_merchants`` — finalized merchants with no submission AND
  no document upload in the last ``STALE_LOOKBACK_DAYS`` (30).

Pure functions. The route hands in fully-loaded lists (submissions in
the window, every merchant, every document, the decision snapshot
projection); the aggregator returns a Pydantic-strict view model the
template renders directly.

Money math is Decimal-only per CLAUDE.md; tier letters stay as
``str`` (industry-strings are operator-typed and may be ``None``).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aegis.funder_note_submissions.models import (
    FunderNoteSubmissionRow,
)
from aegis.merchants.models import MerchantRow
from aegis.storage import DocumentRow

# Look-back windows. ``APPROVAL_LOOKBACK_DAYS`` mirrors the operator's
# "last 90 days" framing for funder + industry metrics; the monthly
# comparison is calendar-month-aware so partial months don't undercount
# the current period. Stale lookback is 30 days per the prompt.
APPROVAL_LOOKBACK_DAYS: Final[int] = 90
STALE_LOOKBACK_DAYS: Final[int] = 30

# Trailing-window size for the monthly submission-volume bar chart.
# Six calendar months (the one containing ``now`` and the five prior)
# is the operator's preferred "last half year" view.
MONTHLY_VOLUME_LOOKBACK_MONTHS: Final[int] = 6

# Below this submission count an industry's decline-rate is suppressed
# from the top-3 list. A single declined submission would otherwise
# pin a one-deal industry at 100% and crowd out the operator's real
# concentration data.
_MIN_INDUSTRY_VOL_FOR_DECLINE_RATE: Final[int] = 3

_TOP_N: Final[int] = 3


@dataclass(frozen=True)
class _StatusCounter:
    """Per-bucket (funder or industry) submission-count rollup.

    ``total`` counts ALL submissions including pending. ``approved``,
    ``declined``, ``countered`` are the non-pending tallies. Pending
    derives as ``total - (approved + declined + countered)``.
    """

    total: int = 0
    approved: int = 0
    declined: int = 0
    countered: int = 0


class FunderApprovalRow(BaseModel):
    """One row of the per-funder approval-rate table."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    funder_id: UUID
    submitted: int
    approved: int
    declined: int
    countered: int
    pending: int
    approval_rate_pct: Decimal | None = Field(
        default=None,
        description=(
            "approved / submitted * 100 as a 1dp Decimal. ``None`` when "
            "``submitted`` is zero — surfaced as em-dash in the UI."
        ),
    )
    avg_response_hours: Decimal | None = Field(
        default=None,
        description=(
            "Mean hours from ``submitted_at`` to ``responded_at`` for the "
            "in-window non-pending submissions of this funder, as a 1dp "
            "Decimal. ``None`` when no funder reply has landed yet — "
            "surfaced as em-dash in the UI."
        ),
    )
    source_submission_ids: tuple[UUID, ...] = Field(
        default_factory=tuple,
        description=(
            "UUIDs of the funder_note_submissions rows that fed this "
            "aggregate (CLAUDE.md auditability rule). Order matches the "
            "in-window iteration order — not stability-sorted."
        ),
    )


class MonthlyVolumeBar(BaseModel):
    """One bar in the monthly-submission-volume chart.

    Bars are emitted for the last ``MONTHLY_VOLUME_LOOKBACK_MONTHS``
    calendar months ending in the month containing ``now``, oldest first
    (so the template can render left-to-right time progression). A
    bar's ``count`` is zero when the month has no submissions — the
    operator sees the absence rather than a hole in the axis.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    month_start: date
    month_label: str = Field(
        min_length=1,
        description="YYYY-MM string for the operator-facing axis label.",
    )
    count: int = Field(ge=0)
    source_submission_ids: tuple[UUID, ...] = Field(
        default_factory=tuple,
        description=(
            "UUIDs of every funder_note_submissions row whose "
            "``submitted_at`` falls in this month. Audit drill-down "
            "per CLAUDE.md."
        ),
    )


class IndustryRow(BaseModel):
    """One row of the top-industries tables."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    industry: str
    submitted: int
    declined: int
    decline_rate_pct: Decimal | None = Field(default=None)


class StaleMerchant(BaseModel):
    """One stale-merchant row.

    ``last_activity_at`` is the max of ``last_document_upload`` and
    ``last_submission`` — ``None`` when the merchant has neither. Days
    since is computed against the same ``now`` the route used.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    merchant_id: UUID
    business_name: str
    state: str | None
    last_activity_at: datetime | None
    days_since_activity: int | None


class Sprint3PortfolioMetrics(BaseModel):
    """The full Sprint-3 portfolio view-model handed to the template."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    deals_this_month: int
    deals_last_month: int
    deals_month_delta_pct: Decimal | None
    approval_rate_per_funder: tuple[FunderApprovalRow, ...]
    tier_breakdown: dict[str, int]
    # Tier breakdown restricted to the submissions in the current
    # calendar month (vs ``tier_breakdown`` which spans the full
    # input window). Powers the "average AEGIS score tier
    # breakdown — submitted deals this month" tile.
    tier_breakdown_this_month: dict[str, int]
    top_industries_by_volume: tuple[IndustryRow, ...]
    top_industries_by_decline_rate: tuple[IndustryRow, ...]
    stale_merchants: tuple[StaleMerchant, ...]
    # Last six calendar months of submission counts, oldest first.
    # Always six bars even when some months are empty — the UI needs
    # a stable axis the operator can read at a glance.
    monthly_volume_last_6: tuple[MonthlyVolumeBar, ...]


def _month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def _prev_month_start(d: date) -> date:
    s = _month_start(d)
    return date(s.year - 1, 12, 1) if s.month == 1 else date(s.year, s.month - 1, 1)


def _calendar_month_window(now: datetime) -> tuple[date, date, date, date]:
    """Return ``(this_start, this_end_exclusive, prev_start, prev_end_exclusive)``
    for the calendar month of ``now`` and the one prior.

    End-exclusive lets the caller filter ``submitted_at < this_end``
    without worrying about edge-of-month timestamps.
    """
    today = now.date()
    this_start = _month_start(today)
    next_start = (
        date(this_start.year + 1, 1, 1)
        if this_start.month == 12
        else date(this_start.year, this_start.month + 1, 1)
    )
    prev_start = _prev_month_start(today)
    return this_start, next_start, prev_start, this_start


def _count_in_month(
    submissions: Iterable[FunderNoteSubmissionRow],
    start: date,
    end_exclusive: date,
) -> int:
    return sum(
        1 for s in submissions if start <= s.submitted_at.astimezone(UTC).date() < end_exclusive
    )


def _pct(numerator: int, denominator: int) -> Decimal | None:
    """percentage as 1dp Decimal; ``None`` when denominator is zero."""
    if denominator <= 0:
        return None
    return (Decimal(numerator) * Decimal("100") / Decimal(denominator)).quantize(Decimal("0.1"))


def _month_delta_pct(this_m: int, last_m: int) -> Decimal | None:
    """(this - last) / last * 100 as 1dp Decimal. ``None`` when last_m is
    zero — a 0→N jump can't be expressed as a percentage."""
    if last_m <= 0:
        return None
    return ((Decimal(this_m) - Decimal(last_m)) * Decimal("100") / Decimal(last_m)).quantize(
        Decimal("0.1")
    )


def _tally(rows: Iterable[FunderNoteSubmissionRow]) -> _StatusCounter:
    total = approved = declined = countered = 0
    for r in rows:
        total += 1
        if r.status == "approved":
            approved += 1
        elif r.status == "declined":
            declined += 1
        elif r.status == "countered":
            countered += 1
    return _StatusCounter(
        total=total,
        approved=approved,
        declined=declined,
        countered=countered,
    )


def _avg_response_hours(rows: Iterable[FunderNoteSubmissionRow]) -> Decimal | None:
    """Mean response latency, in hours, across rows with a stamped
    ``responded_at``. ``None`` when no row in the bucket has a reply yet
    (the funder hasn't responded — em-dash in the UI, never a fake 0)."""
    total_seconds = Decimal("0")
    counted = 0
    for r in rows:
        if r.responded_at is None:
            continue
        delta = r.responded_at - r.submitted_at
        # ``timedelta`` total_seconds is a float, but the conversion to
        # Decimal via str preserves the exact integer second count
        # (``responded_at`` is stamped from ``NOW()`` at second
        # resolution, no fractional precision to round-trip).
        total_seconds += Decimal(str(delta.total_seconds()))
        counted += 1
    if counted == 0:
        return None
    hours = total_seconds / Decimal("3600") / Decimal(counted)
    return hours.quantize(Decimal("0.1"))


def _calendar_month_starts(now: datetime, months: int) -> list[date]:
    """Return the first day of each of the trailing ``months`` calendar
    months, oldest first. ``months=6`` with ``now=2026-06-15`` →
    ``[2026-01-01, 2026-02-01, ..., 2026-06-01]``."""
    today = now.astimezone(UTC).date()
    cur = _month_start(today)
    starts: list[date] = []
    for _ in range(months):
        starts.append(cur)
        cur = _prev_month_start(cur)
    starts.reverse()
    return starts


def _next_month_start(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def compute_sprint3_metrics(
    *,
    submissions: list[FunderNoteSubmissionRow],
    merchants: list[MerchantRow],
    documents: list[DocumentRow],
    latest_decision_tier_by_merchant: dict[UUID, str],
    now: datetime,
) -> Sprint3PortfolioMetrics:
    """Roll the Sprint-3 metrics from already-loaded lists.

    ``submissions`` is the full set in the >=90-day window the caller
    chose (this function does not re-filter — it trusts the route to
    bound the query). ``latest_decision_tier_by_merchant`` maps each
    merchant UUID to its most-recent decision's tier letter; merchants
    without a decision are absent from the dict (handled as
    ``"unknown"`` in the tier breakdown).

    ``now`` is injectable so tests can pin the month boundary.
    """
    now_utc = now.astimezone(UTC)
    today = now_utc.date()

    # ── Monthly comparison ─────────────────────────────────────────
    this_start, this_end, prev_start, prev_end = _calendar_month_window(now_utc)
    deals_this = _count_in_month(submissions, this_start, this_end)
    deals_last = _count_in_month(submissions, prev_start, prev_end)
    deals_delta = _month_delta_pct(deals_this, deals_last)

    # ── 90-day window for funder + industry metrics ───────────────
    cutoff_90 = now_utc - timedelta(days=APPROVAL_LOOKBACK_DAYS)
    last_90 = [s for s in submissions if s.submitted_at >= cutoff_90]

    # ── Approval rate per funder ──────────────────────────────────
    per_funder: dict[UUID, list[FunderNoteSubmissionRow]] = defaultdict(list)
    for s in last_90:
        per_funder[s.funder_id].append(s)
    funder_rows: list[FunderApprovalRow] = []
    for fid, rows in per_funder.items():
        t = _tally(rows)
        funder_rows.append(
            FunderApprovalRow(
                funder_id=fid,
                submitted=t.total,
                approved=t.approved,
                declined=t.declined,
                countered=t.countered,
                pending=t.total - (t.approved + t.declined + t.countered),
                approval_rate_pct=_pct(t.approved, t.total),
                avg_response_hours=_avg_response_hours(rows),
                source_submission_ids=tuple(r.id for r in rows),
            )
        )
    funder_rows.sort(key=lambda r: (-r.submitted, str(r.funder_id)))

    # ── Tier breakdown (over ALL submissions, not just 90 days) ───
    tier_counter: Counter[str] = Counter()
    for s in submissions:
        tier_counter[latest_decision_tier_by_merchant.get(s.merchant_id, "unknown")] += 1
    tier_breakdown = dict(tier_counter)

    # ── Tier breakdown — current calendar month only ──────────────
    # The "average AEGIS score tier breakdown — submitted deals this
    # month" tile reads from this restricted counter; the operator
    # wants to see *this period's* mix, not the cumulative 90-day
    # window.
    tier_counter_this_month: Counter[str] = Counter()
    for s in submissions:
        s_date = s.submitted_at.astimezone(UTC).date()
        if this_start <= s_date < this_end:
            tier_counter_this_month[
                latest_decision_tier_by_merchant.get(s.merchant_id, "unknown")
            ] += 1
    tier_breakdown_this_month = dict(tier_counter_this_month)

    # ── Monthly volume — trailing 6 calendar months ───────────────
    # One bar per month, oldest first. Empty months emit a zero bar
    # rather than a missing entry so the operator can read the axis
    # without guessing which months are missing.
    month_starts = _calendar_month_starts(now_utc, MONTHLY_VOLUME_LOOKBACK_MONTHS)
    by_month_ids: dict[date, list[UUID]] = {ms: [] for ms in month_starts}
    earliest_month = month_starts[0]
    latest_month_end = _next_month_start(month_starts[-1])
    for s in submissions:
        s_date = s.submitted_at.astimezone(UTC).date()
        if s_date < earliest_month or s_date >= latest_month_end:
            continue
        bucket = _month_start(s_date)
        # Defensive: only add if the bucket is one we asked for. Should
        # always hold given the earliest/latest guard above.
        if bucket in by_month_ids:
            by_month_ids[bucket].append(s.id)
    monthly_bars: list[MonthlyVolumeBar] = []
    for ms in month_starts:
        ids = by_month_ids[ms]
        monthly_bars.append(
            MonthlyVolumeBar(
                month_start=ms,
                month_label=ms.strftime("%Y-%m"),
                count=len(ids),
                source_submission_ids=tuple(ids),
            )
        )

    # ── Top industries (90-day window) ────────────────────────────
    merchants_by_id = {m.id: m for m in merchants}
    by_industry: dict[str, list[FunderNoteSubmissionRow]] = defaultdict(list)
    for s in last_90:
        merch = merchants_by_id.get(s.merchant_id)
        industry = (merch.industry_choice if merch else None) or "unknown"
        by_industry[industry].append(s)
    industry_volume: list[IndustryRow] = []
    industry_decline: list[IndustryRow] = []
    for industry, rows in by_industry.items():
        t = _tally(rows)
        industry_volume.append(
            IndustryRow(
                industry=industry,
                submitted=t.total,
                declined=t.declined,
                decline_rate_pct=_pct(t.declined, t.total),
            )
        )
        if t.total >= _MIN_INDUSTRY_VOL_FOR_DECLINE_RATE:
            industry_decline.append(
                IndustryRow(
                    industry=industry,
                    submitted=t.total,
                    declined=t.declined,
                    decline_rate_pct=_pct(t.declined, t.total),
                )
            )
    industry_volume.sort(key=lambda r: (-r.submitted, r.industry))
    industry_decline.sort(
        key=lambda r: (-(r.decline_rate_pct or Decimal("0")), -r.submitted, r.industry)
    )

    # ── Stale merchants (>=30d since any activity) ────────────────
    last_submission_by_merchant: dict[UUID, datetime] = {}
    for s in submissions:
        cur = last_submission_by_merchant.get(s.merchant_id)
        if cur is None or s.submitted_at > cur:
            last_submission_by_merchant[s.merchant_id] = s.submitted_at
    last_upload_by_merchant: dict[UUID, datetime] = {}
    for d in documents:
        if d.merchant_id is None:
            continue
        cur = last_upload_by_merchant.get(d.merchant_id)
        if cur is None or d.uploaded_at > cur:
            last_upload_by_merchant[d.merchant_id] = d.uploaded_at

    stale_cutoff = now_utc - timedelta(days=STALE_LOOKBACK_DAYS)
    stale_rows: list[StaleMerchant] = []
    for m in merchants:
        if not m.is_finalized:
            continue
        last_sub = last_submission_by_merchant.get(m.id)
        last_doc = last_upload_by_merchant.get(m.id)
        candidates = [t for t in (last_sub, last_doc) if t is not None]
        last_activity = max(candidates) if candidates else None
        if last_activity is not None and last_activity >= stale_cutoff:
            continue
        days_since = (
            (today - last_activity.astimezone(UTC).date()).days
            if last_activity is not None
            else None
        )
        stale_rows.append(
            StaleMerchant(
                merchant_id=m.id,
                business_name=m.business_name,
                state=m.state,
                last_activity_at=last_activity,
                days_since_activity=days_since,
            )
        )
    # Sort with merchants that have NEVER had activity at the bottom (None
    # last) so the oldest active-but-now-stale merchants surface first.
    stale_rows.sort(
        key=lambda r: (
            r.days_since_activity is None,
            -(r.days_since_activity or 0),
            r.business_name.lower(),
        )
    )

    return Sprint3PortfolioMetrics(
        deals_this_month=deals_this,
        deals_last_month=deals_last,
        deals_month_delta_pct=deals_delta,
        approval_rate_per_funder=tuple(funder_rows),
        tier_breakdown=tier_breakdown,
        tier_breakdown_this_month=tier_breakdown_this_month,
        top_industries_by_volume=tuple(industry_volume[:_TOP_N]),
        top_industries_by_decline_rate=tuple(industry_decline[:_TOP_N]),
        stale_merchants=tuple(stale_rows),
        monthly_volume_last_6=tuple(monthly_bars),
    )


__all__ = [
    "APPROVAL_LOOKBACK_DAYS",
    "MONTHLY_VOLUME_LOOKBACK_MONTHS",
    "STALE_LOOKBACK_DAYS",
    "FunderApprovalRow",
    "IndustryRow",
    "MonthlyVolumeBar",
    "Sprint3PortfolioMetrics",
    "StaleMerchant",
    "compute_sprint3_metrics",
]
