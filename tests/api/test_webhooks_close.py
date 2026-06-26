"""Tests for POST /webhooks/close.

Coverage per the operator's spec (commit authorizing step 4):

* HMAC: valid 204, bad sig 401, stale timestamp 401, missing headers 401.
* Idempotency: same event delivered twice -> exactly one merchant row,
  exactly one update audit beyond the receipt audits.
* Out-of-order: status flipping between two states -> the wrong-status
  event does no work.
* Event filtering: opportunity.updated without status_id in
  changed_fields -> 204 + filtered_out receipt, no work; status_id
  changed but to the WRONG status -> same.
* Audit: every reception writes close.webhook.received with event_id,
  status_id, changed_fields, decision.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable, Iterator
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
from aegis.close.field_map import CLOSE_FIELD_IDS
from aegis.config import get_settings
from aegis.merchants.repository import InMemoryMerchantRepository

# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


# Hex-encoded secret to mirror what Close returns from a subscription POST.
_TEST_SECRET_HEX = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"  # noqa: S105 — test stub, not a real secret
_TRIGGER_STATUS_ID = "stat_1YZuVqdPWC8HLjWWvnXqL3NBJUPSjw3upy9mdBYXRqI"
_OTHER_STATUS_ID = "stat_otherSalesStageDoesNotTrigger"


def _sign(timestamp: str, body: bytes, secret_hex: str = _TEST_SECRET_HEX) -> str:
    """Compute the close-sig-hash value Close would send."""
    secret = bytes.fromhex(secret_hex)
    return hmac.new(secret, timestamp.encode("utf-8") + body, hashlib.sha256).hexdigest()


def _lead_payload(close_lead_id: str = "lead_abc") -> dict[str, Any]:
    """Canned Close Lead GET response with the Aegis-relevant custom
    fields populated. Returned by the stub CloseClient when get_lead is
    called."""
    return {
        "id": close_lead_id,
        "display_name": "Acme Inc.",
        f"custom.{CLOSE_FIELD_IDS['legal_name']}": "Acme Holdings LLC",
        f"custom.{CLOSE_FIELD_IDS['dba_name']}": "Acme",
        f"custom.{CLOSE_FIELD_IDS['ein']}": "12-3456789",
        f"custom.{CLOSE_FIELD_IDS['owner_name']}": "Jane Roe",
        f"custom.{CLOSE_FIELD_IDS['state']}": "CA",
        f"custom.{CLOSE_FIELD_IDS['industry']}": "Restaurant / Food Service",
        f"custom.{CLOSE_FIELD_IDS['naics_code']}": "722511",
        f"custom.{CLOSE_FIELD_IDS['time_in_business_months']}": 36,
        f"custom.{CLOSE_FIELD_IDS['fico_range']}": "650-699",
        f"custom.{CLOSE_FIELD_IDS['requested_amount']}": "$50,000.00",
        f"custom.{CLOSE_FIELD_IDS['entity_type_a']}": "LLC",
        f"custom.{CLOSE_FIELD_IDS['entity_type_b']}": "LLC",
    }


def _opportunity_event(
    *,
    lead_id: str = "lead_abc",
    new_status_id: str = _TRIGGER_STATUS_ID,
    changed_fields: list[str] | None = None,
    action: str = "updated",
) -> dict[str, Any]:
    """Build a Close webhook event payload mimicking opportunity.updated."""
    return {
        "event": {
            "id": "ev_test_001",
            "date_created": "2026-05-21T10:00:00+00:00",
            "action": action,
            "object_type": "opportunity",
            "object_id": "oppo_xyz",
            "lead_id": lead_id,
            "organization_id": "orga_abc",
            "user_id": "user_op",
            "changed_fields": (
                changed_fields
                if changed_fields is not None
                else ["status_id", "status_label", "date_status_changed"]
            ),
            "previous_data": {"status_id": _OTHER_STATUS_ID},
            "data": {"status_id": new_status_id},
            "meta": {},
            "request_id": "req_001",
        },
        "subscription_id": "whsub_test_001",
    }


@pytest.fixture()
def repo() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture()
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


def _path_aware_transport_factory(
    *,
    has_opportunity: bool = True,
    has_pdf: bool = True,
    lead_payload_override: dict[str, Any] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Build an httpx transport that routes by path so the lead.updated
    gate (opportunity + PDF check, 2026-06-20) has realistic inputs.

    Defaults yield ``has_opportunity=True`` + ``has_pdf=True`` so the
    pre-gate tests still pass without modification — the merchant
    creation branch fires the way it always did. New gate tests pass
    ``has_opportunity=False`` / ``has_pdf=False`` to exercise the skip
    paths.
    """

    def transport(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        # /api/v1/lead/{lead_id}/ — canned lead payload
        if path.startswith("/api/v1/lead/"):
            return httpx.Response(200, json=lead_payload_override or _lead_payload())
        # /api/v1/opportunity/ — gate read
        if path.startswith("/api/v1/opportunity/"):
            opps = [{"id": "oppo_stub", "lead_id": "lead_abc"}] if has_opportunity else []
            return httpx.Response(200, json={"data": opps, "has_more": False})
        # /api/v1/activity/note/ — gate's PDF check walks here first
        if path.startswith("/api/v1/activity/note/"):
            if has_pdf:
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": "acti_stub",
                                "_type": "Note",
                                "attachments": [
                                    {
                                        "id": "att_stub",
                                        "filename": "stmt.pdf",
                                        "content_type": "application/pdf",
                                        "url": "https://app.close.com/go/file/persisted/stub",
                                    }
                                ],
                            }
                        ],
                        "has_more": False,
                    },
                )
            return httpx.Response(200, json={"data": [], "has_more": False})
        if path.startswith("/api/v1/activity/email/"):
            return httpx.Response(200, json={"data": [], "has_more": False})
        # Fallback: the lead payload (back-compat with the pre-gate stub
        # for any path the test isn't explicitly mapping).
        return httpx.Response(200, json=lead_payload_override or _lead_payload())

    return transport


@pytest.fixture()
def stub_close_client(
    monkeypatch: pytest.MonkeyPatch,
) -> CloseClient:
    """A CloseClient whose underlying httpx transport returns the canned
    Lead payload for ``/lead/`` paths, a one-opportunity stub for
    ``/opportunity/``, and a single PDF attachment for
    ``/activity/note/``. The lead.updated gate (opportunity + PDF
    presence, 2026-06-20) sees realistic positive inputs by default so
    every pre-gate test still goes down the merchant-create branch."""
    monkeypatch.setenv("CLOSE_API_KEY", "api_test")
    monkeypatch.setenv("CLOSE_API_BASE", "https://api.close.example")
    monkeypatch.setenv("CLOSE_WEBHOOK_SECRET", _TEST_SECRET_HEX)
    monkeypatch.setenv("CLOSE_DOCS_IN_PRE_UW_STATUS_ID", _TRIGGER_STATUS_ID)
    get_settings.cache_clear()

    transport = _path_aware_transport_factory()
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


def _post_signed(
    client: TestClient,
    body_dict: dict[str, Any],
    *,
    timestamp: str | None = None,
    secret_hex: str = _TEST_SECRET_HEX,
    override_sig: str | None = None,
    omit_sig_header: bool = False,
    omit_ts_header: bool = False,
) -> Any:
    """Helper: post a JSON body with valid close-sig-hash + close-sig-timestamp."""
    if timestamp is None:
        timestamp = str(int(time.time()))
    raw = json.dumps(body_dict).encode("utf-8")
    sig = override_sig if override_sig is not None else _sign(timestamp, raw, secret_hex)
    headers: dict[str, str] = {"content-type": "application/json"}
    if not omit_sig_header:
        headers["close-sig-hash"] = sig
    if not omit_ts_header:
        headers["close-sig-timestamp"] = timestamp
    return client.post("/webhooks/close", content=raw, headers=headers)


# ----------------------------------------------------------------------
# HMAC + freshness
# ----------------------------------------------------------------------


def test_valid_signature_returns_204(
    client: TestClient,
    repo: InMemoryMerchantRepository,
) -> None:
    resp = _post_signed(client, _opportunity_event())
    assert resp.status_code == 204, resp.text
    # Side effect: merchant was created.
    assert repo.count_total() == 1


def test_bad_signature_returns_401(client: TestClient) -> None:
    resp = _post_signed(client, _opportunity_event(), override_sig="0" * 64)
    assert resp.status_code == 401
    assert resp.json() == {"detail": "unauthorized"}


def test_stale_timestamp_returns_401(client: TestClient) -> None:
    stale_ts = str(int(time.time()) - 10 * 60)  # 10 minutes ago
    resp = _post_signed(client, _opportunity_event(), timestamp=stale_ts)
    assert resp.status_code == 401
    assert resp.json() == {"detail": "unauthorized"}


def test_missing_sig_header_returns_401(client: TestClient) -> None:
    resp = _post_signed(client, _opportunity_event(), omit_sig_header=True)
    assert resp.status_code == 401


def test_missing_timestamp_header_returns_401(client: TestClient) -> None:
    resp = _post_signed(client, _opportunity_event(), omit_ts_header=True)
    assert resp.status_code == 401


def test_secret_not_configured_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLOSE_WEBHOOK_SECRET", raising=False)
    get_settings.cache_clear()
    resp = _post_signed(client, _opportunity_event())
    assert resp.status_code == 503


# ----------------------------------------------------------------------
# Event filtering
# ----------------------------------------------------------------------


def test_filter_wrong_action_returns_204_with_filtered_audit(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """opportunity.created (not .updated) → filtered_out."""
    event = _opportunity_event(action="created")
    resp = _post_signed(client, event)
    assert resp.status_code == 204
    assert repo.count_total() == 0
    receipts = [e for e in audit.entries if e["action"] == "close.webhook.received"]
    assert len(receipts) == 1
    assert receipts[0]["details"]["decision"] == "filtered_out"


def test_filter_status_id_not_in_changed_fields_returns_204(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """An opportunity.updated whose changed_fields does NOT include
    status_id (e.g. just value or confidence changed) is a no-op."""
    event = _opportunity_event(changed_fields=["value", "confidence"])
    resp = _post_signed(client, event)
    assert resp.status_code == 204
    assert repo.count_total() == 0
    receipts = [e for e in audit.entries if e["action"] == "close.webhook.received"]
    assert len(receipts) == 1
    assert receipts[0]["details"]["decision"] == "filtered_out"
    assert receipts[0]["details"]["changed_fields"] == ["value", "confidence"]


def test_filter_wrong_new_status_id_returns_204(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """status_id IS in changed_fields, but it transitioned to a status
    other than Docs In — Pre-UW (e.g. moved to Lender Shopping) — no work."""
    event = _opportunity_event(new_status_id=_OTHER_STATUS_ID)
    resp = _post_signed(client, event)
    assert resp.status_code == 204
    assert repo.count_total() == 0
    receipts = [e for e in audit.entries if e["action"] == "close.webhook.received"]
    assert receipts[0]["details"]["decision"] == "filtered_out"


# ----------------------------------------------------------------------
# Audit reception details
# ----------------------------------------------------------------------


def test_receipt_audit_carries_event_id_status_changed_fields(
    client: TestClient, audit: InMemoryAuditLog
) -> None:
    """The close.webhook.received row carries the structured fields
    needed for compliance traceability."""
    resp = _post_signed(client, _opportunity_event())
    assert resp.status_code == 204
    receipts = [e for e in audit.entries if e["action"] == "close.webhook.received"]
    assert len(receipts) == 1
    d = receipts[0]["details"]
    assert d["event_id"] == "ev_test_001"
    assert d["subscription_id"] == "whsub_test_001"
    assert d["object_type"] == "opportunity"
    assert d["action"] == "updated"
    assert d["lead_id"] == "lead_abc"
    assert d["opp_id"] == "oppo_xyz"
    assert "status_id" in d["changed_fields"]
    assert d["new_status_id"] == _TRIGGER_STATUS_ID
    assert d["trigger_status_id"] == _TRIGGER_STATUS_ID
    assert d["decision"] == "processed"


def test_receipt_audit_written_even_when_filtered(
    client: TestClient, audit: InMemoryAuditLog
) -> None:
    """Guarantee #1 — every reception leaves a receipt, regardless of
    whether downstream work happens."""
    event = _opportunity_event(changed_fields=["value"])
    _post_signed(client, event)
    receipts = [e for e in audit.entries if e["action"] == "close.webhook.received"]
    assert len(receipts) == 1


# ----------------------------------------------------------------------
# Idempotency — same event delivered twice
# ----------------------------------------------------------------------


def test_idempotent_redelivery_creates_exactly_one_merchant(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """Two identical webhook deliveries → one merchants row, one creation
    audit row, two receipt audits (one per delivery), zero update
    audits (no diff between identical events)."""
    event = _opportunity_event()

    resp1 = _post_signed(client, event)
    resp2 = _post_signed(client, event)
    assert resp1.status_code == 204
    assert resp2.status_code == 204

    # Exactly one merchant row by close_lead_id.
    assert repo.count_total() == 1
    found = repo.find_by_close_lead_id("lead_abc")
    assert found is not None

    # Audit counts.
    receipts = [e for e in audit.entries if e["action"] == "close.webhook.received"]
    creates = [e for e in audit.entries if e["action"] == "close.merchant.created"]
    updates = [e for e in audit.entries if e["action"] == "close.merchant.updated"]
    assert len(receipts) == 2, "every reception audits"
    assert len(creates) == 1, "first delivery creates"
    assert len(updates) == 0, "redelivery produces no update (no diff)"


# ----------------------------------------------------------------------
# Out-of-order: status flipping back and forth
# ----------------------------------------------------------------------


def test_out_of_order_status_flip_no_duplicate_work(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """Sequence: Lead enters Docs In — Pre-UW (event 1), moves to a
    different status (event 2 — filtered), then a late retry of event 1
    arrives (event 3). The handler must:
      * process event 1 → merchant created
      * filter event 2 → no work
      * see event 3 as a redelivery of an already-processed transition
        → no second create. The merchants table stays at one row; the
        only AEGIS-side cost is the receipt audit on every delivery.
    """
    event_in = _opportunity_event()
    event_out = _opportunity_event(new_status_id=_OTHER_STATUS_ID)
    event_in_retry = _opportunity_event()  # same event id; redelivery

    _post_signed(client, event_in)
    _post_signed(client, event_out)
    _post_signed(client, event_in_retry)

    assert repo.count_total() == 1
    creates = [e for e in audit.entries if e["action"] == "close.merchant.created"]
    updates = [e for e in audit.entries if e["action"] == "close.merchant.updated"]
    receipts = [e for e in audit.entries if e["action"] == "close.webhook.received"]
    assert len(creates) == 1
    assert len(updates) == 0  # no diff on retry; no spurious update
    assert len(receipts) == 3  # one per delivery, including the filtered one


# ----------------------------------------------------------------------
# Diff-only update behavior
# ----------------------------------------------------------------------


def test_changed_lead_payload_produces_update_with_diff_keys(
    monkeypatch: pytest.MonkeyPatch,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """If Close updates a Lead field between two webhook firings, the
    second delivery results in a close.merchant.updated audit row whose
    details.changed_keys names only the actual diff."""
    monkeypatch.setenv("CLOSE_API_KEY", "api_test")
    monkeypatch.setenv("CLOSE_API_BASE", "https://api.close.example")
    monkeypatch.setenv("CLOSE_WEBHOOK_SECRET", _TEST_SECRET_HEX)
    monkeypatch.setenv("CLOSE_DOCS_IN_PRE_UW_STATUS_ID", _TRIGGER_STATUS_ID)
    get_settings.cache_clear()

    # First delivery: FICO Range 650-699 (credit_score=650).
    first_payload = _lead_payload()
    # Second delivery: FICO Range 700+ (credit_score=700).
    second_payload = _lead_payload()
    second_payload[f"custom.{CLOSE_FIELD_IDS['fico_range']}"] = "700+"

    call_count = {"n": 0}

    def transport(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        body = first_payload if call_count["n"] == 1 else second_payload
        return httpx.Response(200, json=body)

    close_client = CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport)))

    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: repo
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_close_client] = lambda: close_client
    with TestClient(app) as tc:
        event = _opportunity_event()
        _post_signed(tc, event)
        _post_signed(tc, event)

    creates = [e for e in audit.entries if e["action"] == "close.merchant.created"]
    updates = [e for e in audit.entries if e["action"] == "close.merchant.updated"]
    assert len(creates) == 1
    assert len(updates) == 1, "diff should produce exactly one update"
    assert "credit_score" in updates[0]["details"]["changed_keys"]
    reset_dependency_caches()


# ----------------------------------------------------------------------
# Bad payloads
# ----------------------------------------------------------------------


def test_malformed_json_returns_400(client: TestClient) -> None:
    timestamp = str(int(time.time()))
    raw = b"{not json"
    sig = _sign(timestamp, raw)
    resp = client.post(
        "/webhooks/close",
        content=raw,
        headers={
            "content-type": "application/json",
            "close-sig-hash": sig,
            "close-sig-timestamp": timestamp,
        },
    )
    assert resp.status_code == 400


def test_payload_missing_event_object_returns_400(client: TestClient) -> None:
    resp = _post_signed(client, {"subscription_id": "whsub_x"})
    assert resp.status_code == 400


def test_matching_event_missing_lead_id_returns_400(client: TestClient) -> None:
    event = _opportunity_event()
    event["event"]["lead_id"] = None
    resp = _post_signed(client, event)
    assert resp.status_code == 400


# ----------------------------------------------------------------------
# Attachment orchestration enqueue (Feature 2, chunk 3)
# ----------------------------------------------------------------------


def test_webhook_enqueues_orchestration_after_merchant_upsert(
    client: TestClient,
    audit: InMemoryAuditLog,
) -> None:
    """After the merchant upsert lands, the route fires-and-forgets the
    ``process_close_attachments`` arq job. With no arq pool wired (test
    harness), the job is captured in ``pending_close_orchestration_jobs``.
    """
    resp = _post_signed(client, _opportunity_event(lead_id="lead_xyz"))
    assert resp.status_code == 204, resp.text

    pending = getattr(
        client.app.state,  # type: ignore[attr-defined]
        "pending_close_orchestration_jobs",
        [],
    )
    assert pending == [
        {
            "close_lead_id": "lead_xyz",
            "trigger": "webhook",
            "actor_email": None,
            "override_cap": False,
            "ignore_pin": False,
        }
    ]

    actions = [e["action"] for e in audit.entries]
    assert "close.orchestration.enqueued" in actions
    enq = next(e for e in audit.entries if e["action"] == "close.orchestration.enqueued")
    assert enq["details"]["close_lead_id"] == "lead_xyz"
    assert enq["details"]["trigger"] == "webhook"
    # subject_id must be the merchant uuid that was just upserted
    assert enq["subject_id"] is not None


def test_webhook_enqueue_failure_does_not_5xx(
    client: TestClient,
    audit: InMemoryAuditLog,
) -> None:
    """A Redis blip (or any other enqueue raise) audits
    close.orchestration.enqueue_failed and returns 204 — the webhook
    must not 5xx because Close's 72h retry + idempotent merchant
    upsert make self-healing the right policy."""

    class _BoomPool:
        async def enqueue_job(self, *_a: object, **_kw: object) -> None:
            raise RuntimeError("redis_blip")

    client.app.state.arq_pool = _BoomPool()  # type: ignore[attr-defined]
    try:
        resp = _post_signed(client, _opportunity_event(lead_id="lead_xyz"))
        assert resp.status_code == 204, resp.text
    finally:
        client.app.state.arq_pool = None  # type: ignore[attr-defined]

    actions = [e["action"] for e in audit.entries]
    assert "close.orchestration.enqueue_failed" in actions
    assert "close.orchestration.enqueued" not in actions
    fail = next(e for e in audit.entries if e["action"] == "close.orchestration.enqueue_failed")
    assert fail["details"]["error"] == "RuntimeError"
    assert fail["details"]["close_lead_id"] == "lead_xyz"


def test_webhook_filtered_event_does_not_enqueue(
    client: TestClient,
    audit: InMemoryAuditLog,
) -> None:
    """If the status_id didn't change (event filtered out), we must
    NOT enqueue the orchestrator — there's no merchant upsert and
    nothing to follow up on."""
    event = _opportunity_event(lead_id="lead_filtered")
    event["event"]["data"]["status_id"] = _OTHER_STATUS_ID
    resp = _post_signed(client, event)
    assert resp.status_code == 204

    pending = getattr(
        client.app.state,  # type: ignore[attr-defined]
        "pending_close_orchestration_jobs",
        [],
    )
    assert pending == []
    actions = [e["action"] for e in audit.entries]
    assert "close.orchestration.enqueued" not in actions
    assert "close.orchestration.enqueue_failed" not in actions


# ----------------------------------------------------------------------
# lead.updated path — added 2026-06-18 after the Close subscription
# change registered lead.updated events so attachments uploaded BEFORE
# the opportunity reaches Pre-UW still get pulled in.
# ----------------------------------------------------------------------


def _lead_updated_event(*, lead_id: str = "lead_abc") -> dict[str, Any]:
    """Build a Close webhook event payload mimicking lead.updated."""
    return {
        "event": {
            "id": "ev_lead_001",
            "date_created": "2026-06-18T10:00:00+00:00",
            "action": "updated",
            "object_type": "lead",
            "object_id": lead_id,
            "lead_id": lead_id,
            "organization_id": "orga_abc",
            "user_id": "user_op",
            "changed_fields": ["display_name"],
            "previous_data": {"display_name": "Old Name"},
            "data": {"display_name": "New Name"},
            "meta": {},
            "request_id": "req_lead_001",
        },
        "subscription_id": "whsub_test_001",
    }


def test_lead_updated_with_existing_merchant_refreshes_and_enqueues(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """lead.updated for a merchant that already exists in AEGIS:
    no new merchant row, context refreshed best-effort, attachment
    orchestration enqueued with the lead.updated trigger label."""
    from uuid import uuid4

    from aegis.merchants.models import MerchantRow

    existing = MerchantRow(
        id=uuid4(),
        close_lead_id="lead_already_known",
        business_name="Existing Co",
    )
    repo.upsert(existing)

    resp = _post_signed(client, _lead_updated_event(lead_id="lead_already_known"))
    assert resp.status_code == 204, resp.text

    # Merchant count is unchanged — no second row created.
    merchants_in_repo = [
        row for row in repo._by_id.values() if row.close_lead_id == "lead_already_known"
    ]
    assert len(merchants_in_repo) == 1
    assert merchants_in_repo[0].id == existing.id

    # The attachment orchestration was enqueued with the lead-updated trigger.
    pending = getattr(
        client.app.state,  # type: ignore[attr-defined]
        "pending_close_orchestration_jobs",
        [],
    )
    assert pending == [
        {
            "close_lead_id": "lead_already_known",
            "trigger": "webhook_lead_updated",
            "actor_email": None,
            "override_cap": False,
            "ignore_pin": False,
        }
    ]

    actions = [e["action"] for e in audit.entries]
    assert "close.webhook.received" in actions
    assert "close.orchestration.enqueued" in actions
    # No new merchant — created/updated audit rows should not fire on a
    # lead.updated whose payload didn't change anything we map.
    assert "close.merchant.created" not in actions


def test_lead_updated_with_no_merchant_creates_one(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """lead.updated for a lead AEGIS has never seen: a merchant gets
    created from the lead payload — same flow as the Pre-UW trigger
    builds a merchant from a Lead it hadn't yet seen."""
    lead_id = "lead_brand_new_for_aegis"

    assert repo.find_by_close_lead_id(lead_id) is None

    resp = _post_signed(client, _lead_updated_event(lead_id=lead_id))
    assert resp.status_code == 204, resp.text

    created = repo.find_by_close_lead_id(lead_id)
    assert created is not None
    # close_opportunity_id MUST stay None — lead.updated events don't
    # carry an opportunity. The Pre-UW trigger fills it later.
    assert created.close_opportunity_id is None

    actions = [e["action"] for e in audit.entries]
    assert "close.merchant.created" in actions
    pending = getattr(
        client.app.state,  # type: ignore[attr-defined]
        "pending_close_orchestration_jobs",
        [],
    )
    assert len(pending) == 1
    assert pending[0]["trigger"] == "webhook_lead_updated"


def test_lead_updated_idempotent_redelivery_dedups_attachment_path(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """Close redelivers the SAME lead.updated event: enqueue still fires
    (idempotency is enforced downstream by ``process_close_attachments``
    via ``documents.file_hash`` SHA-256 dedup). The receipt audit row
    lands twice (one per delivery), but no duplicate merchant row is
    created."""
    from uuid import uuid4

    from aegis.merchants.models import MerchantRow

    existing = MerchantRow(
        id=uuid4(),
        close_lead_id="lead_redelivered",
        business_name="Redelivered Inc",
    )
    repo.upsert(existing)

    event = _lead_updated_event(lead_id="lead_redelivered")
    resp1 = _post_signed(client, event)
    resp2 = _post_signed(client, event)
    assert resp1.status_code == 204
    assert resp2.status_code == 204

    pending = getattr(
        client.app.state,  # type: ignore[attr-defined]
        "pending_close_orchestration_jobs",
        [],
    )
    # Two enqueues — the SHA-256 dedup inside process_close_attachments
    # is what makes this safe, not the route itself.
    assert len(pending) == 2
    assert all(p["trigger"] == "webhook_lead_updated" for p in pending)

    # Only one merchant row keyed on this lead_id.
    matched = [r for r in repo._by_id.values() if r.close_lead_id == "lead_redelivered"]
    assert len(matched) == 1


# ----------------------------------------------------------------------
# Empty / invalid custom-field guards on _lead_to_merchant_fields
# (regression for the 2026-06-19 syslog flood — leads with no operator-
# set State or Owner Name 500'd the webhook because the field_map
# coerced absent custom fields to "" and Pydantic rejected the
# string_too_short MerchantRow.)
# ----------------------------------------------------------------------


_KEEP_DEFAULT = object()


def _lead_payload_with(
    *,
    state: Any = _KEEP_DEFAULT,
    owner_name: Any = _KEEP_DEFAULT,
    close_lead_id: str = "lead_empty",
) -> dict[str, Any]:
    """Canned Lead payload with ``state`` / ``owner_name`` controlled
    independently. Pass ``None`` to drop the custom-field key entirely
    (mimics Close returning a payload with the field unset). Pass an
    explicit string to override that field's value. Omit the argument
    to keep the canned default.
    """
    payload = _lead_payload(close_lead_id=close_lead_id)
    if state is not _KEEP_DEFAULT:
        key = f"custom.{CLOSE_FIELD_IDS['state']}"
        if state is None:
            payload.pop(key, None)
        else:
            payload[key] = state
    if owner_name is not _KEEP_DEFAULT:
        key = f"custom.{CLOSE_FIELD_IDS['owner_name']}"
        if owner_name is None:
            payload.pop(key, None)
        else:
            payload[key] = owner_name
    return payload


def _build_client_with_payload(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
) -> TestClient:
    """One-off TestClient wired to a CloseClient that returns ``payload``
    on every Lead GET."""
    monkeypatch.setenv("CLOSE_API_KEY", "api_test")
    monkeypatch.setenv("CLOSE_API_BASE", "https://api.close.example")
    monkeypatch.setenv("CLOSE_WEBHOOK_SECRET", _TEST_SECRET_HEX)
    monkeypatch.setenv("CLOSE_DOCS_IN_PRE_UW_STATUS_ID", _TRIGGER_STATUS_ID)
    get_settings.cache_clear()

    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    close_client = CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport)))
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: repo
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_close_client] = lambda: close_client
    return TestClient(app)


def test_webhook_handles_lead_with_unset_state(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lead payload with no ``state`` custom field must NOT 500. The
    merchant lands with ``state=None``; downstream auto-state-detection
    fills it later via Close note or operator edit."""
    payload = _lead_payload_with(state=None, close_lead_id="lead_no_state")
    with _build_client_with_payload(repo, audit, monkeypatch, payload) as tc:
        resp = _post_signed(tc, _opportunity_event(lead_id="lead_no_state"))
    assert resp.status_code == 204, resp.text
    saved = repo.find_by_close_lead_id("lead_no_state")
    assert saved is not None
    assert saved.state is None
    assert saved.owner_name == "Jane Roe"
    reset_dependency_caches()


def test_webhook_handles_lead_with_empty_string_state(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit empty string (Close convention for cleared fields)
    behaves the same as an unset key — merchant gets ``state=None``."""
    payload = _lead_payload_with(state="", close_lead_id="lead_empty_state")
    with _build_client_with_payload(repo, audit, monkeypatch, payload) as tc:
        resp = _post_signed(tc, _opportunity_event(lead_id="lead_empty_state"))
    assert resp.status_code == 204, resp.text
    saved = repo.find_by_close_lead_id("lead_empty_state")
    assert saved is not None
    assert saved.state is None
    reset_dependency_caches()


def test_webhook_handles_lead_with_non_2letter_state(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A garbage state value (typo, full state name, numeric noise)
    must collapse to None rather than blow up Pydantic's max_length=2."""
    payload = _lead_payload_with(state="California", close_lead_id="lead_garbage_state")
    with _build_client_with_payload(repo, audit, monkeypatch, payload) as tc:
        resp = _post_signed(tc, _opportunity_event(lead_id="lead_garbage_state"))
    assert resp.status_code == 204, resp.text
    saved = repo.find_by_close_lead_id("lead_garbage_state")
    assert saved is not None
    assert saved.state is None
    reset_dependency_caches()


def test_webhook_handles_lead_with_unset_owner_name(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lead payload with no ``owner_name`` custom field must NOT 500.
    The merchant lands with ``owner_name=None`` (intake form fills it
    later)."""
    payload = _lead_payload_with(owner_name=None, close_lead_id="lead_no_owner")
    with _build_client_with_payload(repo, audit, monkeypatch, payload) as tc:
        resp = _post_signed(tc, _opportunity_event(lead_id="lead_no_owner"))
    assert resp.status_code == 204, resp.text
    saved = repo.find_by_close_lead_id("lead_no_owner")
    assert saved is not None
    assert saved.owner_name is None
    assert saved.state == "CA"
    reset_dependency_caches()


def test_webhook_handles_lead_with_empty_string_owner_name(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _lead_payload_with(owner_name="   ", close_lead_id="lead_blank_owner")
    with _build_client_with_payload(repo, audit, monkeypatch, payload) as tc:
        resp = _post_signed(tc, _opportunity_event(lead_id="lead_blank_owner"))
    assert resp.status_code == 204, resp.text
    saved = repo.find_by_close_lead_id("lead_blank_owner")
    assert saved is not None
    assert saved.owner_name is None
    reset_dependency_caches()


def test_webhook_handles_lead_with_both_unset(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The combination the syslog actually surfaced: both state AND
    owner_name unset together. Pre-fix this 500'd; merchant still has
    ``business_name`` from display_name so the row remains valid."""
    payload = _lead_payload_with(state=None, owner_name=None, close_lead_id="lead_both_unset")
    with _build_client_with_payload(repo, audit, monkeypatch, payload) as tc:
        resp = _post_signed(tc, _opportunity_event(lead_id="lead_both_unset"))
    assert resp.status_code == 204, resp.text
    saved = repo.find_by_close_lead_id("lead_both_unset")
    assert saved is not None
    assert saved.state is None
    assert saved.owner_name is None
    assert saved.business_name == "Acme Holdings LLC"
    reset_dependency_caches()


# ----------------------------------------------------------------------
# lead.updated new-merchant gate
# (2026-06-20 — Close lead.updated subscription was bulk-creating
# merchants for every lead change in the org, leaving 80% of the
# merchants table empty. Gate the new-merchant branch on opportunity
# presence AND PDF-attachment presence so AEGIS only mirrors leads
# that are actual deals with documents to score.)
# ----------------------------------------------------------------------


def _build_gate_client(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    monkeypatch: pytest.MonkeyPatch,
    *,
    has_opportunity: bool,
    has_pdf: bool,
) -> TestClient:
    """One-off TestClient with a CloseClient whose transport reports the
    requested opportunity / PDF presence to the lead.updated gate."""
    monkeypatch.setenv("CLOSE_API_KEY", "api_test")
    monkeypatch.setenv("CLOSE_API_BASE", "https://api.close.example")
    monkeypatch.setenv("CLOSE_WEBHOOK_SECRET", _TEST_SECRET_HEX)
    monkeypatch.setenv("CLOSE_DOCS_IN_PRE_UW_STATUS_ID", _TRIGGER_STATUS_ID)
    get_settings.cache_clear()
    transport = _path_aware_transport_factory(has_opportunity=has_opportunity, has_pdf=has_pdf)
    close_client = CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport)))
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: repo
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_close_client] = lambda: close_client
    return TestClient(app)


def test_lead_updated_gate_skips_when_no_opportunity(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cold prospect / no-opportunity lead → no merchant created, skip
    audit row written."""
    lead_id = "lead_no_opp"
    with _build_gate_client(repo, audit, monkeypatch, has_opportunity=False, has_pdf=True) as tc:
        resp = _post_signed(tc, _lead_updated_event(lead_id=lead_id))
    assert resp.status_code == 204, resp.text
    assert repo.find_by_close_lead_id(lead_id) is None
    actions = [e["action"] for e in audit.entries]
    assert "close.lead_update.skipped_no_opportunity" in actions
    assert "close.merchant.created" not in actions
    reset_dependency_caches()


def test_lead_updated_gate_skips_when_opportunity_but_no_pdfs(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lead has an opportunity but no PDFs yet → still skip merchant
    creation. The operator can attach a statement later; the next
    lead.updated event will then create the merchant."""
    lead_id = "lead_opp_no_pdfs"
    with _build_gate_client(repo, audit, monkeypatch, has_opportunity=True, has_pdf=False) as tc:
        resp = _post_signed(tc, _lead_updated_event(lead_id=lead_id))
    assert resp.status_code == 204, resp.text
    assert repo.find_by_close_lead_id(lead_id) is None
    actions = [e["action"] for e in audit.entries]
    assert "close.lead_update.skipped_no_pdfs" in actions
    assert "close.merchant.created" not in actions
    reset_dependency_caches()


def test_lead_updated_gate_creates_when_opportunity_and_pdf_both_present(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both gates pass → existing auto-create behavior fires unchanged."""
    lead_id = "lead_real_deal"
    with _build_gate_client(repo, audit, monkeypatch, has_opportunity=True, has_pdf=True) as tc:
        resp = _post_signed(tc, _lead_updated_event(lead_id=lead_id))
    assert resp.status_code == 204, resp.text
    created = repo.find_by_close_lead_id(lead_id)
    assert created is not None
    actions = [e["action"] for e in audit.entries]
    assert "close.merchant.created" in actions
    assert "close.lead_update.skipped_no_opportunity" not in actions
    assert "close.lead_update.skipped_no_pdfs" not in actions
    reset_dependency_caches()


def test_lead_updated_gate_does_not_block_refresh_on_existing_merchant(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate fires for NEW-merchant creation only. When the lead already
    has an AEGIS merchant, the context refresh + attachment
    orchestration still run even if the gate would have blocked a
    fresh create. Otherwise an opportunity-less existing merchant
    would stop getting context updates."""
    from uuid import uuid4

    from aegis.merchants.models import MerchantRow

    lead_id = "lead_existing_merchant"
    repo.upsert(
        MerchantRow(
            id=uuid4(),
            close_lead_id=lead_id,
            business_name="Existing Inc",
        )
    )

    with _build_gate_client(repo, audit, monkeypatch, has_opportunity=False, has_pdf=False) as tc:
        resp = _post_signed(tc, _lead_updated_event(lead_id=lead_id))
    assert resp.status_code == 204, resp.text
    assert repo.find_by_close_lead_id(lead_id) is not None

    actions = [e["action"] for e in audit.entries]
    # Skip-audit rows MUST NOT fire on the existing-merchant path
    # otherwise the audit trail would falsely report a "skipped" event.
    assert "close.lead_update.skipped_no_opportunity" not in actions
    assert "close.lead_update.skipped_no_pdfs" not in actions
    reset_dependency_caches()


def test_lead_updated_gate_fails_open_when_close_api_errors(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient Close API failure on either gate check defaults to
    ALLOW (fail-OPEN). The pre-gate auto-create behavior is the safer
    fallback when the gate can't actually verify the lead's state — a
    flap should never silently drop a real deal."""
    monkeypatch.setenv("CLOSE_API_KEY", "api_test")
    monkeypatch.setenv("CLOSE_API_BASE", "https://api.close.example")
    monkeypatch.setenv("CLOSE_WEBHOOK_SECRET", _TEST_SECRET_HEX)
    monkeypatch.setenv("CLOSE_DOCS_IN_PRE_UW_STATUS_ID", _TRIGGER_STATUS_ID)
    get_settings.cache_clear()

    def transport(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/api/v1/lead/"):
            return httpx.Response(200, json=_lead_payload())
        # Both gate paths 500 — exercises fail-OPEN.
        return httpx.Response(500, json={"error": "transient"})

    close_client = CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport)))
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: repo
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_close_client] = lambda: close_client

    lead_id = "lead_close_api_flap"
    monkeypatch.setattr("aegis.close.client.time.sleep", lambda _s: None)
    with TestClient(app) as tc:
        resp = _post_signed(tc, _lead_updated_event(lead_id=lead_id))
    assert resp.status_code == 204, resp.text
    # Fail-OPEN: merchant was created despite the gate not getting a
    # clean signal from Close.
    assert repo.find_by_close_lead_id(lead_id) is not None
    actions = [e["action"] for e in audit.entries]
    assert "close.merchant.created" in actions
    reset_dependency_caches()
