"""Tests for the merchant-notes append-only flow.

Covers:

* the pure ``_prepend_timestamped_note`` helper across new / existing
  / whitespace-only existing inputs;
* ``POST /ui/merchants/{merchant_id}/notes`` returns the rendered
  notes block partial as HTMX outerHTML;
* an empty (whitespace-only) submission is a no-op — operator can't
  accidentally wipe history by hitting Submit on a blank textarea;
* repeated saves stack newest-first;
* the dossier renders the operator-notes block at the top and
  populates the textarea container even for a merchant with no notes
  yet;
* an audit row lands on every accepted save.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
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
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.ops.operators import CF_ACCESS_EMAIL_HEADER
from aegis.web.routers.merchants import _prepend_timestamped_note

# ---------------------------------------------------------------------------
# Pure prepend helper
# ---------------------------------------------------------------------------


def _fixed_now() -> datetime:
    return datetime(2026, 6, 15, 12, 34, tzinfo=UTC)


def test_prepend_to_empty_notes_produces_single_timestamped_line() -> None:
    out = _prepend_timestamped_note(
        existing=None,
        new_text="broker mentioned past stacking issues",
        now=_fixed_now(),
        author="filip@commerafunding.com",
    )
    assert out == (
        "[2026-06-15 12:34 UTC] filip@commerafunding.com — broker mentioned past stacking issues"
    )


def test_prepend_to_whitespace_only_existing_treats_as_empty() -> None:
    out = _prepend_timestamped_note(
        existing="   \n\n  ",
        new_text="first real note",
        now=_fixed_now(),
        author="dashboard",
    )
    assert out == "[2026-06-15 12:34 UTC] dashboard — first real note"


def test_prepend_to_existing_stacks_newest_first_with_blank_line() -> None:
    existing = "[2026-06-14 09:00 UTC] dashboard — earlier note"
    out = _prepend_timestamped_note(
        existing=existing,
        new_text="follow-up",
        now=_fixed_now(),
        author="filip@commerafunding.com",
    )
    assert out == (
        "[2026-06-15 12:34 UTC] filip@commerafunding.com — follow-up\n\n"
        "[2026-06-14 09:00 UTC] dashboard — earlier note"
    )


def test_prepend_strips_new_text_whitespace() -> None:
    out = _prepend_timestamped_note(
        existing=None,
        new_text="   text with surrounding whitespace   \n",
        now=_fixed_now(),
        author="dashboard",
    )
    assert "text with surrounding whitespace" in out
    assert "   text" not in out


# ---------------------------------------------------------------------------
# Route — HTMX swap, audit, persistence
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


def _seed_merchant(repo: InMemoryMerchantRepository, *, notes: str | None = None) -> MerchantRow:
    m = MerchantRow(
        id=uuid4(),
        business_name="Notes Test LLC",
        owner_name="Owner",
        state="CA",
        notes=notes,
    )
    repo.upsert(m)
    return m


def test_save_note_persists_and_returns_partial(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    m = _seed_merchant(merchant_repo)
    resp = client.post(
        f"/ui/merchants/{m.id}/notes",
        data={"note_text": "broker says deal is hot"},
        headers={CF_ACCESS_EMAIL_HEADER: "filip@commerafunding.com"},
    )
    assert resp.status_code == 200
    assert 'id="merchant-notes-block"' in resp.text
    assert "broker says deal is hot" in resp.text
    assert "filip@commerafunding.com" in resp.text

    updated = merchant_repo.get(m.id)
    assert updated.notes is not None
    assert "broker says deal is hot" in updated.notes
    assert "filip@commerafunding.com" in updated.notes

    actions = [e["action"] for e in audit.entries]
    assert "merchant.note_added" in actions
    added = next(e for e in audit.entries if e["action"] == "merchant.note_added")
    assert added["actor_email"] == "filip@commerafunding.com"
    assert added["details"]["note_chars"] == len("broker says deal is hot")


def test_repeated_saves_stack_newest_first(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
) -> None:
    m = _seed_merchant(merchant_repo)
    client.post(
        f"/ui/merchants/{m.id}/notes",
        data={"note_text": "first"},
        headers={CF_ACCESS_EMAIL_HEADER: "a@example.com"},
    )
    client.post(
        f"/ui/merchants/{m.id}/notes",
        data={"note_text": "second"},
        headers={CF_ACCESS_EMAIL_HEADER: "b@example.com"},
    )

    updated = merchant_repo.get(m.id)
    assert updated.notes is not None
    # "second" is more recent — appears before "first" in the stored
    # value.
    pos_second = updated.notes.find("second")
    pos_first = updated.notes.find("first")
    assert pos_second != -1
    assert pos_first != -1
    assert pos_second < pos_first


def test_empty_submission_is_noop(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    m = _seed_merchant(merchant_repo, notes="[2026-06-14] earlier note")
    resp = client.post(
        f"/ui/merchants/{m.id}/notes",
        data={"note_text": "   \n  \t  "},
        headers={CF_ACCESS_EMAIL_HEADER: "filip@commerafunding.com"},
    )
    assert resp.status_code == 200
    # Existing notes still present in the rendered partial.
    assert "earlier note" in resp.text

    # Repo unchanged.
    after = merchant_repo.get(m.id)
    assert after.notes == "[2026-06-14] earlier note"

    # No audit row written for a no-op.
    actions = [e["action"] for e in audit.entries]
    assert "merchant.note_added" not in actions


def test_save_note_404_when_merchant_missing(client: TestClient) -> None:
    bogus = uuid4()
    resp = client.post(
        f"/ui/merchants/{bogus}/notes",
        data={"note_text": "noise"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Dossier rendering — block visible for new + populated merchants
# ---------------------------------------------------------------------------


def test_dossier_renders_notes_block_for_merchant_with_no_notes(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
) -> None:
    m = _seed_merchant(merchant_repo, notes=None)
    resp = client.get(f"/ui/merchants/{m.id}", follow_redirects=False)
    assert resp.status_code == 200
    html = resp.text
    # Block container present.
    assert 'id="merchant-notes-block"' in html
    # Empty-state footnote present.
    assert "no notes yet" in html
    # Textarea present + HTMX wired.
    assert 'name="note_text"' in html
    assert f'hx-post="/ui/merchants/{m.id}/notes"' in html


def test_dossier_renders_existing_notes(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
) -> None:
    m = _seed_merchant(
        merchant_repo,
        notes="[2026-06-14 09:00 UTC] dashboard — broker quote came in low",
    )
    resp = client.get(f"/ui/merchants/{m.id}", follow_redirects=False)
    assert resp.status_code == 200
    assert "broker quote came in low" in resp.text
    # Footnote should reflect "1 line on file"
    assert "1 line on file" in resp.text
