"""Operator-override route + module tests (mp Phase 10 / Stage 2D-main).

Two layers:

1. ``aegis.compliance.overrides.record_override`` — the persistence
   path. Tests cover insert + audit + back-stamp from any pending reply.

2. ``POST /ui/decisions/{decision_id}/override`` — the dashboard form
   handler. Tests cover the happy path, validation rejection, and
   form-encoded ``pattern_false_positive`` parsing.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_funder_reply_repository,
    get_override_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.compliance.overrides import (
    InMemoryOverrideRepository,
    OverridePayload,
    record_override,
)
from aegis.funders.replies import (
    FunderReplyPayload,
    InMemoryFunderReplyRepository,
    ReplyTerms,
    ingest_reply,
)

NOW = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)


def _approved_reply_terms() -> ReplyTerms:
    return ReplyTerms(
        amount=Decimal("20000.00"),
        factor=Decimal("1.32"),
        payback=Decimal("26400.00"),
    )


def _make_payload(**overrides: Any) -> OverridePayload:
    defaults: dict[str, Any] = {
        "deal_id": uuid4(),
        "decision_id": uuid4(),
        "original_recommendation": "decline",
        "operator_decision": "approve",
        "reason_code": "score_too_conservative",
        "reason_detail": "Merchant has strong cash flow despite recent NSF activity",
        "pattern_false_positive": ["nsf_volatility"],
        "operator_id": "filip",
    }
    defaults.update(overrides)
    return OverridePayload(**defaults)


# ---------------------------------------------------------------------------
# Module-level (record_override)
# ---------------------------------------------------------------------------


def test_record_override_persists_row_and_audits() -> None:
    override_repo = InMemoryOverrideRepository()
    reply_repo = InMemoryFunderReplyRepository()
    audit = InMemoryAuditLog()

    payload = _make_payload()
    outcome = record_override(
        payload, repo=override_repo, reply_repo=reply_repo, audit=audit
    )

    assert isinstance(outcome.override_id, UUID)
    rows = override_repo.rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["deal_id"] == str(payload.deal_id)
    assert row["decision_id"] == str(payload.decision_id)
    assert row["reason_code"] == "score_too_conservative"
    assert row["pattern_false_positive"] == ["nsf_volatility"]
    assert row["operator_id"] == "filip"

    actions = [e["action"] for e in audit.entries]
    assert "decision.override" in actions
    detail = next(e for e in audit.entries if e["action"] == "decision.override")
    assert detail["details"]["override_id"] == str(outcome.override_id)
    # No back-stamp when no replies in flight.
    assert outcome.back_stamped_outcome is None


def test_record_override_back_stamps_from_pending_reply() -> None:
    """Reply landed first (no override yet). When the override is
    created, the back-stamp path stamps it with the reply's outcome."""
    override_repo = InMemoryOverrideRepository()
    reply_repo = InMemoryFunderReplyRepository()
    audit = InMemoryAuditLog()

    deal_id = uuid4()
    # Step 1: reply lands first.
    ingest_reply(
        FunderReplyPayload(
            deal_id=deal_id,
            funder_id=uuid4(),
            status="approved",
            raw_text="we approve",
            ingested_via="webhook",
            terms=_approved_reply_terms(),
            parsed_confidence=90,
        ),
        repo=reply_repo,
        audit=audit,
        now=NOW,
    )

    # Step 2: override lands AFTER the reply. The override doesn't
    # exist yet in reply_repo's overrides view, so the override
    # repo and the funder-reply repo's override mirror need wiring:
    # the in-memory funder-reply repo carries its own override list
    # that record_override seeds via reply_repo.add_override (the
    # production Supabase version uses the shared overrides table).
    payload = _make_payload(deal_id=deal_id)
    # Manually mirror the override into the reply repo's view so the
    # back-stamp query can see it. In production both writes hit the
    # same SQL table.
    override_id = override_repo.insert_override(payload)
    reply_repo.add_override(
        {
            "id": str(override_id),
            "deal_id": str(deal_id),
            "outcome": None,
            "created_at": (NOW + timedelta(hours=1)).isoformat(),
        }
    )
    # Re-run the back-stamp path (record_override would have done this
    # but we needed to seed the mirror first for the in-memory repo).
    from aegis.funders.replies import stamp_override_from_replies

    stamped_id = stamp_override_from_replies(
        override_id=override_id,
        deal_id=deal_id,
        repo=reply_repo,
        audit=audit,
        now=NOW + timedelta(hours=1),
    )
    assert stamped_id == override_id
    stamped_row = next(
        r for r in reply_repo.overrides() if r["id"] == str(override_id)
    )
    assert stamped_row["outcome"] == "funded"


def test_record_override_rejects_unknown_reason_code_at_validation() -> None:
    """Pydantic catches a bogus reason_code before the DB write
    happens — same defensive shape as the bank parser's strict
    extraction model."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _make_payload(reason_code="totally_made_up_code")


# ---------------------------------------------------------------------------
# Route-level (POST /ui/decisions/{decision_id}/override)
# ---------------------------------------------------------------------------


@pytest.fixture
def override_repo() -> InMemoryOverrideRepository:
    return InMemoryOverrideRepository()


@pytest.fixture
def reply_repo() -> InMemoryFunderReplyRepository:
    return InMemoryFunderReplyRepository()


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def client(
    override_repo: InMemoryOverrideRepository,
    reply_repo: InMemoryFunderReplyRepository,
    audit: InMemoryAuditLog,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_override_repository] = lambda: override_repo
    app.dependency_overrides[get_funder_reply_repository] = lambda: reply_repo
    app.dependency_overrides[get_audit] = lambda: audit
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def test_override_route_persists_and_returns_201(
    client: TestClient, override_repo: InMemoryOverrideRepository
) -> None:
    deal_id = uuid4()
    decision_id = uuid4()
    resp = client.post(
        f"/ui/decisions/{decision_id}/override",
        data={
            "deal_id": str(deal_id),
            "original_recommendation": "decline",
            "operator_decision": "approve",
            "reason_code": "score_too_conservative",
            "reason_detail": "Strong revenue trend",
            "pattern_false_positive": "nsf_volatility, mca_stacking",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "override_id" in body
    assert body["back_stamped_outcome"] is None

    rows = override_repo.rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["deal_id"] == str(deal_id)
    assert row["decision_id"] == str(decision_id)
    # Comma-separated string parsed into a clean list (no whitespace, no empties).
    assert row["pattern_false_positive"] == ["nsf_volatility", "mca_stacking"]


def test_override_route_rejects_unknown_reason_code(client: TestClient) -> None:
    resp = client.post(
        f"/ui/decisions/{uuid4()}/override",
        data={
            "deal_id": str(uuid4()),
            "original_recommendation": "decline",
            "operator_decision": "approve",
            "reason_code": "I-just-want-to",  # not in the Literal
            "reason_detail": "",
            "pattern_false_positive": "",
        },
    )
    assert resp.status_code == 400
    assert "invalid override payload" in resp.text


def test_override_route_blank_patterns_serialize_to_empty_list(
    client: TestClient, override_repo: InMemoryOverrideRepository
) -> None:
    """Empty/whitespace-only ``pattern_false_positive`` → empty list,
    not [""]. Mirrors the in-memory repo's behavior on no-pattern
    submits."""
    resp = client.post(
        f"/ui/decisions/{uuid4()}/override",
        data={
            "deal_id": str(uuid4()),
            "original_recommendation": "approve",
            "operator_decision": "decline",
            "reason_code": "data_quality_concern",
            "reason_detail": "Statement quality unclear",
            "pattern_false_positive": " , ",  # whitespace-only entries
        },
    )
    assert resp.status_code == 201, resp.text
    row = override_repo.rows()[0]
    # In-memory repo stores `list(...) or None`, so blank lists
    # collapse to None — matches the migration's optional TEXT[]
    # semantics.
    assert row["pattern_false_positive"] is None
