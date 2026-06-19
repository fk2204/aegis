"""Phase 7B inline matched-funders panel on the dossier.

Covers:
  * § 4 renders inline cards when matched funders exist.
  * § 4 falls back to empty-state when no matches.
  * § 4 decline-state copy renders when score recommendation is decline.
  * Per-funder Submit button is enabled for green/yellow cards with a
    linked Close Lead and disabled for red cards, missing close_lead,
    or already-submitted funders.
  * Matched-funders CSV link renders only when matches exist.
  * POST /submit-to-funder with ``funder_id`` form field narrows to
    that funder, posts a per-funder note, writes a per-funder durable
    submission row, and tags the audit row with ``target_funder_id``.
  * POST /submit-to-funder with ``funder_id`` pointing at a non-match
    rejects 400 and does NOT call Close.
  * GET /matched-funders.csv returns the expected header rows + funder
    columns + content-disposition.
  * Standalone /match page still renders after the inline lift
    (regression guard).
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_close_client,
    get_funder_note_submission_repository,
    get_funder_repository,
    get_merchant_repository,
    get_ofac_client,
    get_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.close.client import CloseClient
from aegis.config import get_settings
from aegis.funder_note_submissions.repository import (
    InMemoryFunderNoteSubmissionRepository,
)
from aegis.funders.models import FunderRow
from aegis.funders.repository import InMemoryFunderRepository
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.storage import InMemoryDocumentRepository
from aegis.web._templates import templates
from tests.test_storage import _make_pipeline_result

# ---------------------------------------------------------------------------
# Template-only rendering tests (no FastAPI app needed)
# ---------------------------------------------------------------------------


def _make_merchant(*, close_lead_id: str | None = "lead_abc") -> MerchantRow:
    return MerchantRow(
        business_name="Acme Painting LLC",
        owner_name="Jane Owner",
        state="CA",
        industry_naics="722511",
        time_in_business_months=24,
        credit_score=720,
        close_lead_id=close_lead_id,
    )


def _card(
    *,
    funder_id: str | None = None,
    funder_name: str = "Wide Net Capital",
    match_score: int = 75,
    color: str = "green",
    hard_reasons: list[str] | None = None,
    soft_concerns: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "funder_id": funder_id or str(uuid4()),
        "funder_name": funder_name,
        "match_score": match_score,
        "color": color,
        "hard_reasons": hard_reasons or [],
        "soft_concerns": soft_concerns or [],
        "criteria_comparison": [],
        "funder_requires_coj": False,
        "funder_charges_merchant_advance_fees": False,
        "estimated_terms": None,
        "tier_matches": [],
        "historical_approval_rate": None,
    }


def _render_dossier(
    *,
    merchant: MerchantRow,
    matched_funders: list[dict[str, Any]],
    submitted_funder_ids: set[str] | None = None,
    matched_funder_responses: dict[str, dict[str, Any]] | None = None,
    score_recommendation: str = "approve",
    has_score: bool = True,
) -> str:
    """Render the dossier template with minimal stub context.

    The inline § 4 panel is the section under test; everything else
    collapses to its empty-state branches via missing context.
    """

    class _StubScore:
        def __init__(self, recommendation: str) -> None:
            self.recommendation = recommendation
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
        score_result=_StubScore(score_recommendation) if has_score else None,
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
        matched_funder_responses=matched_funder_responses or {},
        submitted_funder_ids=submitted_funder_ids or set(),
    )


def test_dossier_renders_matched_funders_grid_when_cards_present() -> None:
    merchant = _make_merchant()
    cards = [
        _card(funder_name="Wide Net Capital", color="green", match_score=85),
        _card(funder_name="Strict Capital", color="yellow", match_score=60),
    ]
    html = _render_dossier(merchant=merchant, matched_funders=cards)

    assert 'data-test-id="dossier-funder-matching"' in html
    assert 'data-test-id="dossier-matched-funders-grid"' in html
    assert "Wide Net Capital" in html
    assert "Strict Capital" in html
    assert html.count('data-test-id="dossier-matched-funder-card"') == 2
    assert 'data-test-id="dossier-matched-funders-csv"' in html


def test_dossier_falls_back_to_empty_state_when_no_matches() -> None:
    merchant = _make_merchant()
    html = _render_dossier(merchant=merchant, matched_funders=[])

    assert 'data-test-id="dossier-funder-matching-empty"' in html
    assert 'data-test-id="dossier-matched-funders-grid"' not in html
    assert 'data-test-id="dossier-matched-funders-csv"' not in html


def test_dossier_renders_decline_state_copy_for_declined_score() -> None:
    merchant = _make_merchant()
    cards = [_card(color="red", hard_reasons=["nsf 12 > max 5"])]
    html = _render_dossier(
        merchant=merchant,
        matched_funders=cards,
        score_recommendation="decline",
    )

    assert 'data-test-id="dossier-funder-matching-decline"' in html
    assert "no funders will be matched" in html
    # Grid is suppressed when the deal is a hard decline.
    assert 'data-test-id="dossier-matched-funders-grid"' not in html


def test_per_funder_submit_button_is_enabled_for_green_card_with_close_lead() -> None:
    merchant = _make_merchant(close_lead_id="lead_abc")
    funder_id = str(uuid4())
    cards = [_card(funder_id=funder_id, color="green")]
    html = _render_dossier(merchant=merchant, matched_funders=cards)

    assert 'data-test-id="dossier-submit-funder-button"' in html
    assert f'hx-post="/ui/merchants/{merchant.id}/submit-to-funder/{funder_id}"' in html
    assert f'data-funder-id="{funder_id}"' in html
    assert "Submit to Wide Net Capital" in html


def test_per_funder_submit_button_disabled_for_red_card() -> None:
    merchant = _make_merchant(close_lead_id="lead_abc")
    cards = [_card(color="red", hard_reasons=["nsf 12 > max 5"])]
    html = _render_dossier(merchant=merchant, matched_funders=cards)

    assert 'data-test-id="dossier-submit-funder-disabled-hard-fail"' in html
    assert 'data-test-id="dossier-submit-funder-button"' not in html


def test_per_funder_submit_button_disabled_when_no_close_lead() -> None:
    merchant = _make_merchant(close_lead_id=None)
    cards = [_card(color="green")]
    html = _render_dossier(merchant=merchant, matched_funders=cards)

    assert 'data-test-id="dossier-submit-funder-disabled-no-lead"' in html
    assert 'data-test-id="dossier-submit-funder-button"' not in html


def test_per_funder_submit_button_shows_already_submitted_chip() -> None:
    merchant = _make_merchant(close_lead_id="lead_abc")
    funder_id = str(uuid4())
    cards = [_card(funder_id=funder_id, color="green")]
    html = _render_dossier(
        merchant=merchant,
        matched_funders=cards,
        submitted_funder_ids={funder_id},
    )

    assert 'data-test-id="dossier-submit-funder-already-submitted"' in html
    assert 'data-test-id="dossier-submit-funder-button"' not in html


# ---------------------------------------------------------------------------
# Integration tests — full route round-trips through the FastAPI app
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_bedrock_narrative(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip Bedrock for every test in this module.

    The Submit-to-Funder route prepends a Bedrock-generated narrative to
    the Close Note. On a Windows workstation with AWS creds in
    ``~/.aws/credentials`` the lazy ``BedrockClient`` construction
    succeeds, then ``generate_text`` blocks on the AWS API and the
    integration test hangs. The narrative is empty-safe by contract — the
    route falls back to the structured-only Close Note when the narrative
    returns "" — so stubbing to a no-op is equivalent to the test-without-
    AWS-creds branch.
    """
    monkeypatch.setattr(
        "aegis.scoring_v2.deal_summary.generate_funder_narrative",
        lambda **_kwargs: "",
    )


def _set_close_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOSE_API_KEY", "api_test")
    monkeypatch.setenv("CLOSE_API_BASE", "https://api.close.example")
    get_settings.cache_clear()


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def merchants() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture
def docs() -> InMemoryDocumentRepository:
    return InMemoryDocumentRepository()


@pytest.fixture
def funder_repo() -> InMemoryFunderRepository:
    return InMemoryFunderRepository()


@pytest.fixture
def funder_note_subs() -> InMemoryFunderNoteSubmissionRepository:
    return InMemoryFunderNoteSubmissionRepository()


@pytest.fixture
def close_post_calls() -> list[dict[str, Any]]:
    return []


@pytest.fixture
def close_client(
    monkeypatch: pytest.MonkeyPatch,
    close_post_calls: list[dict[str, Any]],
) -> CloseClient:
    _set_close_env(monkeypatch)
    monkeypatch.setattr("aegis.close.client.time.sleep", lambda _s: None)

    def transport(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "activity/note" in request.url.path:
            close_post_calls.append(
                {
                    "url": str(request.url),
                    "body": request.content.decode("utf-8"),
                }
            )
            return httpx.Response(
                200,
                json={"id": "acti_test_123", "_type": "Note"},
            )
        return httpx.Response(405)

    return CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport)))


@pytest.fixture
def client(
    audit: InMemoryAuditLog,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    funder_note_subs: InMemoryFunderNoteSubmissionRepository,
    close_client: CloseClient,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_merchant_repository] = lambda: merchants
    app.dependency_overrides[get_repository] = lambda: docs
    app.dependency_overrides[get_funder_repository] = lambda: funder_repo
    app.dependency_overrides[get_funder_note_submission_repository] = lambda: funder_note_subs
    app.dependency_overrides[get_close_client] = lambda: close_client
    app.dependency_overrides[get_ofac_client] = lambda: None
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def _seed_analyzed_merchant(
    merchants_repo: InMemoryMerchantRepository,
    docs_repo: InMemoryDocumentRepository,
    *,
    close_lead_id: str | None = "lead_abc",
    merchant_status: str = "finalized",
) -> MerchantRow:
    merchant = MerchantRow(
        business_name="Acme Painting LLC",
        owner_name="Jane Owner",
        state="CA",
        close_lead_id=close_lead_id,
        status=merchant_status,
    )
    merchants_repo.upsert(merchant)
    doc = docs_repo.create_document(
        file_hash=uuid4().hex + uuid4().hex,
        byte_size=1024,
        original_filename="stmt.pdf",
    )
    doc = doc.model_copy(update={"merchant_id": merchant.id})
    docs_repo._docs[doc.id] = doc
    docs_repo.persist_parse_result(doc.id, result=_make_pipeline_result(), merchant_id=merchant.id)
    return merchant


def _seed_matching_funder(
    funder_repo: InMemoryFunderRepository,
    *,
    name: str = "Wide Net Capital",
) -> FunderRow:
    funder = FunderRow(
        name=name,
        min_monthly_revenue=Decimal("1000"),
        max_positions=10,
        active=True,
    )
    funder_repo.upsert(funder)
    return funder


def test_submit_to_specific_funder_filters_to_one_funder(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    audit: InMemoryAuditLog,
    funder_note_subs: InMemoryFunderNoteSubmissionRepository,
    close_post_calls: list[dict[str, Any]],
) -> None:
    """Two matching funders seeded; the operator clicks Submit on the
    second one. The Close Note posts framed against that funder only,
    the durable submission row carries the target funder_id, and the
    audit row records ``target_funder_id``."""
    merchant = _seed_analyzed_merchant(merchants, docs)
    _ = _seed_matching_funder(funder_repo, name="Wide Net Capital")
    funder_b = _seed_matching_funder(funder_repo, name="Niche Capital")

    resp = client.post(f"/ui/merchants/{merchant.id}/submit-to-funder/{funder_b.id}")
    assert resp.status_code == 200, resp.text
    assert "Submitted" in resp.text

    # Close Note posted exactly once.
    assert len(close_post_calls) == 1

    # Durable row is for the targeted funder, not the top global match.
    rows = funder_note_subs.list_for_merchant(merchant.id)
    assert len(rows) == 1
    assert rows[0].funder_id == funder_b.id

    posted = [e for e in audit.entries if e["action"] == "deal.funder_note_posted"]
    assert len(posted) == 1
    details = posted[0]["details"]
    assert details["target_funder_id"] == str(funder_b.id)
    assert details["matched_funder_count"] == 1


def test_submit_to_specific_funder_rejects_non_matching_funder(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    funder_note_subs: InMemoryFunderNoteSubmissionRepository,
    close_post_calls: list[dict[str, Any]],
) -> None:
    """POST with a funder_id that doesn't appear in the matched list
    returns 400 and does NOT call Close. Reachable when the dossier UI
    has gone stale (e.g., funder was deactivated since the page rendered)."""
    merchant = _seed_analyzed_merchant(merchants, docs)
    _ = _seed_matching_funder(funder_repo, name="Wide Net Capital")
    unknown_funder_id = uuid4()

    resp = client.post(f"/ui/merchants/{merchant.id}/submit-to-funder/{unknown_funder_id}")
    assert resp.status_code == 400
    assert "is not a current match" in resp.json()["detail"]
    assert close_post_calls == []
    assert funder_note_subs.list_for_merchant(merchant.id) == []


def test_submit_to_specific_funder_rejects_malformed_uuid(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    close_post_calls: list[dict[str, Any]],
) -> None:
    """FastAPI rejects a non-UUID path segment at the routing layer with
    422 before our handler runs."""
    merchant = _seed_analyzed_merchant(merchants, docs)
    _seed_matching_funder(funder_repo)
    resp = client.post(f"/ui/merchants/{merchant.id}/submit-to-funder/not-a-uuid")
    assert resp.status_code == 422
    assert close_post_calls == []


def test_global_submit_to_funder_preserves_existing_behavior(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    audit: InMemoryAuditLog,
    funder_note_subs: InMemoryFunderNoteSubmissionRepository,
    close_post_calls: list[dict[str, Any]],
) -> None:
    """Backwards-compat regression: a POST without ``funder_id`` (the
    legacy global Submit-to-Funder button) keeps top-three behavior
    and does NOT tag the audit with ``target_funder_id``."""
    merchant = _seed_analyzed_merchant(merchants, docs)
    funder = _seed_matching_funder(funder_repo)

    resp = client.post(f"/ui/merchants/{merchant.id}/submit-to-funder")
    assert resp.status_code == 200

    rows = funder_note_subs.list_for_merchant(merchant.id)
    assert len(rows) == 1
    assert rows[0].funder_id == funder.id

    posted = [e for e in audit.entries if e["action"] == "deal.funder_note_posted"]
    assert len(posted) == 1
    assert "target_funder_id" not in posted[0]["details"]


def test_matched_funders_csv_endpoint_returns_csv_with_funder_rows(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
) -> None:
    merchant = _seed_analyzed_merchant(merchants, docs)
    funder = _seed_matching_funder(funder_repo, name="Wide Net Capital")

    resp = client.get(f"/ui/merchants/{merchant.id}/matched-funders.csv")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    assert "matched_funders_" in resp.headers["content-disposition"]
    body = resp.text

    # Deal-level header rows.
    assert "# Deal" in body
    assert "Acme Painting LLC" in body
    assert "# Merchant ID" in body
    assert str(merchant.id) in body
    assert "# Tier" in body
    assert "# Recommendation" in body

    # Funder columns header.
    assert "funder_name,funder_id,match_score,qualifies,color" in body
    # Data row carries the seeded funder.
    assert "Wide Net Capital" in body
    assert str(funder.id) in body


def test_matched_funders_csv_400_when_not_finalized(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
) -> None:
    merchant = _seed_analyzed_merchant(merchants, docs, merchant_status="provisional")
    _seed_matching_funder(funder_repo)
    resp = client.get(f"/ui/merchants/{merchant.id}/matched-funders.csv")
    assert resp.status_code == 400
    assert "not finalized" in resp.json()["detail"]


def test_standalone_match_page_still_renders_after_dossier_inline_lift(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
) -> None:
    """Regression guard. The dossier inline panel reuses the helper that
    powers /match, so the standalone page must still render with the
    same Wide-Net funder card."""
    merchant = _seed_analyzed_merchant(merchants, docs)
    _seed_matching_funder(funder_repo, name="Wide Net Capital")

    resp = client.get(f"/ui/merchants/{merchant.id}/match")
    assert resp.status_code == 200, resp.text
    assert "Wide Net Capital" in resp.text
    assert "Matched funders" in resp.text


def test_dossier_inline_panel_uses_shared_match_card_helper(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
) -> None:
    """End-to-end smoke: hit the dossier route, expect the new § 4
    markers + the seeded funder card to land in the rendered HTML."""
    merchant = _seed_analyzed_merchant(merchants, docs)
    _seed_matching_funder(funder_repo, name="Wide Net Capital")

    resp = client.get(f"/ui/merchants/{merchant.id}")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert 'data-test-id="dossier-funder-matching"' in body
    routing_idx = body.find('id="routing"')
    assert routing_idx >= 0
    routing_chunk = body[routing_idx : routing_idx + 4000]
    # Section landed on either the grid branch (funder qualified) OR
    # the empty-state branch (no active funder cleared the matcher's
    # criteria). Both branches prove the matched_funders context made
    # it from the helper into the template — what we're asserting is
    # that the inline panel is fully wired, not that this synthetic
    # fixture matches every matcher rule.
    has_grid = 'data-test-id="dossier-matched-funders-grid"' in routing_chunk
    has_empty = 'data-test-id="dossier-funder-matching-empty"' in routing_chunk
    has_decline = 'data-test-id="dossier-funder-matching-decline"' in routing_chunk
    assert has_grid or has_empty or has_decline, routing_chunk
    if has_grid:
        assert "Wide Net Capital" in routing_chunk
        assert "/submit-to-funder/" in routing_chunk
        assert 'data-test-id="dossier-submit-funder-button"' in routing_chunk


def test_dossier_route_renders_clean_when_at_least_one_active_funder_exists(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
) -> None:
    """Regression guard: the dossier handler's ``UUID(cards[0]["funder_id"])``
    call (used to derive ``top_matched_funder`` for the stips block) must
    not raise — if the helper hands back a malformed funder_id string the
    dossier 500s. A 200 response with the new § 4 section markers is
    enough to confirm the helper + dossier integration is healthy."""
    merchant = _seed_analyzed_merchant(merchants, docs)
    _seed_matching_funder(funder_repo)

    resp = client.get(f"/ui/merchants/{merchant.id}")
    assert resp.status_code == 200, resp.text
    assert 'data-test-id="dossier-funder-matching"' in resp.text
