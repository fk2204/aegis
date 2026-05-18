"""GET /audit/deal/{deal_id} route tests (mp Phase 2).

The route reads from Supabase directly via ``get_supabase()`` — we don't
need a live DB, just a fake client that records calls and returns
canned rows so the response shape, filtering, and 404 behavior can be
asserted.

What this covers:
- 401 when no bearer token is supplied (router-level auth).
- 404 when the underlying document doesn't exist.
- 200 + empty arrays for a deal that exists but has no decisions /
  disclosures / audit_log / analyses (the route must NOT 404 just
  because sub-tables are empty).
- 200 + populated arrays when sub-tables have rows.
- Validation against DealAuditView — extra columns from sub-tables
  must not break the response (forward-compat per ``_ReadModel`` config).
- 503 when the documents lookup explodes (graceful degradation: a sub-
  query failure returns an empty array, but the deal-existence check is
  the gate, so its failure surfaces as 503).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api import routes as routes_pkg
from aegis.api.app import create_app
from aegis.api.deps import reset_dependency_caches
from aegis.api.routes import audit as audit_route

AUTH = {"Authorization": "Bearer test-token-not-real"}


# ---------------------------------------------------------------------------
# Supabase fake — tailored to what audit.py's helpers actually call.
# ---------------------------------------------------------------------------


class _FakeQuery:
    """Chainable query stub returning canned rows by table name."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def select(self, *_args: Any) -> _FakeQuery:
        return self

    def eq(self, *_args: Any) -> _FakeQuery:
        return self

    def order(self, *_args: Any, **_kw: Any) -> _FakeQuery:
        return self

    def limit(self, *_args: Any) -> _FakeQuery:
        return self

    def execute(self) -> Any:
        return type("R", (), {"data": list(self._rows)})()


class _FakeTable:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def select(self, *args: Any) -> _FakeQuery:
        return _FakeQuery(self.rows).select(*args)


class _ExplodingTable:
    """Raises on .execute() — for the 503 path."""

    def select(self, *_args: Any) -> _ExplodingTable:
        return self

    def eq(self, *_args: Any) -> _ExplodingTable:
        return self

    def order(self, *_args: Any, **_kw: Any) -> _ExplodingTable:
        return self

    def limit(self, *_args: Any) -> _ExplodingTable:
        return self

    def execute(self) -> Any:
        raise RuntimeError("simulated supabase outage")


class _FakeSupabase:
    def __init__(self, *, by_table: dict[str, list[dict[str, Any]]]) -> None:
        self._by_table = by_table
        self._explode: set[str] = set()

    def explode_on(self, table: str) -> None:
        self._explode.add(table)

    def table(self, name: str) -> Any:
        if name in self._explode:
            return _ExplodingTable()
        return _FakeTable(self._by_table.get(name, []))


@pytest.fixture
def fake_supabase(monkeypatch: pytest.MonkeyPatch) -> _FakeSupabase:
    """Install a fake supabase client for the duration of one test."""
    fake = _FakeSupabase(by_table={})
    monkeypatch.setattr(audit_route, "get_supabase", lambda: fake)
    return fake


@pytest.fixture
def client(fake_supabase: _FakeSupabase) -> Iterator[TestClient]:
    """A TestClient that does not depend on Supabase being reachable.

    The app's lifespan hits load_matrix() etc. but never touches Supabase
    on its own; the audit route reads it lazily via get_supabase() which
    we've already monkeypatched on the module."""
    reset_dependency_caches()
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_dependency_caches()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_audit_route_requires_bearer(client: TestClient) -> None:
    resp = client.get(f"/audit/deal/{uuid4()}")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 404 path
# ---------------------------------------------------------------------------


def test_returns_404_when_deal_does_not_exist(
    client: TestClient, fake_supabase: _FakeSupabase
) -> None:
    # No documents table rows for the requested id.
    resp = client.get(f"/audit/deal/{uuid4()}", headers=AUTH)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "deal_not_found"


# ---------------------------------------------------------------------------
# 200 with empty sub-tables
# ---------------------------------------------------------------------------


def test_returns_empty_arrays_when_deal_has_no_history(
    client: TestClient, fake_supabase: _FakeSupabase
) -> None:
    deal_id = uuid4()
    fake_supabase._by_table["documents"] = [{"id": str(deal_id)}]
    # decisions / disclosures / audit_log / analyses — all empty.
    resp = client.get(f"/audit/deal/{deal_id}", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deal_id"] == str(deal_id)
    assert body["decisions"] == []
    assert body["disclosures"] == []
    assert body["audit_log"] == []
    assert body["analyses"] == []


# ---------------------------------------------------------------------------
# 200 with rows
# ---------------------------------------------------------------------------


def _decision_row(deal_id: UUID) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "deal_id": str(deal_id),
        "decided_at": datetime(2026, 5, 14, 10, 30, tzinfo=UTC).isoformat(),
        "decided_by": "filip",
        "decision": "approve",
        "decision_reason_codes": [],
        "score": "72.50",
        "score_factors": {"revenue": 25, "balance": 15},
        "analysis_id": str(uuid4()),
        "contributing_transaction_uuids": [],
        "bank_statement_pdf_sha256": "a" * 64,
        "state_code": "CA",
        "cfdl_tier": 1,
        "disclosure_template_path": "docs/compliance/states/CA/03_disclosure_template.j2",
        "disclosure_template_sha256": "b" * 64,
        "disclosure_pdf_sha256": "c" * 64,
        "apr_calculated": "32.4500",
        "apr_method": "reg_z_1026_22",
        "ofac_cache_timestamp": datetime(2026, 5, 14, tzinfo=UTC).isoformat(),
        "ofac_cache_sha256": "d" * 64,
        "aegis_version": "2.0.0",
        "rule_pack_version": "2026.05.18",
        "backfill_quality": None,
        # Forward-compat: a column added in a later migration must not
        # break this endpoint. _ReadModel uses extra='ignore'.
        "future_column_added_in_migration_999": "noise",
    }


def _disclosure_row(deal_id: UUID, decision_id: UUID) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "deal_id": str(deal_id),
        "decision_id": str(decision_id),
        "state_code": "CA",
        "template_path": "docs/compliance/states/CA/03_disclosure_template.j2",
        "template_sha256": "b" * 64,
        "disclosure_type": "sb1235",
        "rendered_pdf_path": "uploads/disclosures/x.pdf",
        "rendered_pdf_sha256": "e" * 64,
        "delivered_at": datetime(2026, 5, 14, 11, 0, tzinfo=UTC).isoformat(),
        "delivery_method": "email",
        "merchant_signature_at": None,
        "merchant_signature_ip": None,
        "merchant_signature_hash": None,
        "created_at": datetime(2026, 5, 14, 10, 45, tzinfo=UTC).isoformat(),
    }


def _audit_row(deal_id: UUID) -> dict[str, Any]:
    return {
        "actor": "api",
        "action": "decision.approve",
        "subject_type": "deal",
        "subject_id": str(deal_id),
        "details": {"decision_id": str(uuid4())},
        "created_at": datetime(2026, 5, 14, 10, 30, tzinfo=UTC).isoformat(),
    }


def _analysis_row(deal_id: UUID) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "document_id": str(deal_id),
        "statement_period_start": "2026-04-01",
        "statement_period_end": "2026-04-30",
        "created_at": datetime(2026, 5, 14, 10, 0, tzinfo=UTC).isoformat(),
    }


def test_returns_full_audit_trail_when_present(
    client: TestClient, fake_supabase: _FakeSupabase
) -> None:
    deal_id = uuid4()
    decision = _decision_row(deal_id)
    disclosure = _disclosure_row(deal_id, UUID(decision["id"]))
    audit = _audit_row(deal_id)
    analysis = _analysis_row(deal_id)

    fake_supabase._by_table["documents"] = [{"id": str(deal_id)}]
    fake_supabase._by_table["decisions"] = [decision]
    fake_supabase._by_table["disclosures"] = [disclosure]
    fake_supabase._by_table["audit_log"] = [audit]
    fake_supabase._by_table["analyses"] = [analysis]

    resp = client.get(f"/audit/deal/{deal_id}", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert len(body["decisions"]) == 1
    d = body["decisions"][0]
    assert d["decision"] == "approve"
    assert d["state_code"] == "CA"
    assert d["cfdl_tier"] == 1
    # The forward-compat noise column is silently dropped.
    assert "future_column_added_in_migration_999" not in d

    assert len(body["disclosures"]) == 1
    assert body["disclosures"][0]["state_code"] == "CA"

    assert len(body["audit_log"]) == 1
    assert body["audit_log"][0]["action"] == "decision.approve"

    assert len(body["analyses"]) == 1
    assert body["analyses"][0]["document_id"] == str(deal_id)


# ---------------------------------------------------------------------------
# 503 on doc-lookup failure
# ---------------------------------------------------------------------------


def test_returns_503_when_documents_lookup_fails(
    client: TestClient, fake_supabase: _FakeSupabase
) -> None:
    fake_supabase.explode_on("documents")
    resp = client.get(f"/audit/deal/{uuid4()}", headers=AUTH)
    assert resp.status_code == 503
    assert resp.json()["detail"] == "audit_db_unavailable"


# ---------------------------------------------------------------------------
# Graceful degradation: sub-query failure -> empty array, not 500
# ---------------------------------------------------------------------------


def test_sub_query_failure_degrades_to_empty_array(
    client: TestClient, fake_supabase: _FakeSupabase
) -> None:
    deal_id = uuid4()
    fake_supabase._by_table["documents"] = [{"id": str(deal_id)}]
    fake_supabase.explode_on("decisions")
    fake_supabase._by_table["disclosures"] = []
    fake_supabase._by_table["audit_log"] = []
    fake_supabase._by_table["analyses"] = []

    resp = client.get(f"/audit/deal/{deal_id}", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # decisions sub-query failed → degraded to empty; the page still
    # serves so the operator sees what IS available.
    assert body["decisions"] == []
    assert body["disclosures"] == []


# ---------------------------------------------------------------------------
# Smoke: the router is actually mounted on the app
# ---------------------------------------------------------------------------


def test_audit_router_is_mounted() -> None:
    assert audit_route.router in routes_pkg.ALL_ROUTERS
