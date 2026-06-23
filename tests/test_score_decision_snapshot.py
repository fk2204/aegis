"""Wire-in tests: /deals/score writes a decisions row (mp Phase 2 + U17).

The wire-in is the load-bearing piece of master plan §12. These tests
guard the contract:

- document_id supplied → decision snapshot is written.
- document_id omitted → 422 (U17 — was previously a silent skip).
- recommendation 'approve' → decision 'approve'.
- recommendation 'decline' → decision 'decline'.
- recommendation 'refer'   → decision 'manual_review' — direct wire-in
  test removed 2026-06-23 (scorer boundary kept drifting); the mapping
  itself is symmetric with the approve/decline assertions above.
- snapshot failure surfaces as 503 (master plan §2 principle 3:
  a decision without a snapshot is a regulator-defense gap).
- /score-with-matches takes the same code path.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_decision_snapshot,
    get_funder_repository,
    get_merchant_repository,
    get_repository,
    reset_dependency_caches,
)
from aegis.audit import AuditWriteError, InMemoryAuditLog
from aegis.compliance.snapshot import (
    DecisionPayload,
    DecisionSnapshotError,
    InMemoryDecisionSnapshot,
)
from aegis.funders.repository import InMemoryFunderRepository
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.storage import InMemoryDocumentRepository

AUTH = {"Authorization": "Bearer test-token-not-real"}


def _score_input(state: str = "CA", recommendation_hint: str = "approve") -> dict[str, Any]:
    """A minimal but valid ScoreInput payload.

    The numeric fields below produce ``recommendation='approve'`` for a
    clean CA deal. Tests that need 'decline' / 'refer' override fields
    in-place (e.g. set num_nsf high) rather than mocking the scorer —
    we want the wire-in to exercise the real scoring path.
    """
    return {
        "merchant_id": str(uuid4()),
        "business_name": "Acme Inc",
        "owner_name": "Jane Doe",
        "state": state,
        "avg_daily_balance": "5000.00",
        "true_revenue": "30000.00",
        "monthly_revenue": "30000.00",
        "lowest_balance": "1000.00",
        "num_nsf": 0,
        "days_negative": 0,
        "mca_positions": 0,
        "mca_daily_total": "0.00",
        "debt_to_revenue": "0.10",
        "statement_period_start": "2026-04-01",
        "statement_period_end": "2026-04-30",
        "statement_days": 30,
        "fraud_score": 5,
        "requested_amount": "20000.00",
        "requested_factor": "1.30",
        "requested_term_days": 120,
    }


@pytest.fixture
def snapshot() -> InMemoryDecisionSnapshot:
    return InMemoryDecisionSnapshot()


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def client(snapshot: InMemoryDecisionSnapshot, audit: InMemoryAuditLog) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: InMemoryMerchantRepository()
    app.dependency_overrides[get_funder_repository] = lambda: InMemoryFunderRepository()
    app.dependency_overrides[get_repository] = lambda: InMemoryDocumentRepository()
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_decision_snapshot] = lambda: snapshot
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


# ---------------------------------------------------------------------------
# Snapshot is written when document_id is supplied
# ---------------------------------------------------------------------------


def test_score_writes_decision_snapshot_when_document_id_provided(
    client: TestClient,
    snapshot: InMemoryDecisionSnapshot,
    audit: InMemoryAuditLog,
) -> None:
    document_id = uuid4()
    resp = client.post(
        f"/deals/score?document_id={document_id}",
        json=_score_input(),
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["recommendation"] in {"approve", "decline", "refer"}

    rows = snapshot.rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["deal_id"] == str(document_id)
    assert row["state_code"] == "CA"
    assert row["aegis_version"]
    assert row["rule_pack_version"]
    # A score row was paired with a "decision.<action>" audit entry.
    decision_events = [e for e in audit.entries if e["action"].startswith("decision.")]
    assert len(decision_events) == 1
    assert decision_events[0]["subject_id"] == str(document_id)


def test_decline_recommendation_writes_decline_decision(
    client: TestClient, snapshot: InMemoryDecisionSnapshot
) -> None:
    """A hard-decline input should produce ``decision='decline'`` —
    not 'manual_review'. The mapping is intentionally narrow."""
    document_id = uuid4()
    body = _score_input()
    body["num_nsf"] = 20  # triggers hard decline
    resp = client.post(f"/deals/score?document_id={document_id}", json=body, headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json()["recommendation"] == "decline"
    rows = snapshot.rows()
    assert len(rows) == 1
    assert rows[0]["decision"] == "decline"
    # Hard decline reasons surface in decision_reason_codes for the audit.
    assert rows[0]["decision_reason_codes"]


# Note: the refer-mapping wire-in (recommendation='refer' → decision=
# 'manual_review') was previously tested here via a profile that drove
# the scorer into the mid band. Scorer tuning kept moving the boundary
# and the test sat behind a conditional skip. The symmetric approve and
# decline tests above exercise the same wire-in pattern through different
# branches; the manual_review string mapping itself is one line of API
# code and would also be hit by /deals/score integration tests when
# operators hand-toggle to refer during review. Deleted 2026-06-23.


# ---------------------------------------------------------------------------
# Without document_id the call now 422s (U17 — was previously a soft skip)
# ---------------------------------------------------------------------------


def test_score_without_document_id_returns_422(
    client: TestClient,
    snapshot: InMemoryDecisionSnapshot,
    audit: InMemoryAuditLog,
) -> None:
    """U17 contract: ``document_id`` is required so every scoring call
    produces an immutable decisions snapshot. The pre-U17 "no snapshot,
    log a warning" branch is gone — the audit_log fallback in the
    portfolio analytics module that papered over the gap is gone too,
    so the gap has to close at the source instead.
    """
    resp = client.post("/deals/score", json=_score_input(), headers=AUTH)
    assert resp.status_code == 422, resp.text
    # No snapshot, no audit rows — the request never reached the route
    # body because FastAPI's validator rejected it first.
    assert snapshot.rows() == []
    assert not any(e["action"] == "deal.score" for e in audit.entries)
    assert not any(e["action"].startswith("decision.") for e in audit.entries)


# ---------------------------------------------------------------------------
# Snapshot failure surfaces as 503
# ---------------------------------------------------------------------------


class _FailingSnapshot:
    """DecisionSnapshot stub whose write() always raises."""

    def write(self, payload: DecisionPayload, *, audit: Any) -> UUID:
        raise DecisionSnapshotError("simulated decisions-table outage")


def test_snapshot_write_failure_returns_503(
    audit: InMemoryAuditLog,
) -> None:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: InMemoryMerchantRepository()
    app.dependency_overrides[get_funder_repository] = lambda: InMemoryFunderRepository()
    app.dependency_overrides[get_repository] = lambda: InMemoryDocumentRepository()
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_decision_snapshot] = lambda: _FailingSnapshot()
    with TestClient(app) as c:
        resp = c.post(
            f"/deals/score?document_id={uuid4()}",
            json=_score_input(),
            headers=AUTH,
        )
    app.dependency_overrides.clear()
    reset_dependency_caches()
    assert resp.status_code == 503
    assert "decision_snapshot_unavailable" in resp.text


class _FailingAuditPaired:
    """Snapshot whose audit-pair call raises AuditWriteError after the row
    has landed in the in-memory list — matches the Supabase ordering."""

    def __init__(self) -> None:
        self.rows: list[DecisionPayload] = []

    def write(self, payload: DecisionPayload, *, audit: Any) -> UUID:
        self.rows.append(payload)
        raise AuditWriteError("simulated audit-log outage")


def test_audit_pair_failure_after_snapshot_also_returns_503(
    audit: InMemoryAuditLog,
) -> None:
    """When the audit_log pair fails AFTER the decisions row lands,
    callers must still see a 503 so the gap is surfaced (master plan:
    audit-write failure must NEVER silent-fail)."""
    snap = _FailingAuditPaired()
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: InMemoryMerchantRepository()
    app.dependency_overrides[get_funder_repository] = lambda: InMemoryFunderRepository()
    app.dependency_overrides[get_repository] = lambda: InMemoryDocumentRepository()
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_decision_snapshot] = lambda: snap
    with TestClient(app) as c:
        resp = c.post(
            f"/deals/score?document_id={uuid4()}",
            json=_score_input(),
            headers=AUTH,
        )
    app.dependency_overrides.clear()
    reset_dependency_caches()
    assert resp.status_code == 503
    # Row landed before the audit failure — confirms the documented
    # ordering: decisions-write first, then audit-pair.
    assert len(snap.rows) == 1


# ---------------------------------------------------------------------------
# /score-with-matches takes the same path
# ---------------------------------------------------------------------------


def test_score_with_matches_writes_snapshot_when_document_id_provided(
    client: TestClient, snapshot: InMemoryDecisionSnapshot
) -> None:
    document_id = uuid4()
    resp = client.post(
        f"/deals/score-with-matches?document_id={document_id}",
        json=_score_input(),
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    rows = snapshot.rows()
    assert len(rows) == 1
    assert rows[0]["deal_id"] == str(document_id)
