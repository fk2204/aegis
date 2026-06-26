"""POST /ui/merchants/{merchant_id}/close-rescan — manual rescan button.

Covers:
* Happy path enqueues with trigger='rescan' and operator's email
* ?override_cap=true threads through to the enqueue
* 404 when merchant has no close_lead_id
* 404 when merchant doesn't exist
* Cap-override button visibility (rendered iff latest close.orchestration.complete
  audit row carried capped=true)
* close.orchestration.manual_rescan audit row written
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
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.ops.operators import CF_ACCESS_EMAIL_HEADER


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


def _seed_merchant(
    repo: InMemoryMerchantRepository,
    *,
    close_lead_id: str | None = "lead_abc",
) -> MerchantRow:
    m = MerchantRow(
        id=uuid4(),
        business_name="Rescan Merchant",
        owner_name="Owner",
        state="CA",
        close_lead_id=close_lead_id,
    )
    repo.upsert(m)
    return m


def test_rescan_happy_path_enqueues_with_operator_email(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    m = _seed_merchant(merchant_repo)
    resp = client.post(
        f"/ui/merchants/{m.id}/close-rescan",
        headers={CF_ACCESS_EMAIL_HEADER: "filip@commerafunding.com"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/ui/merchants/{m.id}"

    pending = getattr(
        client.app.state,
        "pending_close_orchestration_jobs",
        [],  # type: ignore[attr-defined]
    )
    assert pending == [
        {
            "close_lead_id": "lead_abc",
            "trigger": "rescan",
            "actor_email": "filip@commerafunding.com",
            "override_cap": False,
            "ignore_pin": False,
        }
    ]

    actions = [e["action"] for e in audit.entries]
    assert "close.orchestration.enqueued" in actions
    assert "close.orchestration.manual_rescan" in actions

    manual = next(e for e in audit.entries if e["action"] == "close.orchestration.manual_rescan")
    assert manual["actor_email"] == "filip@commerafunding.com"
    assert manual["details"]["override_cap"] is False
    assert manual["details"]["close_lead_id"] == "lead_abc"


def test_rescan_with_override_cap_threads_through(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    m = _seed_merchant(merchant_repo)
    resp = client.post(
        f"/ui/merchants/{m.id}/close-rescan?override_cap=true",
        headers={CF_ACCESS_EMAIL_HEADER: "dima@commerafunding.com"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    pending = getattr(
        client.app.state,
        "pending_close_orchestration_jobs",
        [],  # type: ignore[attr-defined]
    )
    assert pending[0]["override_cap"] is True
    assert pending[0]["actor_email"] == "dima@commerafunding.com"
    assert pending[0]["trigger"] == "rescan"

    manual = next(e for e in audit.entries if e["action"] == "close.orchestration.manual_rescan")
    assert manual["details"]["override_cap"] is True


def test_rescan_404_when_no_close_lead_id(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
) -> None:
    m = _seed_merchant(merchant_repo, close_lead_id=None)
    resp = client.post(
        f"/ui/merchants/{m.id}/close-rescan",
        follow_redirects=False,
    )
    assert resp.status_code == 404
    assert "no close_lead_id" in resp.json()["detail"]


def test_rescan_404_when_merchant_missing(
    client: TestClient,
) -> None:
    bogus = uuid4()
    resp = client.post(
        f"/ui/merchants/{bogus}/close-rescan",
        follow_redirects=False,
    )
    assert resp.status_code == 404


def test_rescan_button_hidden_when_no_close_lead_id(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
) -> None:
    """Merchant detail must not render the rescan form when there's no
    linked Close Lead."""
    m = _seed_merchant(merchant_repo, close_lead_id=None)
    resp = client.get(f"/ui/merchants/{m.id}", follow_redirects=False)
    assert resp.status_code == 200
    assert "Rescan Close attachments" not in resp.text


def test_rescan_button_visible_when_close_lead_set_no_cap(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
) -> None:
    """Primary rescan button visible; cap-override button hidden when
    latest orchestration didn't hit the cap."""
    m = _seed_merchant(merchant_repo)
    resp = client.get(f"/ui/merchants/{m.id}", follow_redirects=False)
    assert resp.status_code == 200
    assert "Rescan Close attachments" in resp.text
    assert "Rescan all (override cap)" not in resp.text


def test_cap_override_button_visible_after_capped_run(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """When the most recent close.orchestration.complete row has
    capped=true, the merchant detail surfaces the override button."""
    m = _seed_merchant(merchant_repo)
    # Simulate a prior capped orchestration run via direct audit insert.
    audit.record(
        actor="worker",
        action="close.orchestration.complete",
        subject_type="merchant",
        subject_id=m.id,
        details={
            "trigger": "webhook",
            "close_lead_id": "lead_abc",
            "total": 17,
            "fetched": 15,
            "skipped": 0,
            "failed": 0,
            "duplicates": 0,
            "capped": True,
            "override_cap": False,
        },
    )
    resp = client.get(f"/ui/merchants/{m.id}", follow_redirects=False)
    assert resp.status_code == 200
    assert "Rescan all (override cap)" in resp.text


def test_cap_override_button_hidden_after_uncapped_run(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """A capped:false summary on the most recent row → no override button.
    Even if an OLDER row was capped, the latest one wins."""
    m = _seed_merchant(merchant_repo)
    # Older row was capped...
    audit.record(
        actor="worker",
        action="close.orchestration.complete",
        subject_type="merchant",
        subject_id=m.id,
        details={"trigger": "webhook", "capped": True},
    )
    # ... but the latest row is fine. _close_orchestration_last_capped
    # walks history newest-first; this is the row it must see.
    audit.record(
        actor="worker",
        action="close.orchestration.complete",
        subject_type="merchant",
        subject_id=m.id,
        details={"trigger": "rescan", "capped": False},
    )
    resp = client.get(f"/ui/merchants/{m.id}", follow_redirects=False)
    assert resp.status_code == 200
    assert "Rescan all (override cap)" not in resp.text


# ---------------------------------------------------------------------------
# Close-lead cross-link (Wave 2 §2.1: kill the dead-button)
# ---------------------------------------------------------------------------


def test_close_lead_link_points_to_app_close_com_in_new_tab(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
) -> None:
    """The Close-lead cross-link must open the actual Close CRM lead in a
    new tab. Operator hard constraint: no dead-end buttons."""
    m = _seed_merchant(merchant_repo, close_lead_id="lead_abc")
    resp = client.get(f"/ui/merchants/{m.id}", follow_redirects=False)
    assert resp.status_code == 200
    html = resp.text

    # Full Close-app URL with trailing slash.
    assert 'href="https://app.close.com/lead/lead_abc/"' in html
    # Opens in a new tab.
    assert 'target="_blank"' in html
    # Security: noopener (+noreferrer) on the same anchor.
    assert 'rel="noopener noreferrer"' in html
    # Display text preserved.
    assert "↗ Close lead lead_abc" in html


def test_merchant_detail_has_no_placeholder_hrefs(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
) -> None:
    """Regression guard: no anchor on the merchant detail page may use the
    placeholder href="#". If a future developer reintroduces a dead-end
    button anywhere on the dossier, this test fails."""
    m = _seed_merchant(merchant_repo, close_lead_id="lead_abc")
    resp = client.get(f"/ui/merchants/{m.id}", follow_redirects=False)
    assert resp.status_code == 200
    assert 'href="#"' not in resp.text


def test_close_lead_link_not_rendered_when_lead_id_missing(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
) -> None:
    """The {% if merchant.close_lead_id %} gate must suppress the link
    entirely when there is no linked Close Lead — no empty href, no
    bare arrow."""
    m = _seed_merchant(merchant_repo, close_lead_id=None)
    resp = client.get(f"/ui/merchants/{m.id}", follow_redirects=False)
    assert resp.status_code == 200
    html = resp.text

    assert "app.close.com/lead/" not in html
    assert "↗ Close lead" not in html


# ===================================================================
# Pin gate UI (commit 5 of fix/close-note-attachments)
# ===================================================================


def test_rescan_with_ignore_pin_threads_through(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """?ignore_pin=true is enqueued onto process_close_attachments and
    the manual_rescan audit row captures it."""
    m = _seed_merchant(merchant_repo)
    resp = client.post(
        f"/ui/merchants/{m.id}/close-rescan?ignore_pin=true",
        headers={CF_ACCESS_EMAIL_HEADER: "filip@commerafunding.com"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    pending = getattr(
        client.app.state,
        "pending_close_orchestration_jobs",
        [],  # type: ignore[attr-defined]
    )
    assert pending[0]["ignore_pin"] is True
    assert pending[0]["override_cap"] is False
    assert pending[0]["trigger"] == "rescan"

    manual = next(e for e in audit.entries if e["action"] == "close.orchestration.manual_rescan")
    assert manual["details"]["ignore_pin"] is True
    assert manual["details"]["override_cap"] is False


def test_rescan_with_both_overrides_threads_through(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
) -> None:
    """Both override_cap=true and ignore_pin=true on the same rescan."""
    m = _seed_merchant(merchant_repo)
    resp = client.post(
        f"/ui/merchants/{m.id}/close-rescan?override_cap=true&ignore_pin=true",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    pending = getattr(
        client.app.state,
        "pending_close_orchestration_jobs",
        [],  # type: ignore[attr-defined]
    )
    assert pending[0]["override_cap"] is True
    assert pending[0]["ignore_pin"] is True


def test_ignore_pin_button_visible_when_latest_run_had_unpinned_pdfs(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """A 'close.attachment.skipped' with reason=not_pinned in the latest
    orchestration window → '⚡ Rescan all unpinned PDFs (ignore pin)'
    button visible."""
    m = _seed_merchant(merchant_repo)
    # Simulate a prior orchestration: a not_pinned skip then complete.
    audit.record(
        actor="worker",
        action="close.attachment.skipped",
        subject_type="merchant",
        subject_id=m.id,
        details={"reason": "not_pinned", "filename": "voided_check.pdf"},
    )
    audit.record(
        actor="worker",
        action="close.orchestration.complete",
        subject_type="merchant",
        subject_id=m.id,
        details={"trigger": "webhook", "capped": False, "fetched": 0},
    )
    resp = client.get(f"/ui/merchants/{m.id}", follow_redirects=False)
    assert resp.status_code == 200
    assert "Rescan all unpinned PDFs (ignore pin)" in resp.text


def test_ignore_pin_button_visible_after_no_pinned_files_signal(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """close.orchestration.no_pinned_files in the latest window also
    triggers the ignore-pin button."""
    m = _seed_merchant(merchant_repo)
    audit.record(
        actor="worker",
        action="close.orchestration.no_pinned_files",
        subject_type="merchant",
        subject_id=m.id,
        details={
            "close_lead_id": "lead_abc",
            "total_pdfs_seen": 3,
            "unpinned_pdfs": [],
        },
    )
    audit.record(
        actor="worker",
        action="close.orchestration.complete",
        subject_type="merchant",
        subject_id=m.id,
        details={"trigger": "webhook", "capped": False, "fetched": 0},
    )
    resp = client.get(f"/ui/merchants/{m.id}", follow_redirects=False)
    assert "Rescan all unpinned PDFs (ignore pin)" in resp.text


def test_ignore_pin_button_hidden_when_latest_run_had_no_unpinned(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """Latest run had no not_pinned skips and no no_pinned_files → no
    ignore-pin button. Just the default rescan."""
    m = _seed_merchant(merchant_repo)
    audit.record(
        actor="worker",
        action="close.attachment.fetched",
        subject_type="document",
        subject_id=m.id,
        details={"close_lead_id": "lead_abc", "duplicate": False},
    )
    audit.record(
        actor="worker",
        action="close.orchestration.complete",
        subject_type="merchant",
        subject_id=m.id,
        details={"trigger": "webhook", "capped": False, "fetched": 1},
    )
    resp = client.get(f"/ui/merchants/{m.id}", follow_redirects=False)
    assert "Rescan all unpinned PDFs (ignore pin)" not in resp.text
    # Default button is still there
    assert "Rescan Close attachments" in resp.text


def test_empty_state_message_shown_after_no_pinned_files(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """Latest orchestration audited no_pinned_files → merchant detail
    surfaces the 'Pin the bank-statement files...' message."""
    m = _seed_merchant(merchant_repo)
    audit.record(
        actor="worker",
        action="close.orchestration.no_pinned_files",
        subject_type="merchant",
        subject_id=m.id,
        details={"close_lead_id": "lead_abc"},
    )
    audit.record(
        actor="worker",
        action="close.orchestration.complete",
        subject_type="merchant",
        subject_id=m.id,
        details={"trigger": "webhook", "capped": False, "fetched": 0},
    )
    resp = client.get(f"/ui/merchants/{m.id}", follow_redirects=False)
    assert "No statements imported." in resp.text
    assert "Pin the bank-statement files in Close" in resp.text


def test_empty_state_message_hidden_when_not_pinned_skips_only(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """Latest run had not_pinned skips (some PDFs unpinned) but ALSO
    pinned PDFs that got processed → empty-state message NOT shown
    (only fires on the all-unpinned case). The ignore-pin button IS
    shown because there were unpinned PDFs."""
    m = _seed_merchant(merchant_repo)
    audit.record(
        actor="worker",
        action="close.attachment.skipped",
        subject_type="merchant",
        subject_id=m.id,
        details={"reason": "not_pinned"},
    )
    audit.record(
        actor="worker",
        action="close.orchestration.complete",
        subject_type="merchant",
        subject_id=m.id,
        details={"trigger": "webhook", "capped": False, "fetched": 2},
    )
    resp = client.get(f"/ui/merchants/{m.id}", follow_redirects=False)
    assert "No statements imported." not in resp.text
    assert "Rescan all unpinned PDFs (ignore pin)" in resp.text


def test_latest_window_helper_stops_at_prior_complete(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """An older orchestration that capped should NOT influence the
    current view. Only signals between the latest two complete rows
    count."""
    m = _seed_merchant(merchant_repo)
    # OLDER run had not_pinned skips
    audit.record(
        actor="worker",
        action="close.attachment.skipped",
        subject_type="merchant",
        subject_id=m.id,
        details={"reason": "not_pinned"},
    )
    audit.record(
        actor="worker",
        action="close.orchestration.complete",
        subject_type="merchant",
        subject_id=m.id,
        details={"trigger": "webhook", "capped": False, "fetched": 0},
    )
    # LATEST run had a clean fetch, no unpinned PDFs
    audit.record(
        actor="worker",
        action="close.attachment.fetched",
        subject_type="document",
        subject_id=m.id,
        details={"close_lead_id": "lead_abc"},
    )
    audit.record(
        actor="worker",
        action="close.orchestration.complete",
        subject_type="merchant",
        subject_id=m.id,
        details={"trigger": "rescan", "capped": False, "fetched": 1},
    )
    resp = client.get(f"/ui/merchants/{m.id}", follow_redirects=False)
    # Older run's not_pinned skip must NOT bleed into this view.
    assert "Rescan all unpinned PDFs (ignore pin)" not in resp.text
