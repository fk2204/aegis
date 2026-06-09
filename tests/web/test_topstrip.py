"""Tests for the shared ``_topstrip.html.j2`` nav partial.

The partial is included by ``base.html.j2`` and
``merchant_detail_dossier.html.j2``. Pages that extend ``base`` get the
strip "for free" — the regression we're protecting against here is the
opposite: a nav link gets dropped, or a new page's ``active`` token
breaks the highlight contract.

The U13 Portfolio link is the explicit assertion. Other links sit in
the same template and would benefit from coverage too — keep this file
tight and add as needed.
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


def test_topstrip_includes_portfolio_link(client: TestClient) -> None:
    """The portfolio nav link is present on the dashboard root page.

    Render any page that extends ``base.html.j2`` and assert the
    rendered HTML contains ``href="/ui/portfolio"``. Using the deals
    page (``/ui/deals``) so we don't depend on the portfolio route
    being reachable — the contract under test is the partial.
    """
    resp = client.get("/ui/deals")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert 'href="/ui/portfolio"' in body
    assert ">Portfolio</a>" in body


def test_topstrip_portfolio_is_active_on_portfolio_page(client: TestClient) -> None:
    """``active='Portfolio'`` highlights the Portfolio link via the
    ``is-active`` class. Regression-prevent against the route handler
    forgetting to pass ``active`` into the template context.
    """
    resp = client.get("/ui/portfolio")
    assert resp.status_code == 200, resp.text
    body = resp.text
    # The active marker sits inline on the Portfolio <a> tag — exact
    # match against the rendered template line.
    assert (
        '<a href="/ui/portfolio" class="is-active">Portfolio</a>' in body
    )
