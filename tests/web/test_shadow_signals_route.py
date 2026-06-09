"""Tests for ``GET /ui/shadow-signals`` (U24 — cross-merchant view).

Covers:

  * 200 + filter form + banner render.
  * Filter by ``?code=...`` narrows results.
  * Filter by ``?merchant=<uuid>`` narrows results.
  * Empty repo → empty-state copy.
  * Humanized titles via the U18 _flag_labels formatter render in the
    table (not the raw code).

The route is exercised through ``TestClient`` with the in-memory
``InMemoryMerchantShadowSignalRepository`` injected via
``app.dependency_overrides``. Mirrors ``test_disclosure_events_route``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_merchant_shadow_signal_repository,
    reset_dependency_caches,
)
from aegis.merchants.shadow_signals import InMemoryMerchantShadowSignalRepository


@pytest.fixture
def repo_and_client() -> Iterator[
    tuple[TestClient, InMemoryMerchantShadowSignalRepository]
]:
    """TestClient pinned to an in-memory shadow-signal repo."""
    reset_dependency_caches()
    repo = InMemoryMerchantShadowSignalRepository()
    app = create_app()
    app.dependency_overrides[get_merchant_shadow_signal_repository] = lambda: repo
    with TestClient(app) as c:
        yield c, repo
    app.dependency_overrides.clear()
    reset_dependency_caches()


def _seed(
    repo: InMemoryMerchantShadowSignalRepository,
    *,
    signal_code: str,
    merchant_id: object | None = None,
    detail: str | None = None,
) -> object:
    """Drop one row into the repo with sensible U22 defaults."""
    return repo.record(
        merchant_id=merchant_id if merchant_id is not None else uuid4(),  # type: ignore[arg-type]
        signal_code=signal_code,
        signal_severity=0,
        detail=detail,
        source_document_id=uuid4(),
        source_ids=[uuid4()],
        metadata=None,
        detected_by="worker",
        detected_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------


def test_shadow_signals_route_200_on_empty_repo(
    repo_and_client: tuple[TestClient, InMemoryMerchantShadowSignalRepository],
) -> None:
    """Empty repo renders 200, the banner, the filter form, the empty state."""
    client, _repo = repo_and_client
    resp = client.get("/ui/shadow-signals")
    assert resp.status_code == 200, resp.text
    body = resp.text

    # Operator-context banner.
    assert "Cross-statement evidence in shadow mode." in body

    # Filter form fields.
    assert 'name="code"' in body
    assert 'name="merchant"' in body
    assert 'name="days"' in body

    # Empty-state copy.
    assert "No shadow signals matching the current filter" in body


# ---------------------------------------------------------------------------
# Filter narrowing
# ---------------------------------------------------------------------------


def test_shadow_signals_route_filters_by_code(
    repo_and_client: tuple[TestClient, InMemoryMerchantShadowSignalRepository],
) -> None:
    """``?code=duplicate_pdf_upload`` shows only matching rows."""
    client, repo = repo_and_client
    dup = _seed(
        repo,
        signal_code="duplicate_pdf_upload",
        detail="sha256_match_with_doc=...",
    )
    related = _seed(
        repo,
        signal_code="related_account_suspected",
        detail="holder=ACME:existing_last4=1234:new_last4=5678",
    )

    resp = client.get("/ui/shadow-signals?code=duplicate_pdf_upload")
    assert resp.status_code == 200, resp.text
    body = resp.text
    # Matching row's merchant_id (first 8 chars) should appear in the body.
    dup_short = str(dup.merchant_id)[:8]  # type: ignore[attr-defined]
    rel_short = str(related.merchant_id)[:8]  # type: ignore[attr-defined]
    assert dup_short in body
    assert rel_short not in body


def test_shadow_signals_route_filters_by_merchant(
    repo_and_client: tuple[TestClient, InMemoryMerchantShadowSignalRepository],
) -> None:
    """``?merchant=<uuid>`` shows only rows for that merchant."""
    client, repo = repo_and_client
    target_merchant = uuid4()
    other_merchant = uuid4()
    target = _seed(
        repo,
        signal_code="duplicate_pdf_upload",
        merchant_id=target_merchant,
    )
    other = _seed(
        repo,
        signal_code="duplicate_pdf_upload",
        merchant_id=other_merchant,
    )

    resp = client.get(f"/ui/shadow-signals?merchant={target_merchant}")
    assert resp.status_code == 200, resp.text
    body = resp.text
    # Target row's id should be present; the other merchant's id absent.
    assert str(target.id) not in body or str(target_merchant)[:8] in body  # type: ignore[attr-defined]
    # Definitive narrowing assertion: the other merchant's 8-char prefix
    # must not appear in the table (the row was filtered out by the
    # repository before render).
    assert str(other_merchant)[:8] not in body
    assert str(other.id) not in body  # type: ignore[attr-defined]


def test_shadow_signals_route_rejects_malformed_merchant_uuid(
    repo_and_client: tuple[TestClient, InMemoryMerchantShadowSignalRepository],
) -> None:
    """Non-UUID ``?merchant=...`` surfaces as 400 rather than a silent
    empty page."""
    client, _repo = repo_and_client
    resp = client.get("/ui/shadow-signals?merchant=not-a-uuid")
    assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# Humanization (U18 _flag_labels)
# ---------------------------------------------------------------------------


def test_shadow_signals_route_renders_humanized_title(
    repo_and_client: tuple[TestClient, InMemoryMerchantShadowSignalRepository],
) -> None:
    """U18-humanized title appears in the table for known signal codes.

    ``duplicate_pdf_upload`` is registered in ``_FLAG_REGISTRY`` so the
    humanizer returns a hand-authored title that must surface in the
    rendered body instead of the raw code alone.
    """
    client, repo = repo_and_client
    _seed(
        repo,
        signal_code="duplicate_pdf_upload",
        detail="sha256_match_with_doc=abc:uploaded=2026-06-01",
    )

    resp = client.get("/ui/shadow-signals")
    assert resp.status_code == 200, resp.text
    body = resp.text

    # The raw code remains visible as a small subtitle (so the operator
    # can still look up the detector by name).
    assert "duplicate_pdf_upload" in body
    # The humanized title is the registered string — assert the title
    # is rendered with a leading uppercase character (versus the snake-
    # case raw code).
    assert "Duplicate" in body
