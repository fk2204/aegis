"""Tests for ``GET /ui/disclosure-events`` (U21 — operator triage page).

Covers:

  * List route returns 200 + renders the operator-context banner.
  * Status filter narrows the visible rows.
  * Detail subroute returns the row (200) + the requested id.
  * Detail subroute returns 404 on an unknown id.
  * Empty-status window renders the empty-state copy.
  * Invalid status query param surfaces as 400.

The page is exercised through ``TestClient`` against an in-memory
``InMemoryDisclosureRenderEventRepository`` (U16) so no Supabase client
is instantiated. Mirrors the renewals / portfolio route-test pattern.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_disclosure_render_event_repository,
    reset_dependency_caches,
)
from aegis.compliance.render_events import (
    RENDER_EVENT_STATUS_APR_FAILED,
    RENDER_EVENT_STATUS_NEEDS_REVIEW,
    RENDER_EVENT_STATUS_OK,
    InMemoryDisclosureRenderEventRepository,
)


@pytest.fixture
def repo_and_client() -> Iterator[
    tuple[TestClient, InMemoryDisclosureRenderEventRepository]
]:
    """Build an app whose render-event repo is an injected in-memory list.

    The fixture yields the live repo so each test can pre-seed rows
    directly via ``repo.record(...)``. No Supabase client is touched.
    """
    reset_dependency_caches()
    repo = InMemoryDisclosureRenderEventRepository()
    app = create_app()
    app.dependency_overrides[get_disclosure_render_event_repository] = (
        lambda: repo
    )
    with TestClient(app) as c:
        yield c, repo
    reset_dependency_caches()


def _seed(
    repo: InMemoryDisclosureRenderEventRepository,
    *,
    status: str,
    state: str = "CA",
    status_reason: str | None = "brentq failed to converge",
    rendered_at: datetime | None = None,
    details: dict[str, object] | None = None,
) -> object:
    """Drop one row into the in-memory repo with sensible defaults."""
    return repo.record(
        deal_id=uuid4(),
        merchant_id=uuid4(),
        state=state,
        template_path="compliance/templates/ca_sb1235.html.j2",
        status=status,
        status_reason=status_reason,
        details=details or {"term_days": 180, "factor": "1.35"},
        recipient_email=None,
        rendered_by="api",
        rendered_at=rendered_at or datetime.now(UTC),
        metadata={"render_mode": "preview"},
    )


# ---------------------------------------------------------------------------
# List route
# ---------------------------------------------------------------------------


def test_disclosure_events_route_200_and_banner_on_empty_repo(
    repo_and_client: tuple[TestClient, InMemoryDisclosureRenderEventRepository],
) -> None:
    """Empty repo renders 200, the banner copy, and the empty-state."""
    client, _repo = repo_and_client
    resp = client.get("/ui/disclosure-events")
    assert resp.status_code == 200, resp.text
    body = resp.text
    # Banner — operator-context wording from the template. The
    # template wraps long copy across newlines, so assert distinctive
    # phrases that survive the wrap rather than full sentences.
    assert "Internal pre-flight log." in body
    assert "Render-event records are AEGIS internal pre-flight." in body
    assert "regulator-facing issuance" in body
    # Empty-state copy.
    assert "No render events matching the current filter" in body


def test_disclosure_events_route_filters_by_status_param(
    repo_and_client: tuple[TestClient, InMemoryDisclosureRenderEventRepository],
) -> None:
    """``?status=apr_compute_failed`` returns only that bucket."""
    client, repo = repo_and_client
    needs_review_row = _seed(
        repo,
        status=RENDER_EVENT_STATUS_NEEDS_REVIEW,
        status_reason="needs review row",
    )
    apr_row = _seed(
        repo,
        status=RENDER_EVENT_STATUS_APR_FAILED,
        status_reason="apr_failed row",
    )
    _ok_row = _seed(
        repo,
        status=RENDER_EVENT_STATUS_OK,
        status_reason=None,
    )

    # apr_compute_failed → only the apr row appears in the body.
    resp = client.get(
        "/ui/disclosure-events?status=apr_compute_failed"
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert str(apr_row.id) in body  # type: ignore[attr-defined]
    assert str(needs_review_row.id) not in body  # type: ignore[attr-defined]
    assert "apr_failed row" in body
    assert "needs review row" not in body


def test_disclosure_events_route_default_filter_is_needs_review(
    repo_and_client: tuple[TestClient, InMemoryDisclosureRenderEventRepository],
) -> None:
    """No ``?status=`` → defaults to ``needs_review``; the ``ok`` row
    seeded into the repo is hidden in the default view."""
    client, repo = repo_and_client
    needs_review_row = _seed(
        repo,
        status=RENDER_EVENT_STATUS_NEEDS_REVIEW,
        status_reason="visible needs_review",
    )
    ok_row = _seed(
        repo,
        status=RENDER_EVENT_STATUS_OK,
        status_reason=None,
    )

    resp = client.get("/ui/disclosure-events")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert str(needs_review_row.id) in body  # type: ignore[attr-defined]
    assert "visible needs_review" in body
    # The ``ok`` row must NOT appear in the default filter.
    assert str(ok_row.id) not in body  # type: ignore[attr-defined]


def test_disclosure_events_route_status_all_returns_every_bucket(
    repo_and_client: tuple[TestClient, InMemoryDisclosureRenderEventRepository],
) -> None:
    """``?status=`` (empty) returns every status."""
    client, repo = repo_and_client
    a = _seed(repo, status=RENDER_EVENT_STATUS_NEEDS_REVIEW)
    b = _seed(repo, status=RENDER_EVENT_STATUS_APR_FAILED)
    c = _seed(repo, status=RENDER_EVENT_STATUS_OK)

    resp = client.get("/ui/disclosure-events?status=")
    assert resp.status_code == 200, resp.text
    body = resp.text
    for row in (a, b, c):
        assert str(row.id) in body  # type: ignore[attr-defined]


def test_disclosure_events_route_rejects_unknown_status(
    repo_and_client: tuple[TestClient, InMemoryDisclosureRenderEventRepository],
) -> None:
    """An unknown ``?status=`` value surfaces as 400 (not silent empty)."""
    client, _repo = repo_and_client
    resp = client.get("/ui/disclosure-events?status=not-a-real-status")
    assert resp.status_code == 400, resp.text


def test_disclosure_events_route_rejects_malformed_date(
    repo_and_client: tuple[TestClient, InMemoryDisclosureRenderEventRepository],
) -> None:
    """Malformed date in the query string surfaces as 400, not 500."""
    client, _repo = repo_and_client
    resp = client.get("/ui/disclosure-events?from=not-a-date")
    assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# Detail subroute
# ---------------------------------------------------------------------------


def test_disclosure_event_detail_returns_the_row(
    repo_and_client: tuple[TestClient, InMemoryDisclosureRenderEventRepository],
) -> None:
    """Detail subroute returns 200 + every column the record carries."""
    client, repo = repo_and_client
    row = _seed(
        repo,
        status=RENDER_EVENT_STATUS_APR_FAILED,
        status_reason="brentq failed to converge",
        details={"term_days": 180, "factor": "1.35"},
    )

    resp = client.get(f"/ui/disclosure-events/{row.id}")  # type: ignore[attr-defined]
    assert resp.status_code == 200, resp.text
    body = resp.text
    # The id, the status, the status_reason, and the details JSON
    # should each render in the detail body.
    assert str(row.id) in body  # type: ignore[attr-defined]
    assert "apr_compute_failed" in body
    assert "brentq failed to converge" in body
    assert "term_days" in body
    assert "factor" in body


def test_disclosure_event_detail_returns_404_on_unknown_id(
    repo_and_client: tuple[TestClient, InMemoryDisclosureRenderEventRepository],
) -> None:
    """Detail subroute returns 404 on an unknown UUID."""
    client, _repo = repo_and_client
    resp = client.get(f"/ui/disclosure-events/{uuid4()}")
    assert resp.status_code == 404, resp.text
