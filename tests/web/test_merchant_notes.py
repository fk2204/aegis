"""Feature C — operator notes panel route + dossier render.

Replaces the legacy single-text-column ``merchants.notes`` test surface
(migration 058) with the migration-066 ``merchant_notes`` row table
flow. Covers:

  * ``POST /ui/merchants/{merchant_id}/notes`` — happy path: a row is
    persisted via the repository, an audit row is written, and the
    response is a 303 redirect back to the dossier.
  * Empty submission — 400, no repo write, no audit row.
  * Oversize submission — 400 at the route boundary (mirrors the DB
    CHECK constraint), no repo write, no audit row.
  * Missing merchant — 404.
  * Audit details carry the length ONLY, not the body bytes (PII rule).
  * Dossier renders the panel with the right textarea + Save button
    + counter + cards list.
  * The panel is positioned ABOVE the existing chips section of the
    dossier — the document-on-file chips are the anchor.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_merchant_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.merchants.models import MERCHANT_NOTE_MAX_CHARS, MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.ops.operators import CF_ACCESS_EMAIL_HEADER

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def merchant_repo() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture
def client(
    audit: InMemoryAuditLog,
    merchant_repo: InMemoryMerchantRepository,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_merchant_repository] = lambda: merchant_repo
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def _seed_merchant(repo: InMemoryMerchantRepository) -> MerchantRow:
    m = MerchantRow(
        id=uuid4(),
        business_name="Notes Test LLC",
        owner_name="Owner",
        state="CA",
    )
    repo.upsert(m)
    return m


# ---------------------------------------------------------------------------
# Route — happy path, validation, audit
# ---------------------------------------------------------------------------


def test_save_note_persists_redirects_and_audits(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    m = _seed_merchant(merchant_repo)
    resp = client.post(
        f"/ui/merchants/{m.id}/notes",
        data={"body": "broker says deal is hot"},
        headers={CF_ACCESS_EMAIL_HEADER: "filip@commerafunding.com"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/ui/merchants/{m.id}"

    rows = merchant_repo.list_notes(merchant_id=m.id)
    assert len(rows) == 1
    assert rows[0].body == "broker says deal is hot"
    assert rows[0].actor == "filip@commerafunding.com"

    actions = [e["action"] for e in audit.entries]
    assert "merchant.note.added" in actions
    added = next(e for e in audit.entries if e["action"] == "merchant.note.added")
    assert added["actor"] == "operator"
    assert added["actor_email"] == "filip@commerafunding.com"
    assert added["subject_type"] == "merchant"
    assert added["subject_id"] == str(m.id)
    # Audit details must carry length ONLY — never the body bytes (PII).
    assert added["details"] == {"length": len("broker says deal is hot")}


def test_save_note_strips_surrounding_whitespace(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
) -> None:
    m = _seed_merchant(merchant_repo)
    client.post(
        f"/ui/merchants/{m.id}/notes",
        data={"body": "   broker quote came in low   \n"},
        follow_redirects=False,
    )
    rows = merchant_repo.list_notes(merchant_id=m.id)
    assert len(rows) == 1
    # Stored body is trimmed — leading/trailing whitespace doesn't enter
    # the database.
    assert rows[0].body == "broker quote came in low"


def test_empty_body_returns_400_and_writes_nothing(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    m = _seed_merchant(merchant_repo)
    resp = client.post(
        f"/ui/merchants/{m.id}/notes",
        data={"body": "   \n  \t  "},
        follow_redirects=False,
    )
    assert resp.status_code == 400

    assert merchant_repo.list_notes(merchant_id=m.id) == []
    actions = [e["action"] for e in audit.entries]
    assert "merchant.note.added" not in actions


def test_oversize_body_returns_400_and_writes_nothing(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    m = _seed_merchant(merchant_repo)
    too_long = "x" * (MERCHANT_NOTE_MAX_CHARS + 1)
    resp = client.post(
        f"/ui/merchants/{m.id}/notes",
        data={"body": too_long},
        follow_redirects=False,
    )
    assert resp.status_code == 400

    assert merchant_repo.list_notes(merchant_id=m.id) == []
    actions = [e["action"] for e in audit.entries]
    assert "merchant.note.added" not in actions


def test_save_note_404_when_merchant_missing(client: TestClient) -> None:
    bogus = uuid4()
    resp = client.post(
        f"/ui/merchants/{bogus}/notes",
        data={"body": "noise"},
        follow_redirects=False,
    )
    assert resp.status_code == 404


def test_save_note_falls_back_to_dashboard_actor_when_no_email_header(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """Without a CF Access SSO header (dev-loopback or test) the row is
    saved with ``actor='dashboard'`` and the audit row's ``actor_email``
    is ``None``."""
    m = _seed_merchant(merchant_repo)
    client.post(
        f"/ui/merchants/{m.id}/notes",
        data={"body": "no sso header in this test"},
        follow_redirects=False,
    )
    rows = merchant_repo.list_notes(merchant_id=m.id)
    assert rows[0].actor == "dashboard"

    added = next(e for e in audit.entries if e["action"] == "merchant.note.added")
    assert added["actor"] == "operator"
    assert added["actor_email"] is None


# ---------------------------------------------------------------------------
# Dossier rendering — panel visible, position above chips
# ---------------------------------------------------------------------------


def test_dossier_renders_empty_notes_panel(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
) -> None:
    """The operator-notes panel is lazy-loaded via HTMX (2026-06-28
    perf change). The dossier render now contains only a placeholder
    div; the panel body is served from
    ``GET /ui/merchants/{id}/operator-notes``. This test asserts the
    panel-body contract against the lazy endpoint directly — the
    dossier-position test below covers the hook + wrapper.
    """
    m = _seed_merchant(merchant_repo)
    panel_resp = client.get(f"/ui/merchants/{m.id}/operator-notes")
    assert panel_resp.status_code == 200
    panel = panel_resp.text

    assert 'id="merchant-notes-block"' in panel
    assert "no notes yet" in panel
    assert 'name="body"' in panel
    assert 'rows="6"' in panel
    assert f'action="/ui/merchants/{m.id}/notes"' in panel
    assert "Save note" in panel
    assert "data-merchant-notes-counter" in panel
    assert f"0 / {MERCHANT_NOTE_MAX_CHARS}" in panel
    assert f'maxlength="{MERCHANT_NOTE_MAX_CHARS}"' in panel


def test_dossier_renders_existing_notes_as_cards_newest_first(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
) -> None:
    """Cards render via the lazy endpoint (2026-06-28 perf change).
    Asserted directly against ``GET /ui/merchants/{id}/operator-notes``
    instead of the dossier render — bodies no longer appear inline on
    the initial dossier page load."""
    m = _seed_merchant(merchant_repo)
    # Two notes via the repo so the in-memory ``created_at`` is set; this
    # mirrors the route's persistence path without the redirect overhead.
    merchant_repo.add_note(merchant_id=m.id, body="older broker quote", actor="a@x.com")
    import time

    time.sleep(0.005)
    merchant_repo.add_note(merchant_id=m.id, body="newer pricing note", actor="b@x.com")

    panel_resp = client.get(f"/ui/merchants/{m.id}/operator-notes")
    assert panel_resp.status_code == 200
    panel = panel_resp.text

    assert "older broker quote" in panel
    assert "newer pricing note" in panel
    assert "merchant-notes-panel__cards" in panel
    pos_newer = panel.find("newer pricing note")
    pos_older = panel.find("older broker quote")
    assert pos_newer != -1 and pos_older != -1
    assert pos_newer < pos_older
    assert "2 notes on file" in panel


def test_notes_panel_renders_at_bottom_of_dossier_main(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
) -> None:
    """Post-2026-06-28 dossier contract: the operator-notes ``<section>``
    wrapper is the LAST section in ``<main>`` and carries an HTMX
    placeholder that lazy-loads the panel body from
    ``/ui/merchants/{id}/operator-notes``. The placeholder sits below
    the verdict section and immediately before the closing ``</main>``
    tag.
    """
    m = _seed_merchant(merchant_repo)
    resp = client.get(f"/ui/merchants/{m.id}", follow_redirects=False)
    assert resp.status_code == 200
    html = resp.text

    pos_sheet = html.find('<div class="sheet">')
    pos_name = html.find('class="title">Notes Test LLC')
    pos_verdict = html.find('id="verdict"')
    pos_main_close = html.find("</main>")
    pos_operator_notes_section = html.find('id="operator-notes"')
    pos_lazy_placeholder = html.find('data-test-id="dossier-operator-notes-lazy"')

    assert pos_sheet != -1, "sheet container missing — template drift?"
    assert pos_name != -1, "merchant name h1 missing — template drift?"
    assert pos_verdict != -1, "verdict section missing — template drift?"
    assert pos_main_close != -1, "main closing tag missing — template drift?"
    assert pos_operator_notes_section != -1, "operator-notes section wrapper missing"
    assert pos_lazy_placeholder != -1, "operator-notes lazy placeholder missing"

    # Wrapper sits after the verdict section and before closing main.
    assert pos_name < pos_sheet < pos_verdict < pos_operator_notes_section < pos_main_close
    # The lazy placeholder is INSIDE the operator-notes wrapper.
    assert pos_operator_notes_section < pos_lazy_placeholder < pos_main_close
