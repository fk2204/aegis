"""Disclosure render-event log tests (U16 — migration 042).

Covers ``compliance/render_events.py`` and the route wiring in
``api/routes/disclosures.py`` that U3 (commit 924d799) deferred.

  * Repository round-trip + status enum acceptance / rejection
  * Status filter (``list_by_status``) returns newest first
  * Happy-path render writes one ``ok`` render-event row + paired
    ``aegis_disclosure_render_event`` audit_log row
  * APR failure writes one ``apr_compute_failed`` render-event row +
    paired ``aegis_disclosure_render_event`` audit_log row, AND the
    pre-existing U3 ``aegis_apr_compute_failed`` audit_log row still
    fires (regression guard — render-event is additive, not a
    replacement)
  * PII canary on the render-event ``details`` JSONB

Scope note: AEGIS is internal pre-flight per
``.claude/rules/compliance.md``; this tests the internal render log,
NOT a regulator-facing surface.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_disclosure_render_event_repository,
    get_funder_repository,
    get_merchant_repository,
    get_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.compliance import disclosure_context
from aegis.compliance.apr import APRCalculationError
from aegis.compliance.render_events import (
    RENDER_EVENT_STATUS_APR_FAILED,
    RENDER_EVENT_STATUS_NEEDS_REVIEW,
    RENDER_EVENT_STATUS_OK,
    DisclosureRenderEventRecord,
    InMemoryDisclosureRenderEventRepository,
    record_disclosure_render_event,
)
from aegis.funders.repository import InMemoryFunderRepository
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.storage import InMemoryDocumentRepository

AUTH = {"Authorization": "Bearer test-token-not-real"}

# Deterministic merchant id used by the route-level tests so audit /
# render-event subject assertions are stable.
_FIXED_MERCHANT_ID = "22222222-2222-4222-8222-222222222222"

# PII canary strings — must not appear in render-event details.
_PII_BUSINESS = "Render Event Bakery LLC"
_PII_OWNER = "Render Event Owner PII Canary"


# ---------------------------------------------------------------------------
# Repository round-trip + status enum acceptance
# ---------------------------------------------------------------------------


def _common_kwargs() -> dict[str, Any]:
    return {
        "deal_id": UUID(_FIXED_MERCHANT_ID),
        "merchant_id": UUID(_FIXED_MERCHANT_ID),
        "state": "CA",
        "template_path": "compliance/templates/ca_sb1235.html.j2",
        "status_reason": None,
        "details": {"deal_id": _FIXED_MERCHANT_ID, "tier": 1},
        "recipient_email": None,
        "rendered_by": "api",
    }


def test_in_memory_repo_records_ok_row_with_expected_fields() -> None:
    """A successful record() call returns a populated record with the
    correct status, state, and details payload."""
    repo = InMemoryDisclosureRenderEventRepository()
    kwargs = _common_kwargs()
    rec = repo.record(status=RENDER_EVENT_STATUS_OK, **kwargs)

    assert isinstance(rec, DisclosureRenderEventRecord)
    assert rec.status == RENDER_EVENT_STATUS_OK
    assert rec.state == "CA"
    assert rec.template_path == "compliance/templates/ca_sb1235.html.j2"
    assert rec.deal_id == UUID(_FIXED_MERCHANT_ID)
    assert rec.merchant_id == UUID(_FIXED_MERCHANT_ID)
    assert rec.details == {"deal_id": _FIXED_MERCHANT_ID, "tier": 1}
    assert isinstance(rec.rendered_at, datetime)
    assert rec.rendered_at.tzinfo is not None
    assert len(repo.rows) == 1


def test_in_memory_repo_accepts_all_known_statuses() -> None:
    """``status`` is free-text on the DB but constrained on the Python
    side. Every named constant must be accepted."""
    repo = InMemoryDisclosureRenderEventRepository()
    for status in (
        RENDER_EVENT_STATUS_OK,
        RENDER_EVENT_STATUS_NEEDS_REVIEW,
        RENDER_EVENT_STATUS_APR_FAILED,
    ):
        rec = repo.record(status=status, **_common_kwargs())
        assert rec.status == status


def test_in_memory_repo_rejects_unknown_status() -> None:
    """A caller-side typo must fail loud rather than silently writing
    an unqueryable row."""
    repo = InMemoryDisclosureRenderEventRepository()
    with pytest.raises(ValueError, match="status must be one of"):
        repo.record(status="totally_made_up", **_common_kwargs())


def test_in_memory_repo_normalizes_lowercase_state() -> None:
    """State codes are normalized to USPS uppercase."""
    repo = InMemoryDisclosureRenderEventRepository()
    kwargs = _common_kwargs()
    kwargs["state"] = "ny"
    rec = repo.record(status=RENDER_EVENT_STATUS_OK, **kwargs)
    assert rec.state == "NY"


def test_in_memory_repo_accepts_null_state_and_template() -> None:
    """Both nullable because the route may catch APRDisclosureError
    before any state / template resolution."""
    repo = InMemoryDisclosureRenderEventRepository()
    kwargs = _common_kwargs()
    kwargs["state"] = None
    kwargs["template_path"] = None
    rec = repo.record(status=RENDER_EVENT_STATUS_APR_FAILED, **kwargs)
    assert rec.state is None
    assert rec.template_path is None


# ---------------------------------------------------------------------------
# Status filter
# ---------------------------------------------------------------------------


def test_list_by_status_returns_newest_first() -> None:
    """The triage queue reads needs_review events newest-first."""
    repo = InMemoryDisclosureRenderEventRepository()
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    for offset, status in enumerate(
        [
            RENDER_EVENT_STATUS_OK,
            RENDER_EVENT_STATUS_APR_FAILED,
            RENDER_EVENT_STATUS_APR_FAILED,
            RENDER_EVENT_STATUS_OK,
        ]
    ):
        repo.record(
            status=status,
            rendered_at=base + timedelta(minutes=offset),
            **_common_kwargs(),
        )

    failed = repo.list_by_status(status=RENDER_EVENT_STATUS_APR_FAILED)
    assert len(failed) == 2
    # newest first
    assert failed[0].rendered_at > failed[1].rendered_at

    ok = repo.list_by_status(status=RENDER_EVENT_STATUS_OK)
    assert len(ok) == 2


def test_list_by_status_respects_limit() -> None:
    """Bounded reads keep the triage query predictable."""
    repo = InMemoryDisclosureRenderEventRepository()
    for _ in range(5):
        repo.record(status=RENDER_EVENT_STATUS_APR_FAILED, **_common_kwargs())
    assert (
        len(repo.list_by_status(status=RENDER_EVENT_STATUS_APR_FAILED, limit=3))
        == 3
    )


# ---------------------------------------------------------------------------
# Helper: write + audit pairing
# ---------------------------------------------------------------------------


def test_record_helper_writes_render_event_and_audit_row() -> None:
    """The helper writes a render-event row AND a paired audit_log row
    with ``action='aegis_disclosure_render_event'`` so the durable
    audit trail captures the event regardless of which side a reader
    queries from."""
    repo = InMemoryDisclosureRenderEventRepository()
    audit = InMemoryAuditLog()
    record_disclosure_render_event(
        repo,
        audit,
        deal_id=UUID(_FIXED_MERCHANT_ID),
        merchant_id=UUID(_FIXED_MERCHANT_ID),
        state="CA",
        template_path="compliance/templates/ca_sb1235.html.j2",
        status=RENDER_EVENT_STATUS_OK,
        status_reason=None,
        details={"tier": 1},
        recipient_email=None,
        rendered_by="api",
    )

    assert len(repo.rows) == 1
    audit_rows = [
        e
        for e in audit.entries
        if e["action"] == "aegis_disclosure_render_event"
    ]
    assert len(audit_rows) == 1
    row = audit_rows[0]
    assert row["subject_type"] == "deal"
    assert row["subject_id"] == _FIXED_MERCHANT_ID
    assert row["details"]["status"] == RENDER_EVENT_STATUS_OK
    assert row["details"]["render_event_id"] == str(repo.rows[0].id)


# ---------------------------------------------------------------------------
# Route-level: happy path
# ---------------------------------------------------------------------------


@pytest.fixture
def audit_log() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def render_event_repo() -> InMemoryDisclosureRenderEventRepository:
    return InMemoryDisclosureRenderEventRepository()


@pytest.fixture
def client(
    audit_log: InMemoryAuditLog,
    render_event_repo: InMemoryDisclosureRenderEventRepository,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = InMemoryMerchantRepository
    app.dependency_overrides[get_funder_repository] = InMemoryFunderRepository
    app.dependency_overrides[get_repository] = InMemoryDocumentRepository
    app.dependency_overrides[get_audit] = lambda: audit_log
    app.dependency_overrides[get_disclosure_render_event_repository] = (
        lambda: render_event_repo
    )
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def _deal_payload(state: str = "CA") -> dict[str, Any]:
    return {
        "merchant_id": _FIXED_MERCHANT_ID,
        "business_name": _PII_BUSINESS,
        "owner_name": _PII_OWNER,
        "state": state,
        "avg_daily_balance": "12500.00",
        "true_revenue": "110000.00",
        "monthly_revenue": "110000.00",
        "lowest_balance": "3000.00",
        "num_nsf": 0,
        "days_negative": 0,
        "mca_positions": 0,
        "mca_daily_total": "0.00",
        "debt_to_revenue": "0.00",
        "fraud_score": 10,
        "statement_period_start": "2026-04-01",
        "statement_period_end": "2026-04-30",
        "statement_days": 30,
        "requested_amount": "50000.00",
        "requested_factor": "1.30",
        "requested_term_days": 120,
    }


def _score_payload() -> dict[str, Any]:
    return {
        "score": 70,
        "tier": "B",
        "recommendation": "approve",
        "suggested_max_advance": "50000.00",
        "recommended_factor_rate": "1.30",
        "recommended_holdback_pct": "0.12",
        "estimated_payback_days": 120,
    }


def _force_apr_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: object, **kwargs: object) -> Decimal:
        raise APRCalculationError("brentq failed to converge")

    monkeypatch.setattr(disclosure_context, "calculate_apr", _boom)


def test_happy_path_writes_one_ok_render_event(
    client: TestClient,
    render_event_repo: InMemoryDisclosureRenderEventRepository,
) -> None:
    """A normal CA disclosure render must persist exactly one render-
    event row with ``status='ok'``."""
    body = {"state": "CA", "deal": _deal_payload(), "score": _score_payload()}
    resp = client.post("/disclosures/render", json=body, headers=AUTH)
    assert resp.status_code == 200, resp.text

    rows = render_event_repo.rows
    assert len(rows) == 1
    rec = rows[0]
    assert rec.status == RENDER_EVENT_STATUS_OK
    assert rec.state == "CA"
    assert rec.merchant_id == UUID(_FIXED_MERCHANT_ID)
    assert rec.deal_id == UUID(_FIXED_MERCHANT_ID)


def test_happy_path_writes_paired_audit_row(
    client: TestClient, audit_log: InMemoryAuditLog
) -> None:
    """The render-event helper also writes one ``aegis_disclosure_render_event``
    audit row so the durable audit trail captures the event."""
    body = {"state": "CA", "deal": _deal_payload(), "score": _score_payload()}
    resp = client.post("/disclosures/render", json=body, headers=AUTH)
    assert resp.status_code == 200

    audit_rows = [
        e
        for e in audit_log.entries
        if e["action"] == "aegis_disclosure_render_event"
    ]
    assert len(audit_rows) == 1
    assert audit_rows[0]["details"]["status"] == RENDER_EVENT_STATUS_OK


# ---------------------------------------------------------------------------
# Route-level: APR failure
# ---------------------------------------------------------------------------


def test_apr_failure_writes_one_apr_compute_failed_render_event(
    client: TestClient,
    render_event_repo: InMemoryDisclosureRenderEventRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """APR failure must persist exactly one render-event row with
    ``status='apr_compute_failed'`` and the same non-PII details the
    U3 audit row already carries."""
    _force_apr_failure(monkeypatch)
    body = {"state": "CA", "deal": _deal_payload(), "score": _score_payload()}
    resp = client.post("/disclosures/render", json=body, headers=AUTH)
    assert resp.status_code == 503

    rows = render_event_repo.rows
    assert len(rows) == 1
    rec = rows[0]
    assert rec.status == RENDER_EVENT_STATUS_APR_FAILED
    assert rec.state == "CA"
    assert rec.deal_id == UUID(_FIXED_MERCHANT_ID)
    assert rec.template_path is None
    # Numeric inputs surfaced through details.
    assert rec.details is not None
    assert rec.details["principal"] == "50000.00"
    assert rec.details["factor"] == "1.30"
    assert rec.details["term_days"] == 120
    # disbursement_date is set by the context builder; ISO-format.
    assert isinstance(rec.details["disbursement_date"], str)
    date.fromisoformat(rec.details["disbursement_date"])  # ParseError if not


def test_apr_failure_still_writes_u3_audit_row(
    client: TestClient,
    audit_log: InMemoryAuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REGRESSION: U16 must NOT replace U3's audit_log write. Both
    audit actions fire on the failure path:

      * U3's ``aegis_apr_compute_failed`` row (the existing contract)
      * U16's ``aegis_disclosure_render_event`` row (the new structured
        render-event signal written by the helper)
    """
    _force_apr_failure(monkeypatch)
    body = {"state": "CA", "deal": _deal_payload(), "score": _score_payload()}
    resp = client.post("/disclosures/render", json=body, headers=AUTH)
    assert resp.status_code == 503

    u3 = [
        e for e in audit_log.entries if e["action"] == "aegis_apr_compute_failed"
    ]
    u16 = [
        e
        for e in audit_log.entries
        if e["action"] == "aegis_disclosure_render_event"
    ]
    assert len(u3) == 1, audit_log.entries
    assert len(u16) == 1, audit_log.entries


# ---------------------------------------------------------------------------
# PII canary
# ---------------------------------------------------------------------------


def test_render_event_details_carry_no_pii_on_apr_failure(
    client: TestClient,
    render_event_repo: InMemoryDisclosureRenderEventRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per CLAUDE.md PII rules: the render-event ``details`` JSONB must
    NOT carry business_name, owner_name, or transaction descriptions.
    Numeric APR inputs + deal-id-ish references only."""
    _force_apr_failure(monkeypatch)
    body = {"state": "CA", "deal": _deal_payload(), "score": _score_payload()}
    resp = client.post("/disclosures/render", json=body, headers=AUTH)
    assert resp.status_code == 503

    rec = render_event_repo.rows[0]
    flat = repr(rec.details)
    assert _PII_BUSINESS not in flat
    assert _PII_OWNER not in flat
    # Defensive: also assert on metadata + status_reason just in case
    # someone later refactors and starts stashing context there.
    assert _PII_BUSINESS not in repr(rec.metadata)
    assert _PII_OWNER not in repr(rec.metadata)
    assert _PII_BUSINESS not in (rec.status_reason or "")
    assert _PII_OWNER not in (rec.status_reason or "")


def test_render_event_details_carry_no_pii_on_happy_path(
    client: TestClient,
    render_event_repo: InMemoryDisclosureRenderEventRepository,
) -> None:
    """Same PII canary for the success path — the happy-path details
    payload is operator-debug-shaped, not merchant-shaped."""
    body = {"state": "CA", "deal": _deal_payload(), "score": _score_payload()}
    resp = client.post("/disclosures/render", json=body, headers=AUTH)
    assert resp.status_code == 200

    rec = render_event_repo.rows[0]
    flat = repr(rec.details)
    assert _PII_BUSINESS not in flat
    assert _PII_OWNER not in flat
