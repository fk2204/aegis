"""Tests for the shared ``_topstrip.html.j2`` nav partial.

The partial is included by ``base.html.j2`` and
``merchant_detail_dossier.html.j2``. Pages that extend ``base`` get the
strip "for free" — the regression we're protecting against here is the
opposite: a nav link gets dropped, or a new page's ``active`` token
breaks the highlight contract.

Updated 2026-06-29 for the 4-main + settings-gear consolidation. Main
nav is now Today / Deals / Funders / Upload. Pipeline / Bank coverage /
Calibration / Overrides / Probe review live in the ⚙ settings dropdown
on the right.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import reset_dependency_caches


@pytest.fixture
def client() -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_dependency_caches()


def test_topstrip_main_nav_four_items(client: TestClient) -> None:
    """The simplified main nav exposes Today / Deals / Funders / Upload.

    Render any page that extends ``base.html.j2`` and assert each
    href + label is present. Uses the deals page so we don't depend on
    Today's data deps.
    """
    resp = client.get("/ui/deals")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert 'href="/ui/"' in body
    assert ">Today</a>" in body
    assert 'href="/ui/merchants"' in body
    assert ">Deals</a>" in body
    assert 'href="/ui/funders"' in body
    assert ">Funders</a>" in body
    assert 'href="/ui/upload"' in body
    assert ">Upload</a>" in body


def test_topstrip_settings_gear_dropdown_present(client: TestClient) -> None:
    """The ⚙ settings dropdown carries the admin surfaces."""
    resp = client.get("/ui/deals")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert 'class="settings-dropdown"' in body
    assert 'data-test-id="settings-pipeline"' in body
    assert 'href="/ui/pipeline"' in body
    assert 'data-test-id="settings-bank-coverage"' in body
    assert 'href="/ui/bank-coverage"' in body
    assert 'data-test-id="settings-calibration"' in body
    assert 'href="/ui/calibration"' in body
    assert 'data-test-id="settings-overrides"' in body
    assert 'href="/ui/overrides/summary"' in body
    assert 'data-test-id="settings-probe-review"' in body
    assert 'href="/ui/admin/text-layer-probe-review"' in body


def test_topstrip_notification_bell_renders_with_operator(
    client: TestClient,
) -> None:
    """The notification bell renders inside the topstrip when an
    operator is resolved. The badge mount carries the HTMX trigger that
    fetches the unread count; the badge itself is populated on the
    polled response (covered by test_notifications_route.py).
    """
    resp = client.get("/ui/deals")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert 'data-test-id="bell-wrap"' in body
    assert 'id="bell-badge-mount"' in body
    assert "/ui/notifications/unread-count" in body
