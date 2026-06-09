"""Triage-backlog analytics (U24) — read-only aggregations across the
three operator triage queues.

The ``/ui/triage`` dashboard answers a single operator question: "where
is the human-review backlog right now?" — by tallying open rows across
three independent surfaces that, before this module, the operator had
to browse separately:

  * **Scoring disagreements** — ``scoring_shadow_disagreements`` rows
    with ``triaged_at IS NULL``. Source of truth: the open-triage view
    ``scoring_disagreements_open`` (migration 038). Surfaces as a
    by-category breakdown so the regression-sentinel bucket
    (``old-caught-something-new-misses``) is loud against the noise of
    the other four.
  * **Disclosure render events** — ``disclosure_render_events`` rows
    whose ``status`` is ``needs_review`` or ``apr_compute_failed``
    (the U21 actionable buckets) inside a date window. Source: the U16
    repository's ``list_in_window``.
  * **Shadow signals** — ``merchants_shadow_signals`` rows detected
    inside the window, grouped by ``signal_code``. Source: the U22
    repository's ``list_in_window`` (added in this same change).

Module shape mirrors ``aegis.deals.portfolio_analytics``:

  * One pure-data Pydantic model (``TriageBacklog``) holding the
    three queue tallies.
  * One orchestrator (``compute_triage_backlog``) that takes the three
    repositories and the date window and returns the assembled model.
  * Each repository fetch is wrapped in a defensive try/except so a
    Supabase blip on one queue does not 500 the whole page — the
    affected tile renders as zero counts (same posture as the
    portfolio render-queue tile).

Constraints (per U24 spec):

  * Read-only — no INSERTs / UPDATEs / DELETEs.
  * No new migrations / no new tables.
  * No coupling to the U4 triage CLI (``scripts/triage_disagreement.py``)
    — the CLI continues to own write paths; this dashboard is a strict
    reader.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from aegis.compliance.render_events import (
    RENDER_EVENT_STATUS_APR_FAILED,
    RENDER_EVENT_STATUS_NEEDS_REVIEW,
    RENDER_EVENT_STATUS_OK,
    DisclosureRenderEventRecord,
    DisclosureRenderEventRepository,
)
from aegis.merchants.shadow_signals import (
    MerchantShadowSignalRecord,
    MerchantShadowSignalRepository,
)
from aegis.scoring_v2.shadow_disagreements import (
    ScoringDisagreementRecord,
    ScoringDisagreementRepository,
)

# Default window for the triage dashboard. Matches the operator's
# weekly cadence — 30 days is "show me everything that has accumulated
# since the last full sweep" without overflowing the in-page tally.
DEFAULT_TRIAGE_WINDOW_DAYS: Final[int] = 30

# Hard cap on the operator-provided ``?days=N``. Same posture as the
# portfolio view's MAX_WINDOW_DAYS — bounds the worst-case fetch from
# Supabase even if the operator paste-bombs the query string.
MAX_TRIAGE_WINDOW_DAYS: Final[int] = 365

# Display order for the scoring-disagreement category breakdown.
# Regression sentinel first so the operator sees the loudest bucket
# without scrolling. Matches the ordering convention from
# ``scoring_v2.shadow_disagreements._CATEGORY_ORDER`` (which mirrors
# migration 038's view), kept literal here to avoid coupling the public
# dashboard model to a private constant.
DISAGREEMENT_CATEGORY_ORDER: Final[tuple[str, ...]] = (
    "old-caught-something-new-misses",
    "new-is-better",
    "genuinely-ambiguous",
    "agreement",
    "insufficient-new-data",
)


# ---------------------------------------------------------------------------
# Pydantic model
# ---------------------------------------------------------------------------


class _StrictModel(BaseModel):
    """Strict base mirroring ``portfolio_analytics._StrictModel``."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
        frozen=True,
    )


class TriageBacklog(_StrictModel):
    """Aggregate view of the three operator triage queues.

    All counts are non-negative integers (validated by ``ge=0``). The
    per-category / per-status / per-code breakdowns are insertion-
    ordered dicts so the template iterates in the order the analytics
    layer chose (regression sentinel first for disagreements, the
    actionable-buckets-only convention for render events).
    """

    # Window the tallies cover. Both ends inclusive — matches the U16
    # repository's ``list_in_window`` contract.
    from_date: date
    to_date: date

    # --- Scoring disagreements (U4 / migration 037+038) -------------------
    scoring_disagreements_open_count: int = Field(ge=0)
    """Open-triage row count across all categories."""

    scoring_disagreements_by_category: dict[str, int] = Field(default_factory=dict)
    """One entry per ``ALLOWED_CATEGORIES`` value; zeros INCLUDED so the
    template can render every bucket. Iteration order follows
    ``DISAGREEMENT_CATEGORY_ORDER`` (regression sentinel first)."""

    # --- Disclosure render events (U16 / U21 / migration 042) -------------
    render_events_actionable_count: int = Field(ge=0)
    """``needs_review`` + ``apr_compute_failed`` row count in the window —
    the two operator-actionable buckets. The ``ok`` bucket is excluded
    (informational only, surfaces on the portfolio page)."""

    render_events_by_status: dict[str, int] = Field(default_factory=dict)
    """Per-status breakdown. Keys are the three render-event statuses
    that have operator-meaning today (``needs_review`` /
    ``apr_compute_failed`` / ``ok``); future statuses tally under their
    own key. ``ok`` is included for context but the tile chip reads off
    ``render_events_actionable_count``."""

    # --- Shadow signals (U22 / migration 044) -----------------------------
    shadow_signals_count_in_window: int = Field(ge=0)
    """Total ``merchants_shadow_signals`` rows ``detected_at`` inside
    the window — across all signal codes + all merchants."""

    shadow_signals_by_code: dict[str, int] = Field(default_factory=dict)
    """Per-``signal_code`` breakdown. Sorted descending by count so the
    most-common signal lands at the top of the tile breakdown."""

    @property
    def total_actionable(self) -> int:
        """Sum of all three queue counts — the page-level "you have N
        items pending" banner number. The operator reads this first;
        the breakdowns drill down into which surface to open."""
        return (
            self.scoring_disagreements_open_count
            + self.render_events_actionable_count
            + self.shadow_signals_count_in_window
        )

    @property
    def is_empty(self) -> bool:
        """Convenience for the template's "no triage pending" branch."""
        return self.total_actionable == 0


# ---------------------------------------------------------------------------
# Date-range helper
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TriageWindow:
    """Validated, clamped triage date window."""

    from_date: date
    to_date: date
    days: int


def resolve_triage_window(days: int | None) -> TriageWindow:
    """Parse ``?days=N``; default + clamp to project bounds.

    ``days < 1`` is clamped to ``1`` so the window always covers at
    least today. ``days > MAX_TRIAGE_WINDOW_DAYS`` is clamped to the
    cap. ``None`` resolves to ``DEFAULT_TRIAGE_WINDOW_DAYS``. The route
    surfaces the effective span in the page banner so the cap is never
    silent.
    """
    effective = days if days is not None else DEFAULT_TRIAGE_WINDOW_DAYS
    if effective < 1:
        effective = 1
    if effective > MAX_TRIAGE_WINDOW_DAYS:
        effective = MAX_TRIAGE_WINDOW_DAYS
    today = datetime.now(UTC).date()
    return TriageWindow(
        from_date=today - timedelta(days=effective),
        to_date=today,
        days=effective,
    )


# ---------------------------------------------------------------------------
# Per-queue tallies (pure functions over already-fetched records)
# ---------------------------------------------------------------------------


def _tally_disagreements_by_category(
    rows: Sequence[ScoringDisagreementRecord],
) -> dict[str, int]:
    """Return a category → count map in display order.

    Every category in ``DISAGREEMENT_CATEGORY_ORDER`` is present in the
    output with a zero default so the template never crashes on a
    missing key. Unknown categories (defensive — the row is constrained
    to ``ALLOWED_CATEGORIES`` at write time) are appended after the
    known buckets to keep the loud-first ordering.
    """
    counts: dict[str, int] = {cat: 0 for cat in DISAGREEMENT_CATEGORY_ORDER}
    for r in rows:
        if r.category in counts:
            counts[r.category] += 1
        else:
            counts[r.category] = counts.get(r.category, 0) + 1
    return counts


def _tally_render_events_by_status(
    rows: Sequence[DisclosureRenderEventRecord],
) -> dict[str, int]:
    """Return a status → count map for the render-event tile.

    Keys are the three operator-meaningful statuses in display order:
    ``needs_review``, ``apr_compute_failed``, ``ok``. Other statuses
    (e.g. ``template_render_failed``) tally under their own key after
    the known three so the breakdown surfaces every status seen
    without burying actionable ones.
    """
    counts: dict[str, int] = {
        RENDER_EVENT_STATUS_NEEDS_REVIEW: 0,
        RENDER_EVENT_STATUS_APR_FAILED: 0,
        RENDER_EVENT_STATUS_OK: 0,
    }
    for r in rows:
        if r.status in counts:
            counts[r.status] += 1
        else:
            counts[r.status] = counts.get(r.status, 0) + 1
    return counts


def _render_events_actionable(rows: Sequence[DisclosureRenderEventRecord]) -> int:
    """Count the two operator-actionable render-event buckets."""
    return sum(
        1
        for r in rows
        if r.status
        in (RENDER_EVENT_STATUS_NEEDS_REVIEW, RENDER_EVENT_STATUS_APR_FAILED)
    )


def _tally_shadow_signals_by_code(
    rows: Sequence[MerchantShadowSignalRecord],
) -> dict[str, int]:
    """Return a signal_code → count map sorted descending by count.

    Ties break alphabetically on the code so the breakdown is
    deterministic across runs. Empty input returns an empty dict (the
    template branches on ``shadow_signals_count_in_window`` to render
    the empty state).
    """
    raw: dict[str, int] = {}
    for r in rows:
        raw[r.signal_code] = raw.get(r.signal_code, 0) + 1
    return dict(sorted(raw.items(), key=lambda kv: (-kv[1], kv[0])))


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def compute_triage_backlog(
    *,
    disagreements_repo: ScoringDisagreementRepository,
    render_events_repo: DisclosureRenderEventRepository,
    shadow_signals_repo: MerchantShadowSignalRepository,
    window: TriageWindow,
) -> TriageBacklog:
    """Fetch + tally the three triage queues for the operator dashboard.

    Each repository read is wrapped in try/except → zero tile so a
    Supabase blip on one queue never 500s the whole page. The route
    handler also logs ``triage.fetch_failed`` for visibility.

    ``window`` is honored for render events and shadow signals (both
    have a ``rendered_at`` / ``detected_at`` timestamp). Scoring
    disagreements are filtered by ``triaged_at IS NULL`` only —
    ``comparison_run_at`` is independent of the operator's chosen
    window because the corpus run cadence is bounded by the nightly
    cron, not by an arbitrary calendar window. Showing every open row
    matches the U4 CLI's default.
    """
    from aegis.logger import get_logger  # local import — log only on failure

    log = get_logger(__name__)

    try:
        disagreement_rows = list(disagreements_repo.list_open())
    except Exception:
        log.warning("triage.disagreements_fetch_failed")
        disagreement_rows = []

    try:
        render_event_rows = list(
            render_events_repo.list_in_window(
                from_date=window.from_date, to_date=window.to_date
            )
        )
    except Exception:
        log.warning(
            "triage.render_events_fetch_failed window=%s..%s",
            window.from_date,
            window.to_date,
        )
        render_event_rows = []

    try:
        shadow_signal_rows = list(
            shadow_signals_repo.list_in_window(
                from_date=window.from_date, to_date=window.to_date
            )
        )
    except Exception:
        log.warning(
            "triage.shadow_signals_fetch_failed window=%s..%s",
            window.from_date,
            window.to_date,
        )
        shadow_signal_rows = []

    disagreements_by_cat = _tally_disagreements_by_category(disagreement_rows)
    render_by_status = _tally_render_events_by_status(render_event_rows)
    shadow_by_code = _tally_shadow_signals_by_code(shadow_signal_rows)

    return TriageBacklog(
        from_date=window.from_date,
        to_date=window.to_date,
        scoring_disagreements_open_count=len(disagreement_rows),
        scoring_disagreements_by_category=disagreements_by_cat,
        render_events_actionable_count=_render_events_actionable(render_event_rows),
        render_events_by_status=render_by_status,
        shadow_signals_count_in_window=len(shadow_signal_rows),
        shadow_signals_by_code=shadow_by_code,
    )


__all__ = [
    "DEFAULT_TRIAGE_WINDOW_DAYS",
    "DISAGREEMENT_CATEGORY_ORDER",
    "MAX_TRIAGE_WINDOW_DAYS",
    "TriageBacklog",
    "TriageWindow",
    "compute_triage_backlog",
    "resolve_triage_window",
]


# ``time`` import kept for symmetry with portfolio_analytics — the
# shadow-signals repository's list_in_window accepts dates and expands
# to end-of-day internally. Re-exported indirectly via __all__ above
# only if a future caller needs it.
_ = time
