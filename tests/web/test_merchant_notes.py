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
    m = _seed_merchant(merchant_repo)
    resp = client.get(f"/ui/merchants/{m.id}", follow_redirects=False)
    assert resp.status_code == 200
    html = resp.text

    # Panel container is present.
    assert 'id="merchant-notes-block"' in html
    # Empty-state copy when no notes.
    assert "no notes yet" in html
    # 6-row textarea wired to the route.
    assert 'name="body"' in html
    assert 'rows="6"' in html
    assert f'action="/ui/merchants/{m.id}/notes"' in html
    # Save button label.
    assert "Save note" in html
    # Character counter element with the cap.
    assert "data-merchant-notes-counter" in html
    assert f"0 / {MERCHANT_NOTE_MAX_CHARS}" in html
    # Max-length attribute on the textarea reflects the cap.
    assert f'maxlength="{MERCHANT_NOTE_MAX_CHARS}"' in html


def test_dossier_renders_existing_notes_as_cards_newest_first(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
) -> None:
    m = _seed_merchant(merchant_repo)
    # Two notes via the repo so the in-memory ``created_at`` is set; this
    # mirrors the route's persistence path without the redirect overhead.
    merchant_repo.add_note(merchant_id=m.id, body="older broker quote", actor="a@x.com")
    import time

    time.sleep(0.005)
    merchant_repo.add_note(merchant_id=m.id, body="newer pricing note", actor="b@x.com")

    resp = client.get(f"/ui/merchants/{m.id}", follow_redirects=False)
    assert resp.status_code == 200
    html = resp.text

    # Both bodies appear.
    assert "older broker quote" in html
    assert "newer pricing note" in html
    # Card list class present.
    assert "merchant-notes-panel__cards" in html
    # Newest-first — "newer pricing note" appears before "older broker
    # quote" in the rendered HTML.
    pos_newer = html.find("newer pricing note")
    pos_older = html.find("older broker quote")
    assert pos_newer != -1 and pos_older != -1
    assert pos_newer < pos_older

    # Footnote reflects the count.
    assert "2 notes on file" in html


def test_notes_panel_renders_below_name_and_above_sheet(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
) -> None:
    """The dossier contract for Feature C: the operator notes panel sits
    between the merchant name (in the masthead) and the chips section
    (inside ``<div class="sheet">``). The sheet div is the stable anchor
    that always renders — the document-on-file chips only appear once a
    document has been uploaded and the score panel has rendered, so the
    sheet container is what we lock against. Notes block landing
    BETWEEN the name and the sheet ensures it sits above every chip on
    the dossier.
    """
    m = _seed_merchant(merchant_repo)
    resp = client.get(f"/ui/merchants/{m.id}", follow_redirects=False)
    assert resp.status_code == 200
    html = resp.text

    pos_notes = html.find('id="merchant-notes-block"')
    pos_sheet = html.find('<div class="sheet">')
    pos_name = html.find('class="title">Notes Test LLC')

    assert pos_notes != -1, "notes block missing from dossier"
    assert pos_sheet != -1, "sheet container missing — template drift?"
    assert pos_name != -1, "merchant name h1 missing — template drift?"

    # Notes panel sits BELOW the merchant name and ABOVE the sheet
    # container (which holds every chip on the page).
    assert pos_name < pos_notes < pos_sheet
