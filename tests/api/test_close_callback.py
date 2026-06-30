"""Tests for the Close → AEGIS callback router (/api/close-callback/*).

Coverage focus:

* Auth: bearer required (constant-time compare against CLOSE_CALLBACK_TOKEN).
* Boot-guard fail-closed: unset token -> every endpoint returns 503.
* Per-endpoint happy paths for read merchant, read deal, upload trigger,
  sync trigger.
* Audit: every request writes a close_callback.* row with actor="close_callback"
  and the endpoint + close_lead_id in details.
* Rate limit: 60 req/min per IP -> 61st request from same IP returns 429.
* 404 hygiene: unknown close_lead_id -> 404, no information leak.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.auth import _reset_close_callback_warning_latch
from aegis.api.deps import (
    get_audit,
    get_close_client,
    get_decision_snapshot,
    get_merchant_repository,
    get_repository,
    reset_dependency_caches,
)
from aegis.api.routes.close_callback import reset_rate_limiter_for_tests
from aegis.audit import InMemoryAuditLog
from aegis.close.client import CloseClient
from aegis.close.field_map import CLOSE_FIELD_IDS
from aegis.compliance.snapshot import (
    DecisionPayload,
    InMemoryDecisionSnapshot,
)
from aegis.config import get_settings
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.storage import DocumentRow, InMemoryDocumentRepository

_BEARER = "test-close-callback-token-do-not-rotate"
_LEAD_ID = "lead_close_callback_test"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _lead_payload(close_lead_id: str = _LEAD_ID) -> dict[str, Any]:
    """Canned Close Lead GET response used by push_decision_to_close."""
    return {
        "id": close_lead_id,
        "display_name": "Acme Inc.",
        f"custom.{CLOSE_FIELD_IDS['aegis_applicant_id']}": "",
        f"custom.{CLOSE_FIELD_IDS['aegis_score']}": None,
        f"custom.{CLOSE_FIELD_IDS['aegis_recommendation']}": "",
        f"custom.{CLOSE_FIELD_IDS['ofac_status']}": "",
    }


@pytest.fixture(autouse=True)
def _reset_rate_limit_and_warnings() -> Iterator[None]:
    """Wipe rate-limit counters + warn latches before every test so
    cases don't interact through module-level state.

    Also force-clears the ``get_settings`` lru_cache at teardown:
    ``_build_client`` mutates env vars via monkeypatch + cache_clear,
    monkeypatch reverts the env at end-of-test but the cached
    ``Settings`` object still holds the test values. Without the
    teardown cache_clear, the next test (often outside this file —
    e.g. ``test_routes.py``) inherits stale settings whose
    ``api_bearer_token`` no longer matches conftest's ``test-token-not-real``,
    producing spurious 401s. See ``.claude/rules/testing.md``.
    """
    reset_rate_limiter_for_tests()
    _reset_close_callback_warning_latch()
    yield
    reset_rate_limiter_for_tests()
    _reset_close_callback_warning_latch()
    get_settings.cache_clear()


@pytest.fixture()
def repo() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture()
def docs() -> InMemoryDocumentRepository:
    return InMemoryDocumentRepository()


@pytest.fixture()
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture()
def snapshot() -> InMemoryDecisionSnapshot:
    return InMemoryDecisionSnapshot()


@pytest.fixture()
def merchant(repo: InMemoryMerchantRepository) -> MerchantRow:
    """Pre-seeded merchant with close_lead_id matching _LEAD_ID."""
    m = MerchantRow(
        business_name="Acme Inc",
        owner_name="Jane Roe",
        state="CA",
        industry_naics="722511",
        requested_amount=Decimal("50000.00"),
        close_lead_id=_LEAD_ID,
    )
    repo.upsert(m)
    return m


@pytest.fixture()
def captured_patches() -> list[dict[str, Any]]:
    """List the stub_close_client appends every PUT/PATCH body to.

    The /sync endpoint test reads this to verify the request body
    contains exactly the five expected ``custom.{CLOSE_FIELD_IDS[...]}``
    keys and nothing else — the no-funder-in-payload invariant the
    operator wants to see verified at the wire level, not just at the
    source level.
    """
    return []


@pytest.fixture()
def stub_close_client(
    monkeypatch: pytest.MonkeyPatch,
    captured_patches: list[dict[str, Any]],
) -> CloseClient:
    """Stub CloseClient returning the canned lead payload on GET and
    capturing any PUT/PATCH body for later assertion."""
    monkeypatch.setenv("CLOSE_API_KEY", "api_test")
    monkeypatch.setenv("CLOSE_API_BASE", "https://api.close.example")
    get_settings.cache_clear()

    # update_lead_custom_fields ships a PUT (Close's lead update is
    # PUT-shaped despite being semantically partial). Capture both PUT
    # and PATCH so the test remains correct if Close's verb changes.
    def transport(request: httpx.Request) -> httpx.Response:
        if request.method in ("PUT", "PATCH"):
            try:
                body = json.loads(request.content)
            except json.JSONDecodeError:
                body = {}
            captured_patches.append(body)
            return httpx.Response(200, json={"id": _LEAD_ID, "updated": True})
        return httpx.Response(200, json=_lead_payload())

    return CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport)))


def _build_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    repo: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
    snapshot: InMemoryDecisionSnapshot,
    stub_close_client: CloseClient,
    token: str | None = _BEARER,
) -> TestClient:
    """Build the FastAPI TestClient with the requested token config.

    ``token=None`` -> CLOSE_CALLBACK_TOKEN is unset, exercising the
    503 fail-closed boot-guard contract.
    """
    if token is None:
        monkeypatch.delenv("CLOSE_CALLBACK_TOKEN", raising=False)
    else:
        monkeypatch.setenv("CLOSE_CALLBACK_TOKEN", token)
    # Tests don't exercise the operator-API bearer surface; setting it
    # keeps the boot guard quiet.
    monkeypatch.setenv("API_BEARER_TOKEN", "op-token")
    get_settings.cache_clear()

    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: repo
    app.dependency_overrides[get_repository] = lambda: docs
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_decision_snapshot] = lambda: snapshot
    app.dependency_overrides[get_close_client] = lambda: stub_close_client
    return TestClient(app)


def _auth_headers(token: str | None = _BEARER) -> dict[str, str]:
    """Bearer auth headers. ``token=None`` returns no Authorization header
    so the test can exercise the missing-header 401 path."""
    if token is None:
        return {}
    return {"authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Auth — bearer required
# ---------------------------------------------------------------------------


def test_token_unset_returns_503(
    monkeypatch: pytest.MonkeyPatch,
    repo: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
    snapshot: InMemoryDecisionSnapshot,
    stub_close_client: CloseClient,
    merchant: MerchantRow,
) -> None:
    """Fail-closed contract: route refuses to validate without a
    configured token (operator misconfiguration shouldn't read as
    a key mismatch)."""
    client = _build_client(
        monkeypatch,
        repo=repo,
        docs=docs,
        audit=audit,
        snapshot=snapshot,
        stub_close_client=stub_close_client,
        token=None,
    )
    resp = client.get(
        f"/api/close-callback/merchant/{_LEAD_ID}",
        headers=_auth_headers(_BEARER),
    )
    assert resp.status_code == 503
    assert "CLOSE_CALLBACK_TOKEN" in resp.text


def test_bearer_missing_returns_401(
    monkeypatch: pytest.MonkeyPatch,
    repo: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
    snapshot: InMemoryDecisionSnapshot,
    stub_close_client: CloseClient,
    merchant: MerchantRow,
) -> None:
    client = _build_client(
        monkeypatch,
        repo=repo,
        docs=docs,
        audit=audit,
        snapshot=snapshot,
        stub_close_client=stub_close_client,
    )
    resp = client.get(f"/api/close-callback/merchant/{_LEAD_ID}")
    assert resp.status_code == 401


def test_bearer_wrong_returns_401(
    monkeypatch: pytest.MonkeyPatch,
    repo: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
    snapshot: InMemoryDecisionSnapshot,
    stub_close_client: CloseClient,
    merchant: MerchantRow,
) -> None:
    client = _build_client(
        monkeypatch,
        repo=repo,
        docs=docs,
        audit=audit,
        snapshot=snapshot,
        stub_close_client=stub_close_client,
    )
    resp = client.get(
        f"/api/close-callback/merchant/{_LEAD_ID}",
        headers=_auth_headers("wrong-token"),
    )
    assert resp.status_code == 401


def test_bearer_correct_passes(
    monkeypatch: pytest.MonkeyPatch,
    repo: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
    snapshot: InMemoryDecisionSnapshot,
    stub_close_client: CloseClient,
    merchant: MerchantRow,
) -> None:
    client = _build_client(
        monkeypatch,
        repo=repo,
        docs=docs,
        audit=audit,
        snapshot=snapshot,
        stub_close_client=stub_close_client,
    )
    resp = client.get(
        f"/api/close-callback/merchant/{_LEAD_ID}",
        headers=_auth_headers(),
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Endpoints — happy path
# ---------------------------------------------------------------------------


def test_read_merchant_returns_payload_and_audits(
    monkeypatch: pytest.MonkeyPatch,
    repo: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
    snapshot: InMemoryDecisionSnapshot,
    stub_close_client: CloseClient,
    merchant: MerchantRow,
) -> None:
    client = _build_client(
        monkeypatch,
        repo=repo,
        docs=docs,
        audit=audit,
        snapshot=snapshot,
        stub_close_client=stub_close_client,
    )
    resp = client.get(
        f"/api/close-callback/merchant/{_LEAD_ID}",
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["merchant_id"] == str(merchant.id)
    assert body["business_name"] == "Acme Inc"
    assert body["state"] == "CA"
    assert body["close_lead_id"] == _LEAD_ID

    # Audit row: actor=close_callback, action=close_callback.merchant.read.
    rows = [e for e in audit.entries if e["actor"] == "close_callback"]
    assert any(
        r["action"] == "close_callback.merchant.read"
        and r["details"]["close_lead_id"] == _LEAD_ID
        and r["details"]["endpoint"] == f"/api/close-callback/merchant/{_LEAD_ID}"
        for r in rows
    )


def test_read_deal_without_documents_returns_no_document(
    monkeypatch: pytest.MonkeyPatch,
    repo: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
    snapshot: InMemoryDecisionSnapshot,
    stub_close_client: CloseClient,
    merchant: MerchantRow,
) -> None:
    client = _build_client(
        monkeypatch,
        repo=repo,
        docs=docs,
        audit=audit,
        snapshot=snapshot,
        stub_close_client=stub_close_client,
    )
    resp = client.get(
        f"/api/close-callback/deal/{_LEAD_ID}",
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["parse_status"] == "no_document"
    assert "fraud_score" not in body
    assert body["has_analysis"] is False


def test_upload_triggers_orchestration_enqueue(
    monkeypatch: pytest.MonkeyPatch,
    repo: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
    snapshot: InMemoryDecisionSnapshot,
    stub_close_client: CloseClient,
    merchant: MerchantRow,
) -> None:
    client = _build_client(
        monkeypatch,
        repo=repo,
        docs=docs,
        audit=audit,
        snapshot=snapshot,
        stub_close_client=stub_close_client,
    )
    resp = client.post(
        f"/api/close-callback/merchant/{_LEAD_ID}/upload",
        headers=_auth_headers(),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enqueued"] is True

    # Audit row records the enqueue.
    actions = [r["action"] for r in audit.entries if r["actor"] == "close_callback"]
    assert "close_callback.upload.enqueued" in actions


def test_sync_with_decision_patches_exactly_five_close_custom_fields(
    monkeypatch: pytest.MonkeyPatch,
    repo: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
    snapshot: InMemoryDecisionSnapshot,
    stub_close_client: CloseClient,
    captured_patches: list[dict[str, Any]],
    merchant: MerchantRow,
) -> None:
    """Happy-path sync: seed a document + a stored decision, fire the
    /sync endpoint, and confirm the wire-level update body contains
    EXACTLY the five expected ``custom.{CLOSE_FIELD_IDS[...]}`` keys
    — four business fields (applicant_id, score, recommendation,
    ofac_status) plus aegis_last_synced. No funder-related keys.

    This is the assertion the operator asked for explicitly: prove at
    the outbound boundary that the sync touches only Close-managed
    custom fields. Verifying via the captured request body (not the
    source code) means a future change that adds new keys to the
    handler also has to update this test.
    """
    # 1. Seed a document so deal_ids isn't empty.
    doc = DocumentRow(
        id=uuid4(),
        file_hash="a" * 64,
        byte_size=1024,
        original_filename="stmt.pdf",
        merchant_id=merchant.id,
        parse_status="proceed",
        fraud_score=42,
        all_flags=[],
        uploaded_at=datetime.now(UTC),
    )
    docs._docs[doc.id] = doc

    # 2. Seed a decision against that document.
    payload = DecisionPayload(
        deal_id=doc.id,
        decided_by="test_operator",
        decision="approve",
        decision_reason_codes=[],
        score=Decimal("85.00"),
        state_code="CA",
        cfdl_tier=2,
        aegis_version="test",
        rule_pack_version="test",
        ofac_cache_timestamp=datetime.now(UTC),  # -> derive_ofac_status -> "Clear"
    )
    snapshot.write(payload, audit=audit)

    # 3. Fire /sync.
    client = _build_client(
        monkeypatch,
        repo=repo,
        docs=docs,
        audit=audit,
        snapshot=snapshot,
        stub_close_client=stub_close_client,
    )
    resp = client.post(
        f"/api/close-callback/merchant/{_LEAD_ID}/sync",
        headers=_auth_headers(),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["patched"] is True

    # 4. Verify the wire-level update body.
    assert len(captured_patches) == 1, (
        f"expected exactly one PUT/PATCH; got {len(captured_patches)}"
    )
    patch_body = captured_patches[0]

    expected_keys = {
        f"custom.{CLOSE_FIELD_IDS['aegis_applicant_id']}",
        f"custom.{CLOSE_FIELD_IDS['aegis_score']}",
        f"custom.{CLOSE_FIELD_IDS['aegis_recommendation']}",
        f"custom.{CLOSE_FIELD_IDS['ofac_status']}",
        f"custom.{CLOSE_FIELD_IDS['aegis_last_synced']}",
    }
    assert set(patch_body.keys()) == expected_keys, (
        "Update body contained unexpected keys. Expected exactly the "
        "5 Close custom fields. Got: "
        f"{sorted(patch_body.keys())}"
    )

    # 5. Defense: explicitly assert no funder-related key sneaks in.
    for key in patch_body:
        assert "funder" not in key.lower(), f"funder-related field in update body: {key}"


def test_sync_without_decision_returns_400(
    monkeypatch: pytest.MonkeyPatch,
    repo: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
    snapshot: InMemoryDecisionSnapshot,
    stub_close_client: CloseClient,
    merchant: MerchantRow,
) -> None:
    """Sync requires a stored decision. Without one, surface as 400 not
    500 — the caller asked for something we can't fulfill."""
    client = _build_client(
        monkeypatch,
        repo=repo,
        docs=docs,
        audit=audit,
        snapshot=snapshot,
        stub_close_client=stub_close_client,
    )
    resp = client.post(
        f"/api/close-callback/merchant/{_LEAD_ID}/sync",
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 404 hygiene
# ---------------------------------------------------------------------------


def test_unknown_close_lead_id_returns_404(
    monkeypatch: pytest.MonkeyPatch,
    repo: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
    snapshot: InMemoryDecisionSnapshot,
    stub_close_client: CloseClient,
) -> None:
    """Lookup miss => 404 with generic detail. Don't leak whether the
    lead id existed but didn't map vs truly doesn't exist."""
    client = _build_client(
        monkeypatch,
        repo=repo,
        docs=docs,
        audit=audit,
        snapshot=snapshot,
        stub_close_client=stub_close_client,
    )
    resp = client.get(
        "/api/close-callback/merchant/lead_nope",
        headers=_auth_headers(),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------


def test_rate_limit_triggers_after_60_requests_per_window(
    monkeypatch: pytest.MonkeyPatch,
    repo: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
    snapshot: InMemoryDecisionSnapshot,
    stub_close_client: CloseClient,
    merchant: MerchantRow,
) -> None:
    """61st request from the same IP within 60s -> 429."""
    client = _build_client(
        monkeypatch,
        repo=repo,
        docs=docs,
        audit=audit,
        snapshot=snapshot,
        stub_close_client=stub_close_client,
    )
    headers = _auth_headers()
    last_status: int | None = None
    for _ in range(60):
        resp = client.get(f"/api/close-callback/merchant/{_LEAD_ID}", headers=headers)
        last_status = resp.status_code
    assert last_status == 200, "first 60 requests should pass"

    overflow = client.get(f"/api/close-callback/merchant/{_LEAD_ID}", headers=headers)
    assert overflow.status_code == 429
    assert "Rate limit" in overflow.text
