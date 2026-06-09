"""Unit tests for ``aegis.deals.triage_analytics`` (U24 orchestrator).

Exercises the pure-data layer that powers the ``/ui/triage`` dashboard
without spinning up the FastAPI app. Three concerns:

  * ``resolve_triage_window`` honors default + clamp + None.
  * ``compute_triage_backlog`` aggregates the three repositories in the
    right order and surfaces the right breakdowns.
  * A repository raising on ``list_open`` / ``list_in_window`` does NOT
    propagate — the affected tile zeros out (defensive posture mirrors
    the portfolio render-queue tile).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from aegis.compliance.render_events import (
    RENDER_EVENT_STATUS_APR_FAILED,
    RENDER_EVENT_STATUS_NEEDS_REVIEW,
    RENDER_EVENT_STATUS_OK,
    InMemoryDisclosureRenderEventRepository,
)
from aegis.deals.triage_analytics import (
    DEFAULT_TRIAGE_WINDOW_DAYS,
    DISAGREEMENT_CATEGORY_ORDER,
    MAX_TRIAGE_WINDOW_DAYS,
    compute_triage_backlog,
    resolve_triage_window,
)
from aegis.merchants.shadow_signals import InMemoryMerchantShadowSignalRepository
from aegis.scoring_v2.shadow_disagreements import (
    CATEGORY_NEW_BETTER,
    CATEGORY_OLD_BETTER,
    InMemoryScoringDisagreementRepository,
)

# ---------------------------------------------------------------------------
# resolve_triage_window
# ---------------------------------------------------------------------------


def test_resolve_window_defaults_to_30_days() -> None:
    """No ``days`` arg → DEFAULT_TRIAGE_WINDOW_DAYS."""
    window = resolve_triage_window(None)
    assert window.days == DEFAULT_TRIAGE_WINDOW_DAYS
    assert (window.to_date - window.from_date).days == DEFAULT_TRIAGE_WINDOW_DAYS


def test_resolve_window_clamps_upper_bound() -> None:
    """``days > MAX_TRIAGE_WINDOW_DAYS`` → clamped to the cap."""
    window = resolve_triage_window(10_000)
    assert window.days == MAX_TRIAGE_WINDOW_DAYS


def test_resolve_window_clamps_lower_bound() -> None:
    """``days < 1`` → clamped to 1 (always at least today)."""
    window = resolve_triage_window(0)
    assert window.days == 1


def test_resolve_window_honors_explicit_value() -> None:
    """Sensible value passes through unchanged."""
    window = resolve_triage_window(7)
    assert window.days == 7


# ---------------------------------------------------------------------------
# compute_triage_backlog — happy path
# ---------------------------------------------------------------------------


def _populate_disagreements(
    repo: InMemoryScoringDisagreementRepository, *, regressions: int, new_better: int
) -> None:
    """Drop ``regressions`` regression-sentinel rows + ``new_better``
    new-is-better rows into the repo."""
    for _ in range(regressions):
        repo.record(
            merchant_id=uuid4(),
            deal_id=None,
            category=CATEGORY_OLD_BETTER,
            legacy_fraud_score=72,
            legacy_tier="D",
            legacy_recommendation="decline",
            legacy_hard_declines=["wash_deposit_suspected"],
            track_a_verdict="pass",
            track_b_band="material",
            track_c_panel=None,
            evidence={"diff": f"item-{uuid4()}"},
        )
    for _ in range(new_better):
        repo.record(
            merchant_id=uuid4(),
            deal_id=None,
            category=CATEGORY_NEW_BETTER,
            legacy_fraud_score=42,
            legacy_tier="B",
            legacy_recommendation="approve",
            legacy_hard_declines=None,
            track_a_verdict="hold",
            track_b_band="elevated",
            track_c_panel=None,
            evidence={"diff": f"item-{uuid4()}"},
        )


def _populate_render_events(
    repo: InMemoryDisclosureRenderEventRepository,
    *,
    needs_review: int,
    apr_failed: int,
    ok: int,
) -> None:
    for _ in range(needs_review):
        repo.record(
            deal_id=uuid4(),
            merchant_id=uuid4(),
            state="CA",
            template_path="compliance/templates/ca.html.j2",
            status=RENDER_EVENT_STATUS_NEEDS_REVIEW,
            status_reason="needs review",
            details={"term_days": 180},
            recipient_email=None,
            rendered_by="api",
        )
    for _ in range(apr_failed):
        repo.record(
            deal_id=uuid4(),
            merchant_id=uuid4(),
            state="NY",
            template_path="compliance/templates/ny.html.j2",
            status=RENDER_EVENT_STATUS_APR_FAILED,
            status_reason="brentq failed",
            details={"factor": "1.35"},
            recipient_email=None,
            rendered_by="api",
        )
    for _ in range(ok):
        repo.record(
            deal_id=uuid4(),
            merchant_id=uuid4(),
            state="TX",
            template_path="compliance/templates/tx.html.j2",
            status=RENDER_EVENT_STATUS_OK,
            status_reason=None,
            details=None,
            recipient_email=None,
            rendered_by="api",
        )


def _populate_shadow_signals(
    repo: InMemoryMerchantShadowSignalRepository,
    *,
    by_code: dict[str, int],
) -> None:
    for code, n in by_code.items():
        for _ in range(n):
            repo.record(
                merchant_id=uuid4(),
                signal_code=code,
                signal_severity=0,
                detail=f"{code}:sample",
                source_document_id=uuid4(),
                source_ids=[uuid4()],
                metadata=None,
                detected_by="worker",
            )


def test_compute_backlog_aggregates_all_three_queues() -> None:
    """All three repos populated → all three tile counts non-zero."""
    disagreements = InMemoryScoringDisagreementRepository()
    render_events = InMemoryDisclosureRenderEventRepository()
    shadow_signals = InMemoryMerchantShadowSignalRepository()

    _populate_disagreements(disagreements, regressions=2, new_better=1)
    _populate_render_events(render_events, needs_review=3, apr_failed=2, ok=4)
    _populate_shadow_signals(
        shadow_signals,
        by_code={
            "duplicate_pdf_upload": 5,
            "related_account_suspected": 2,
        },
    )

    window = resolve_triage_window(30)
    backlog = compute_triage_backlog(
        disagreements_repo=disagreements,
        render_events_repo=render_events,
        shadow_signals_repo=shadow_signals,
        window=window,
    )

    # Disagreements: 3 total, regression-sentinel first.
    assert backlog.scoring_disagreements_open_count == 3
    assert backlog.scoring_disagreements_by_category[CATEGORY_OLD_BETTER] == 2
    assert backlog.scoring_disagreements_by_category[CATEGORY_NEW_BETTER] == 1

    # Iteration order: regression-sentinel comes first.
    keys = list(backlog.scoring_disagreements_by_category.keys())
    assert keys[0] == DISAGREEMENT_CATEGORY_ORDER[0]
    assert keys[0] == CATEGORY_OLD_BETTER

    # Render events: only the two actionable buckets count toward the chip.
    assert backlog.render_events_actionable_count == 3 + 2
    assert backlog.render_events_by_status[RENDER_EVENT_STATUS_NEEDS_REVIEW] == 3
    assert backlog.render_events_by_status[RENDER_EVENT_STATUS_APR_FAILED] == 2
    assert backlog.render_events_by_status[RENDER_EVENT_STATUS_OK] == 4

    # Shadow signals: 7 total, breakdown sorted descending by count.
    assert backlog.shadow_signals_count_in_window == 7
    code_keys = list(backlog.shadow_signals_by_code.keys())
    assert code_keys == ["duplicate_pdf_upload", "related_account_suspected"]
    assert backlog.shadow_signals_by_code["duplicate_pdf_upload"] == 5

    # Page-level total covers all three.
    assert backlog.total_actionable == 3 + 5 + 7
    assert backlog.is_empty is False


def test_compute_backlog_empty_repos_returns_zero_counts() -> None:
    """All three repos empty → all counts zero, is_empty True."""
    backlog = compute_triage_backlog(
        disagreements_repo=InMemoryScoringDisagreementRepository(),
        render_events_repo=InMemoryDisclosureRenderEventRepository(),
        shadow_signals_repo=InMemoryMerchantShadowSignalRepository(),
        window=resolve_triage_window(30),
    )
    assert backlog.scoring_disagreements_open_count == 0
    assert backlog.render_events_actionable_count == 0
    assert backlog.shadow_signals_count_in_window == 0
    assert backlog.total_actionable == 0
    assert backlog.is_empty is True
    # Every known category still keyed in the dict (with a 0).
    for cat in DISAGREEMENT_CATEGORY_ORDER:
        assert backlog.scoring_disagreements_by_category[cat] == 0


def test_compute_backlog_narrows_shadow_signals_to_window() -> None:
    """Shadow signals older than the window are excluded from the tally."""
    shadow_signals = InMemoryMerchantShadowSignalRepository()
    # In-window: detected today.
    shadow_signals.record(
        merchant_id=uuid4(),
        signal_code="duplicate_pdf_upload",
        signal_severity=0,
        detail=None,
        source_document_id=None,
        source_ids=[],
        metadata=None,
        detected_by="worker",
        detected_at=datetime.now(UTC),
    )
    # Out-of-window: detected 60 days ago, window is 7 days.
    shadow_signals.record(
        merchant_id=uuid4(),
        signal_code="duplicate_pdf_upload",
        signal_severity=0,
        detail=None,
        source_document_id=None,
        source_ids=[],
        metadata=None,
        detected_by="worker",
        detected_at=datetime.now(UTC) - timedelta(days=60),
    )

    backlog = compute_triage_backlog(
        disagreements_repo=InMemoryScoringDisagreementRepository(),
        render_events_repo=InMemoryDisclosureRenderEventRepository(),
        shadow_signals_repo=shadow_signals,
        window=resolve_triage_window(7),
    )
    assert backlog.shadow_signals_count_in_window == 1


# ---------------------------------------------------------------------------
# compute_triage_backlog — defensive posture
# ---------------------------------------------------------------------------


class _BrokenDisagreements:
    """Repository whose ``list_open`` raises — exercises the swallow path."""

    def list_open(self, **_: Any) -> list[Any]:
        raise RuntimeError("supabase unreachable")

    def list_all(self, **_: Any) -> list[Any]:
        return []

    def record(self, **_: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    def get(self, _id: Any) -> Any:  # pragma: no cover - unused
        return None

    def record_triage_decision(self, **_: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


def test_compute_backlog_swallows_disagreements_fetch_failure() -> None:
    """A broken disagreement repo zeros that tile rather than 500-ing."""
    render_events = InMemoryDisclosureRenderEventRepository()
    _populate_render_events(render_events, needs_review=1, apr_failed=0, ok=0)
    shadow_signals = InMemoryMerchantShadowSignalRepository()

    backlog = compute_triage_backlog(
        disagreements_repo=_BrokenDisagreements(),
        render_events_repo=render_events,
        shadow_signals_repo=shadow_signals,
        window=resolve_triage_window(30),
    )
    # Failed tile is zero.
    assert backlog.scoring_disagreements_open_count == 0
    # Healthy tile still tallies.
    assert backlog.render_events_actionable_count == 1
