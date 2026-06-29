"""Phase 2 / item 7.1 — Close ``lead.updated`` with ``description`` in
``changed_fields`` auto-syncs ``merchants.stated_*`` from the structured
FINANCIAL block.

Sibling to ``test_webhooks_close_qualified_opp_open.py``: same dispatch
entry (lead.updated), different changed_field. Coverage:

* (a) description-change event, merchant exists, FINANCIAL changed
      → diff applied + ``close.webhook.stated_fields_auto_synced`` audit.
* (b) description-change event, merchant exists, FINANCIAL identical
      → no-op + ``close.webhook.description_change_no_op`` audit.
* (c) description-change event, merchant exists, no FINANCIAL block
      → skip + ``close.webhook.description_change_skipped``
      (reason=no_financial_block).
* (d) description-change event, no merchant → skip +
      ``close.webhook.description_change_skipped``
      (reason=merchant_not_found).
* (e) lead.updated event WITHOUT ``description`` in changed_fields →
      handler skips entirely (no description-change audit rows).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_close_client,
    get_merchant_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.close.client import CloseClient
from aegis.config import get_settings
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository

_TEST_SECRET_HEX = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"  # noqa: S105 — test stub, not a real secret

_LEAD_ID = "lead_description_test_001"

# Description payload with a FINANCIAL block matching the shape verified
# on prod leads (see migration 087 docstring).
_DESCRIPTION_WITH_FINANCIAL = """\
Inbound from broker — refreshed application data.

FINANCIAL:
  Requested Amount: 75000
  Use of Funds: Working capital
  Monthly Gross Revenue: 250000
  Monthly Deposits: 22
  Existing MCA Positions: 1
  Current Lenders: Velocity
  Existing MCA Balance: 12000
  Daily/Weekly Payment: 850
  Bank: Chase
"""

# Description without a FINANCIAL block — free-form intake notes.
_DESCRIPTION_NO_FINANCIAL = """\
Operator left a free-form note here — no structured block.
Operator: please follow up with the broker.
"""


def _sign(timestamp: str, body: bytes, secret_hex: str = _TEST_SECRET_HEX) -> str:
    """Compute the close-sig-hash value Close would send."""
    secret = bytes.fromhex(secret_hex)
    return hmac.new(secret, timestamp.encode("utf-8") + body, hashlib.sha256).hexdigest()


def _lead_payload(
    *,
    close_lead_id: str = _LEAD_ID,
    description: str | None = _DESCRIPTION_WITH_FINANCIAL,
) -> dict[str, Any]:
    """Canned Close Lead GET response carrying only the description
    field; other custom fields are absent so the field-map path stays
    inert on the description handler (which never re-reads custom
    fields)."""
    payload: dict[str, Any] = {
        "id": close_lead_id,
        "display_name": "Description Sync Co.",
    }
    if description is not None:
        payload["description"] = description
    return payload


def _description_event(
    *,
    lead_id: str = _LEAD_ID,
    changed_fields: list[str] | None = None,
    event_id: str = "ev_description_001",
    action: str = "updated",
) -> dict[str, Any]:
    """Build a Close webhook event payload for a ``lead.updated`` event."""
    return {
        "event": {
            "id": event_id,
            "date_created": "2026-06-28T12:00:00+00:00",
            "action": action,
            "object_type": "lead",
            "object_id": lead_id,
            "lead_id": lead_id,
            "organization_id": "orga_test",
            "user_id": "user_op",
            "changed_fields": (changed_fields if changed_fields is not None else ["description"]),
            "previous_data": {},
            "data": {},
            "meta": {},
            "request_id": "req_description_001",
        },
        "subscription_id": "whsub_description_test",
    }


@pytest.fixture()
def repo() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture()
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture()
def lead_description_state() -> dict[str, str | None]:
    """Mutable container so individual tests can swap the description
    payload the stub transport returns without re-binding the fixture."""
    return {"description": _DESCRIPTION_WITH_FINANCIAL}


@pytest.fixture()
def stub_close_client(
    monkeypatch: pytest.MonkeyPatch,
    lead_description_state: dict[str, str | None],
) -> CloseClient:
    """CloseClient backed by a path-aware mock transport that returns
    the canned Lead payload for ``/api/v1/lead/`` and empty stubs for
    other paths. ``lead_description_state`` lets each test set the
    description body before posting the webhook."""
    # Force-set env (per .claude/rules/testing.md — never setdefault).
    monkeypatch.setenv("CLOSE_API_KEY", "api_test")
    monkeypatch.setenv("CLOSE_API_BASE", "https://api.close.example")
    monkeypatch.setenv("CLOSE_WEBHOOK_SECRET", _TEST_SECRET_HEX)
    monkeypatch.setenv("CLOSE_QUALIFIED_OPP_OPEN_STATUS_ID", "stat_unused_for_this_test")
    get_settings.cache_clear()

    def transport(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/api/v1/lead/"):
            return httpx.Response(
                200,
                json=_lead_payload(description=lead_description_state["description"]),
            )
        if path.startswith("/api/v1/opportunity/"):
            return httpx.Response(200, json={"data": [], "has_more": False})
        if path.startswith("/api/v1/activity/note/") or path.startswith("/api/v1/activity/email/"):
            return httpx.Response(200, json={"data": [], "has_more": False})
        if path.startswith("/api/v1/activity/call/"):
            return httpx.Response(200, json={"data": [], "has_more": False})
        return httpx.Response(200, json={"id": _LEAD_ID})

    return CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport)))


@pytest.fixture()
def client(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    stub_close_client: CloseClient,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: repo
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_close_client] = lambda: stub_close_client
    with TestClient(app) as tc:
        yield tc
    reset_dependency_caches()


def _post_signed(client: TestClient, body_dict: dict[str, Any]) -> Any:
    timestamp = str(int(time.time()))
    raw = json.dumps(body_dict).encode("utf-8")
    sig = _sign(timestamp, raw)
    return client.post(
        "/webhooks/close",
        content=raw,
        headers={
            "content-type": "application/json",
            "close-sig-hash": sig,
            "close-sig-timestamp": timestamp,
        },
    )


def _audit_actions(audit: InMemoryAuditLog, action: str) -> list[dict[str, Any]]:
    return [r for r in audit.entries if r["action"] == action]


def _seed_merchant(
    repo: InMemoryMerchantRepository,
    *,
    close_lead_id: str = _LEAD_ID,
    requested_amount: Decimal | None = Decimal("50000"),
    monthly_revenue: Decimal | None = Decimal("100000"),
    stated_bank: str | None = "Wells Fargo",
) -> MerchantRow:
    """Seed a merchant carrying older stated_* values so a description
    refresh has something to diff against."""
    return repo.upsert(
        MerchantRow(
            business_name="Existing Merchant",
            owner_name="J Owner",
            state="CA",
            close_lead_id=close_lead_id,
            requested_amount=requested_amount,
            monthly_revenue=monthly_revenue,
            stated_bank=stated_bank,
            stated_mca_positions=0,
            stated_current_lenders=[],
        )
    )


# ----------------------------------------------------------------------
# Case (a): merchant exists, FINANCIAL block changed → diff applied
# ----------------------------------------------------------------------


def test_description_change_applies_diff_when_financial_block_changed(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    lead_description_state: dict[str, str | None],
) -> None:
    """A lead.updated event whose changed_fields contains ``description``,
    and whose new description body carries a FINANCIAL block that differs
    from the merchant's current stated_* values, refreshes the merchant
    row via ``financial_diff`` and writes a
    ``close.webhook.stated_fields_auto_synced`` audit row."""
    seeded = _seed_merchant(repo)
    lead_description_state["description"] = _DESCRIPTION_WITH_FINANCIAL

    resp = _post_signed(client, _description_event())
    assert resp.status_code == 204, resp.text

    refreshed = repo.find_by_close_lead_id(_LEAD_ID)
    assert refreshed is not None
    assert refreshed.id == seeded.id
    # FINANCIAL block values overwrote the older seeded values.
    assert refreshed.requested_amount == Decimal("75000")
    assert refreshed.monthly_revenue == Decimal("250000")
    assert refreshed.stated_monthly_deposits == 22
    assert refreshed.stated_mca_positions == 1
    assert refreshed.stated_current_lenders == ["Velocity"]
    assert refreshed.stated_mca_balance == Decimal("12000")
    assert refreshed.stated_daily_payment == Decimal("850")
    assert refreshed.stated_bank == "Chase"
    assert refreshed.use_of_funds == "Working capital"

    synced = _audit_actions(audit, "close.webhook.stated_fields_auto_synced")
    assert len(synced) == 1
    details = synced[0]["details"]
    assert details["close_lead_id"] == _LEAD_ID
    assert details["merchant_id"] == str(seeded.id)
    assert details["event_id"] == "ev_description_001"
    assert details["field_count"] >= 7
    changed = details["changed_keys"]
    assert "requested_amount" in changed
    assert "monthly_revenue" in changed
    assert "stated_bank" in changed
    assert "stated_current_lenders" in changed

    # No skip / no-op rows fired.
    assert _audit_actions(audit, "close.webhook.description_change_skipped") == []
    assert _audit_actions(audit, "close.webhook.description_change_no_op") == []


# ----------------------------------------------------------------------
# Case (b): merchant exists, FINANCIAL block identical → no-op
# ----------------------------------------------------------------------


def test_description_change_no_op_when_financial_block_identical(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    lead_description_state: dict[str, str | None],
) -> None:
    """If the FINANCIAL block parses to values that all match the
    merchant's current stated_* fields, the handler writes a no-op
    audit row and performs no upsert."""
    # Seed with values that exactly match the FINANCIAL block above.
    repo.upsert(
        MerchantRow(
            business_name="Existing Merchant",
            owner_name="J Owner",
            state="CA",
            close_lead_id=_LEAD_ID,
            requested_amount=Decimal("75000"),
            use_of_funds="Working capital",
            monthly_revenue=Decimal("250000"),
            stated_monthly_deposits=22,
            stated_mca_positions=1,
            stated_current_lenders=["Velocity"],
            stated_mca_balance=Decimal("12000"),
            stated_daily_payment=Decimal("850"),
            stated_bank="Chase",
        )
    )
    lead_description_state["description"] = _DESCRIPTION_WITH_FINANCIAL

    resp = _post_signed(client, _description_event())
    assert resp.status_code == 204, resp.text

    no_op = _audit_actions(audit, "close.webhook.description_change_no_op")
    assert len(no_op) == 1
    details = no_op[0]["details"]
    assert details["close_lead_id"] == _LEAD_ID
    assert details["event_id"] == "ev_description_001"

    # No sync / skip rows fired by this handler.
    assert _audit_actions(audit, "close.webhook.stated_fields_auto_synced") == []
    assert _audit_actions(audit, "close.webhook.description_change_skipped") == []


# ----------------------------------------------------------------------
# Case (c): merchant exists, no FINANCIAL block → skip
# ----------------------------------------------------------------------


def test_description_change_skips_when_no_financial_block(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    lead_description_state: dict[str, str | None],
) -> None:
    """A description without a FINANCIAL block (free-form text, DEAL
    block, etc.) is skipped without invoking the Bedrock extractor.
    The handler writes a ``close.webhook.description_change_skipped``
    audit row with reason=no_financial_block.

    Assertion focus is the audit-row contract; the sibling
    ``_handle_lead_updated`` path runs alongside the description
    handler and is free to touch other merchant fields via the
    custom-field upsert — that's not this test's concern.
    """
    seeded = _seed_merchant(repo)
    lead_description_state["description"] = _DESCRIPTION_NO_FINANCIAL

    resp = _post_signed(client, _description_event())
    assert resp.status_code == 204, resp.text

    skipped = _audit_actions(audit, "close.webhook.description_change_skipped")
    # Match only the description-handler skip (not the sibling
    # qualified_skipped path) by reason key.
    matching = [r for r in skipped if r["details"].get("reason") == "no_financial_block"]
    assert len(matching) == 1
    details = matching[0]["details"]
    assert details["close_lead_id"] == _LEAD_ID
    assert details["event_id"] == "ev_description_001"

    # The description handler must not have driven any sync / no-op
    # writes — those are the auto-sync audit lines.
    assert _audit_actions(audit, "close.webhook.stated_fields_auto_synced") == []
    assert _audit_actions(audit, "close.webhook.description_change_no_op") == []

    # Merchant row still exists and keeps the seeded id. (The sibling
    # ``_handle_lead_updated`` path may have touched other custom-field
    # columns through its own upsert; we only assert the merchant
    # identity stayed stable — our handler created no second row.)
    refreshed = repo.find_by_close_lead_id(_LEAD_ID)
    assert refreshed is not None
    assert refreshed.id == seeded.id


# ----------------------------------------------------------------------
# Case (d): description-change event, no merchant exists → skip
# ----------------------------------------------------------------------


def test_description_change_skips_when_merchant_not_found(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    lead_description_state: dict[str, str | None],
) -> None:
    """A description-change event for a lead with no matching merchant
    is skipped — merchant creation is owned by the sibling status-change
    handler. ``close.webhook.description_change_skipped`` audit row with
    reason=merchant_not_found."""
    assert repo.count_total() == 0
    lead_description_state["description"] = _DESCRIPTION_WITH_FINANCIAL

    resp = _post_signed(client, _description_event())
    assert resp.status_code == 204, resp.text

    # No merchant created by this handler.
    assert repo.find_by_close_lead_id(_LEAD_ID) is None

    skipped = _audit_actions(audit, "close.webhook.description_change_skipped")
    matching = [r for r in skipped if r["details"].get("reason") == "merchant_not_found"]
    assert len(matching) == 1
    details = matching[0]["details"]
    assert details["close_lead_id"] == _LEAD_ID
    assert details["event_id"] == "ev_description_001"

    assert _audit_actions(audit, "close.webhook.stated_fields_auto_synced") == []


# ----------------------------------------------------------------------
# Case (e): lead.updated WITHOUT description in changed_fields → skip
# ----------------------------------------------------------------------


def test_description_handler_skips_when_description_not_in_changed_fields(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    lead_description_state: dict[str, str | None],
) -> None:
    """A lead.updated event where only display_name / status_id /
    custom-fields changed (description NOT in changed_fields) MUST NOT
    enter the description-handler branch. No description-handler audit
    rows fire — the contract under test.

    The sibling ``_handle_lead_updated`` path may still upsert via its
    own custom-field mapping; we don't assert merchant-row state here
    because that path is owned by a different handler and a different
    test surface (``test_webhooks_close_financial_block.py``).
    """
    _seed_merchant(repo)
    # Description body still carries a FINANCIAL block — but the event
    # changed_fields does not list ``description``. The description
    # handler must refuse to act regardless of what the body contains.
    lead_description_state["description"] = _DESCRIPTION_WITH_FINANCIAL
    event = _description_event(changed_fields=["display_name"])

    resp = _post_signed(client, event)
    assert resp.status_code == 204, resp.text

    # No description-handler audit rows of any kind — this is the only
    # contract this test owns.
    assert _audit_actions(audit, "close.webhook.stated_fields_auto_synced") == []
    assert _audit_actions(audit, "close.webhook.description_change_no_op") == []
    assert _audit_actions(audit, "close.webhook.description_change_skipped") == []
