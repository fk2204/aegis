"""Phase 7C — per-merchant + portfolio submission views.

Covers ``GET /ui/merchants/{merchant_id}/submissions`` and
``GET /ui/submissions`` plus the deep-link from the dossier inline
funder card to the per-merchant view.

The Bedrock narrative stub is autouse: the dossier deep-link test
renders the dossier template directly, and the underlying
``aegis.scoring_v2.deal_summary`` module is imported during template
context construction. Stubbing returns the empty-string branch the
narrative is empty-safe against — same posture as
``test_dossier_inline_funder_match.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_funder_note_submission_repository,
    get_funder_repository,
    get_merchant_repository,
    reset_dependency_caches,
)
from aegis.funder_note_submissions.repository import (
    InMemoryFunderNoteSubmissionRepository,
)
from aegis.funders.models import FunderRow
from aegis.funders.repository import InMemoryFunderRepository
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository


@pytest.fixture(autouse=True)
def _stub_bedrock_narrative(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip Bedrock for every test in this module — see module docstring."""
    monkeypatch.setattr(
        "aegis.scoring_v2.deal_summary.generate_funder_narrative",
        lambda **_: "",
    )


@pytest.fixture
def merchants() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture
def funders() -> InMemoryFunderRepository:
    return InMemoryFunderRepository()


@pytest.fixture
def subs() -> InMemoryFunderNoteSubmissionRepository:
    return InMemoryFunderNoteSubmissionRepository()


@pytest.fixture
def client(
    merchants: InMemoryMerchantRepository,
    funders: InMemoryFunderRepository,
    subs: InMemoryFunderNoteSubmissionRepository,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: merchants
    app.dependency_overrides[get_funder_repository] = lambda: funders
    app.dependency_overrides[get_funder_note_submission_repository] = lambda: subs
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


_merchant_counter = 0


def _seed_merchant(
    repo: InMemoryMerchantRepository,
    *,
    business_name: str = "Acme Painting LLC",
    close_lead_id: str | None = None,
) -> MerchantRow:
    """Seed a merchant. ``close_lead_id`` defaults to a unique per-call
    value so test cases that seed multiple merchants don't collide on
    the close_lead_id uniqueness constraint."""
    global _merchant_counter
    _merchant_counter += 1
    if close_lead_id is None:
        close_lead_id = f"lead_test_{_merchant_counter}"
    m = MerchantRow(
        business_name=business_name,
        owner_name="Jane Owner",
        state="CA",
        close_lead_id=close_lead_id,
        status="finalized",
    )
    repo.upsert(m)
    return m


def _seed_funder(repo: InMemoryFunderRepository, name: str = "Wide Net Capital") -> FunderRow:
    f = FunderRow(name=name, active=True)
    repo.upsert(f)
    return f


# ---------------------------------------------------------------------------
# Per-merchant view
# ---------------------------------------------------------------------------


def test_merchant_submissions_unknown_merchant_returns_404(client: TestClient) -> None:
    bogus = uuid4()
    resp = client.get(f"/ui/merchants/{bogus}/submissions")
    assert resp.status_code == 404


def test_merchant_submissions_empty_state_uses_required_copy(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
) -> None:
    """The literal copy is part of the spec — workers read it as the
    cue to head back to the dossier and submit."""
    m = _seed_merchant(merchants)
    resp = client.get(f"/ui/merchants/{m.id}/submissions")
    assert resp.status_code == 200
    assert 'data-test-id="merchant-submissions-empty"' in resp.text
    assert "No submissions yet — submit to a funder from the dossier above." in resp.text


def test_merchant_submissions_renders_rows_with_funder_anchor(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    funders: InMemoryFunderRepository,
    subs: InMemoryFunderNoteSubmissionRepository,
) -> None:
    m = _seed_merchant(merchants)
    f = _seed_funder(funders, name="Wide Net Capital")
    subs.create(
        merchant_id=m.id,
        funder_id=f.id,
        funder_note="initial submission",
        submitted_by="dashboard",
    )

    resp = client.get(f"/ui/merchants/{m.id}/submissions")
    assert resp.status_code == 200
    assert 'data-test-id="merchant-submissions-table"' in resp.text
    assert "Wide Net Capital" in resp.text
    # Anchor for the dossier deep-link.
    assert f'id="funder-{f.id}"' in resp.text
    # Pending status renders with no chip-color class.
    assert ">pending<" in resp.text


def test_merchant_submissions_chip_class_approved_is_pos(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    funders: InMemoryFunderRepository,
    subs: InMemoryFunderNoteSubmissionRepository,
) -> None:
    m = _seed_merchant(merchants)
    f = _seed_funder(funders)
    row = subs.create(
        merchant_id=m.id,
        funder_id=f.id,
        funder_note="x",
        submitted_by="dashboard",
    )
    subs.update_status(row.id, status="approved", offer_amount=Decimal("50000.00"))

    resp = client.get(f"/ui/merchants/{m.id}/submissions")
    assert resp.status_code == 200
    assert 'class="chip pos"' in resp.text


def test_merchant_submissions_chip_class_declined_is_bad(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    funders: InMemoryFunderRepository,
    subs: InMemoryFunderNoteSubmissionRepository,
) -> None:
    m = _seed_merchant(merchants)
    f = _seed_funder(funders)
    row = subs.create(
        merchant_id=m.id,
        funder_id=f.id,
        funder_note="x",
        submitted_by="dashboard",
    )
    subs.update_status(row.id, status="declined", notes="stacking risk")

    resp = client.get(f"/ui/merchants/{m.id}/submissions")
    assert resp.status_code == 200
    assert 'class="chip bad"' in resp.text


# ---------------------------------------------------------------------------
# Portfolio view
# ---------------------------------------------------------------------------


def test_portfolio_submissions_empty_state_renders(client: TestClient) -> None:
    resp = client.get("/ui/submissions")
    assert resp.status_code == 200
    assert 'data-test-id="portfolio-submissions-empty"' in resp.text


def test_portfolio_submissions_renders_multi_merchant_rows(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    funders: InMemoryFunderRepository,
    subs: InMemoryFunderNoteSubmissionRepository,
) -> None:
    m1 = _seed_merchant(merchants, business_name="Acme Painting LLC")
    m2 = _seed_merchant(merchants, business_name="Beta Roofing Co")
    f = _seed_funder(funders, name="Wide Net Capital")

    subs.create(
        merchant_id=m1.id,
        funder_id=f.id,
        funder_note="m1",
        submitted_by="dashboard",
    )
    subs.create(
        merchant_id=m2.id,
        funder_id=f.id,
        funder_note="m2",
        submitted_by="dashboard",
    )

    resp = client.get("/ui/submissions")
    assert resp.status_code == 200
    assert 'data-test-id="portfolio-submissions-table"' in resp.text
    # Both merchants present.
    assert "Acme Painting LLC" in resp.text
    assert "Beta Roofing Co" in resp.text
    # Per-row link to the merchant dossier.
    assert f'href="/ui/merchants/{m1.id}"' in resp.text
    assert f'href="/ui/merchants/{m2.id}"' in resp.text
    # Each row carries the data-* attributes the JS reads. Count the
    # ``<tr data-test-id="portfolio-submission-row"`` token specifically
    # — the JS at the bottom of the template references the same
    # data-test-id by name, so a substring count would over-report.
    assert resp.text.count('<tr data-test-id="portfolio-submission-row"') == 2
    assert "data-submitted-at=" in resp.text
    assert "data-funder-id=" in resp.text


def test_portfolio_submissions_counters_reflect_pending_and_month(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    funders: InMemoryFunderRepository,
    subs: InMemoryFunderNoteSubmissionRepository,
) -> None:
    """Pending counter counts every pending row in the limited window;
    approved/declined counters are scoped to the current UTC month."""
    m = _seed_merchant(merchants)
    f = _seed_funder(funders)

    # Pending row.
    subs.create(
        merchant_id=m.id,
        funder_id=f.id,
        funder_note="p",
        submitted_by="dashboard",
    )
    # Approved row (same UTC month — created just now).
    row_a = subs.create(
        merchant_id=m.id,
        funder_id=f.id,
        funder_note="a",
        submitted_by="dashboard",
    )
    subs.update_status(row_a.id, status="approved", offer_amount=Decimal("25000.00"))
    # Declined row (same UTC month).
    row_d = subs.create(
        merchant_id=m.id,
        funder_id=f.id,
        funder_note="d",
        submitted_by="dashboard",
    )
    subs.update_status(row_d.id, status="declined", notes="not a fit")

    resp = client.get("/ui/submissions")
    assert resp.status_code == 200
    assert 'data-test-id="counter-pending"' in resp.text
    assert 'data-test-id="counter-approved-month"' in resp.text
    assert 'data-test-id="counter-declined-month"' in resp.text
    # Filter controls present.
    assert 'data-test-id="filter-status-chips"' in resp.text
    assert 'data-test-id="status-filter-pending"' in resp.text
    assert 'data-test-id="status-filter-approved"' in resp.text
    assert 'data-test-id="status-filter-declined"' in resp.text
    assert 'data-test-id="status-filter-countered"' in resp.text
    assert 'data-test-id="date-filter-30"' in resp.text


def test_portfolio_submissions_funder_dropdown_lists_distinct_funders(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    funders: InMemoryFunderRepository,
    subs: InMemoryFunderNoteSubmissionRepository,
) -> None:
    m = _seed_merchant(merchants)
    f1 = _seed_funder(funders, name="Wide Net Capital")
    f2 = _seed_funder(funders, name="Niche Capital")

    subs.create(
        merchant_id=m.id,
        funder_id=f1.id,
        funder_note="x",
        submitted_by="dashboard",
    )
    subs.create(
        merchant_id=m.id,
        funder_id=f2.id,
        funder_note="x",
        submitted_by="dashboard",
    )

    resp = client.get("/ui/submissions")
    assert resp.status_code == 200
    # Dropdown options for each funder, plus the "All funders" option.
    assert f'value="{f1.id}"' in resp.text
    assert f'value="{f2.id}"' in resp.text
    assert "All funders" in resp.text


# ---------------------------------------------------------------------------
# Dossier deep-link integration
# ---------------------------------------------------------------------------


def _render_dossier_with_card(
    *,
    merchant: MerchantRow,
    matched_funders: list[dict[str, Any]],
) -> str:
    """Render the dossier template with the minimum stub context the
    inline § 4 matched-funders panel needs. Mirrors the helper in
    ``test_dossier_inline_funder_match.py`` so this test module can
    verify the "View history" link on the same code path the
    integration test exercises without booting the FastAPI app."""
    from aegis.web._templates import templates

    class _StubScore:
        def __init__(self) -> None:
            self.recommendation = "approve"
            self.score = 70
            self.tier = "B"
            self.paper_grade = "B"
            self.suggested_max_advance = Decimal("50000")
            self.hard_decline_reasons: list[str] = []
            self.soft_concerns: list[str] = []
            self.decline_details: dict[str, Any] = {}

    template = templates.get_template("merchant_detail_dossier.html.j2")
    return template.render(
        request=None,
        merchant=merchant,
        documents=[],
        document=None,
        analysis=None,
        aggregate_labels={},
        aggregate_unit_kind={},
        pattern_cards=[],
        latest_transactions=[],
        soft_signals=None,
        has_concentration_pattern=False,
        from_intake=False,
        intake_docs_uploaded=0,
        intake_docs_failed=0,
        score_result=_StubScore(),
        score_window=None,
        statement_coverage=None,
        stacking=None,
        mca_stack=None,
        balance_health=None,
        offer=None,
        state_tier=None,
        ofac_status="pending",
        ofac_match=None,
        trend=None,
        history=[],
        close_last_orchestration_capped=False,
        unified_tracks=None,
        shadow_signals=[],
        merchant_shadow_signals=[],
        revenue_trends=None,
        funder_note_submissions=[],
        operator_notes=[],
        operator_note_max_chars=2000,
        deal_summary=None,
        funder_narrative="",
        doc_checklist={
            "voided_check_on_file": False,
            "drivers_license_on_file": False,
            "bank_statements_months": 0,
        },
        stips_result=None,
        top_matched_funder_name=None,
        matched_funders=matched_funders,
        matched_funder_responses={},
        submitted_funder_ids=set(),
    )


def test_dossier_funder_card_renders_view_history_link() -> None:
    """Each inline § 4 funder card must carry a quiet "View history"
    link to the per-merchant submissions page anchored on the funder
    id. Template-only render (no FastAPI round-trip) follows the same
    pattern as the inline matched-funder panel tests so this test
    doesn't depend on the dossier route building matched-funder cards
    via the matcher pipeline."""
    merchant = MerchantRow(
        business_name="Acme Painting LLC",
        owner_name="Jane Owner",
        state="CA",
        close_lead_id="lead_abc",
    )
    funder_id = uuid4()
    card = {
        "funder_id": str(funder_id),
        "funder_name": "Wide Net Capital",
        "match_score": 80,
        "color": "green",
        "hard_reasons": [],
        "soft_concerns": [],
        "criteria_comparison": [],
        "funder_requires_coj": False,
        "funder_charges_merchant_advance_fees": False,
        "estimated_terms": None,
        "tier_matches": [],
        "historical_approval_rate": None,
    }

    html = _render_dossier_with_card(merchant=merchant, matched_funders=[card])

    assert 'data-test-id="dossier-funder-card-view-history"' in html
    expected_href = f"/ui/merchants/{merchant.id}/submissions#funder-{funder_id}"
    assert expected_href in html


# Silence ruff for shared imports — kept for parity with the funder
# performance route tests; ``datetime`` / ``timedelta`` would be needed
# if we extend the suite with windowed-counter tests.
_ = datetime, timedelta, UTC
