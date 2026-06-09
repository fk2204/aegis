"""Tests for ``GET /ui/triage`` (U24 — aggregate triage backlog).

Covers:

  * Empty repos → 200, banner copy, "no triage pending" message.
  * Populated repos → tile counts surface in the rendered body.
  * Date-range query narrows results (``?days=N``).
  * Cap-clamp: ``?days=10000`` still returns 200 (clamped to 365).

The route is exercised through ``TestClient`` with all three repos
injected via ``app.dependency_overrides``. Mirrors the
``test_disclosure_events_route`` pattern.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_disclosure_render_event_repository,
    get_merchant_shadow_signal_repository,
    get_scoring_disagreement_repository,
    reset_dependency_caches,
)
from aegis.compliance.render_events import (
    RENDER_EVENT_STATUS_APR_FAILED,
    RENDER_EVENT_STATUS_NEEDS_REVIEW,
    InMemoryDisclosureRenderEventRepository,
)
from aegis.merchants.shadow_signals import InMemoryMerchantShadowSignalRepository
from aegis.scoring_v2.shadow_disagreements import (
    CATEGORY_OLD_BETTER,
    InMemoryScoringDisagreementRepository,
)


@pytest.fixture
def triage_client() -> Iterator[
    tuple[
        TestClient,
        InMemoryScoringDisagreementRepository,
        InMemoryDisclosureRenderEventRepository,
        InMemoryMerchantShadowSignalRepository,
    ]
]:
    """TestClient with all three triage repos pinned to in-memory."""
    reset_dependency_caches()
    disagreements = InMemoryScoringDisagreementRepository()
    render_events = InMemoryDisclosureRenderEventRepository()
    shadow_signals = InMemoryMerchantShadowSignalRepository()

    app = create_app()
    app.dependency_overrides[get_scoring_disagreement_repository] = (
        lambda: disagreements
    )
    app.dependency_overrides[get_disclosure_render_event_repository] = (
        lambda: render_events
    )
    app.dependency_overrides[get_merchant_shadow_signal_repository] = (
        lambda: shadow_signals
    )
    with TestClient(app) as c:
        yield c, disagreements, render_events, shadow_signals
    app.dependency_overrides.clear()
    reset_dependency_caches()


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------


def test_triage_route_200_on_empty_repos(
    triage_client: tuple[
        TestClient,
        InMemoryScoringDisagreementRepository,
        InMemoryDisclosureRenderEventRepository,
        InMemoryMerchantShadowSignalRepository,
    ],
) -> None:
    """Empty repos render 200 + the "no triage pending" banner + three
    zero-count tiles."""
    client, _d, _r, _s = triage_client
    resp = client.get("/ui/triage")
    assert resp.status_code == 200, resp.text
    body = resp.text

    # Operator-context banner copy.
    assert "Operator triage backlog." in body
    assert "Three independent queues" in body

    # Empty-backlog branch.
    assert "No triage pending." in body

    # All three tiles render (zero counts visible — operator sees a
    # consistent layout regardless of which surface is loudest).
    assert "Scoring disagreements" in body
    assert "Disclosure render events" in body
    assert "Shadow signals" in body


# ---------------------------------------------------------------------------
# Populated state
# ---------------------------------------------------------------------------


def test_triage_route_surfaces_populated_counts(
    triage_client: tuple[
        TestClient,
        InMemoryScoringDisagreementRepository,
        InMemoryDisclosureRenderEventRepository,
        InMemoryMerchantShadowSignalRepository,
    ],
) -> None:
    """Populated repos surface non-zero tile counts in the rendered body."""
    client, disagreements, render_events, shadow_signals = triage_client

    # Two regression-sentinel disagreement rows.
    for _ in range(2):
        disagreements.record(
            merchant_id=uuid4(),
            deal_id=None,
            category=CATEGORY_OLD_BETTER,
            legacy_fraud_score=70,
            legacy_tier="D",
            legacy_recommendation="decline",
            legacy_hard_declines=None,
            track_a_verdict="pass",
            track_b_band="material",
            track_c_panel=None,
            evidence={"diff": f"item-{uuid4()}"},
        )

    # One needs_review + one apr_failed render event.
    render_events.record(
        deal_id=uuid4(),
        merchant_id=uuid4(),
        state="CA",
        template_path="compliance/templates/ca.html.j2",
        status=RENDER_EVENT_STATUS_NEEDS_REVIEW,
        status_reason="needs review row",
        details={"term_days": 180},
        recipient_email=None,
        rendered_by="api",
    )
    render_events.record(
        deal_id=uuid4(),
        merchant_id=uuid4(),
        state="NY",
        template_path="compliance/templates/ny.html.j2",
        status=RENDER_EVENT_STATUS_APR_FAILED,
        status_reason="brentq failed",
        details=None,
        recipient_email=None,
        rendered_by="api",
    )

    # Three shadow signals across two codes.
    for _ in range(2):
        shadow_signals.record(
            merchant_id=uuid4(),
            signal_code="duplicate_pdf_upload",
            signal_severity=0,
            detail=None,
            source_document_id=uuid4(),
            source_ids=[uuid4()],
            metadata=None,
            detected_by="worker",
        )
    shadow_signals.record(
        merchant_id=uuid4(),
        signal_code="related_account_suspected",
        signal_severity=0,
        detail=None,
        source_document_id=uuid4(),
        source_ids=[uuid4()],
        metadata=None,
        detected_by="worker",
    )

    resp = client.get("/ui/triage")
    assert resp.status_code == 200, resp.text
    body = resp.text

    # The "no triage pending" branch must NOT render now.
    assert "No triage pending." not in body

    # Tile chips reflect the actionable counts. We assert distinctive
    # substrings rather than the bare integers because the page also
    # contains the window date strings which could match a digit.
    assert "REGRESSION" in body  # disagreement chip
    assert "NEEDS REVIEW" in body
    assert "APR FAILED" in body
    assert "duplicate_pdf_upload" in body
    assert "related_account_suspected" in body

    # Triage links surface for each tile.
    assert "/ui/disclosure-events" in body
    assert "/ui/shadow-signals" in body
    assert "scripts/triage_disagreement.py" in body


# ---------------------------------------------------------------------------
# Date-range query
# ---------------------------------------------------------------------------


def test_triage_route_narrows_shadow_signals_by_days(
    triage_client: tuple[
        TestClient,
        InMemoryScoringDisagreementRepository,
        InMemoryDisclosureRenderEventRepository,
        InMemoryMerchantShadowSignalRepository,
    ],
) -> None:
    """``?days=7`` excludes a 60-day-old shadow signal from the tally."""
    client, _d, _r, shadow_signals = triage_client

    # Fresh signal — inside any reasonable window.
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
    # Stale signal — 60 days ago.
    shadow_signals.record(
        merchant_id=uuid4(),
        signal_code="related_account_suspected",
        signal_severity=0,
        detail=None,
        source_document_id=None,
        source_ids=[],
        metadata=None,
        detected_by="worker",
        detected_at=datetime.now(UTC) - timedelta(days=60),
    )

    # Window covers only the fresh row.
    resp = client.get("/ui/triage?days=7")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "duplicate_pdf_upload" in body
    # The stale code's chip line should not surface in the tile breakdown
    # (it has zero entries inside the 7-day window).
    assert "related_account_suspected" not in body


def test_triage_route_accepts_max_window_without_500(
    triage_client: tuple[
        TestClient,
        InMemoryScoringDisagreementRepository,
        InMemoryDisclosureRenderEventRepository,
        InMemoryMerchantShadowSignalRepository,
    ],
) -> None:
    """``?days=365`` (the cap) returns 200. Above-cap is rejected by
    FastAPI's Query(le=365); the route never sees it."""
    client, _d, _r, _s = triage_client
    resp = client.get("/ui/triage?days=365")
    assert resp.status_code == 200, resp.text


def test_triage_route_rejects_above_cap_days(
    triage_client: tuple[
        TestClient,
        InMemoryScoringDisagreementRepository,
        InMemoryDisclosureRenderEventRepository,
        InMemoryMerchantShadowSignalRepository,
    ],
) -> None:
    """``?days=1000`` exceeds the validator's le=365 bound → 422."""
    client, _d, _r, _s = triage_client
    resp = client.get("/ui/triage?days=1000")
    assert resp.status_code == 422, resp.text
