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
    outcome = record_override(payload, repo=override_repo, reply_repo=reply_repo, audit=audit)

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
    stamped_row = next(r for r in reply_repo.overrides() if r["id"] == str(override_id))
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


# ---------------------------------------------------------------------------
# Dossier-flow override (mp Phase 10 / migration 072)
# ---------------------------------------------------------------------------


from aegis.api.deps import get_repository  # noqa: E402
from aegis.compliance.overrides import (  # noqa: E402
    DossierOverridePayload,
    build_reason_code_summary,
    record_dossier_override,
)
from aegis.storage import (  # noqa: E402
    DocumentNotFoundError,
    DocumentRow,
    InMemoryDocumentRepository,
)


def _make_doc(docs: InMemoryDocumentRepository, *, merchant_id: UUID | None = None) -> DocumentRow:
    """Helper to drop a parsed-state document into the in-memory repo.

    Returns the persisted ``DocumentRow`` so tests can read its id +
    parse_status. The document lands in ``parse_status='proceed'`` so
    the dossier-override gate (``parse_status IN ('proceed','decline')``)
    is satisfied immediately.
    """
    doc = docs.create_document(
        file_hash=f"deadbeef{uuid4().hex[:24]}",
        byte_size=1024,
        original_filename="bank.pdf",
        merchant_id=merchant_id,
    )
    docs.set_parse_status(doc.id, "proceed")
    return doc


def test_dossier_override_persists_row_audit_and_flips_parse_status() -> None:
    """``record_dossier_override`` writes the override row, flips
    ``documents.parse_status``, and emits a ``deal.operator_override``
    audit row with the right details."""
    override_repo = InMemoryOverrideRepository()
    docs = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()

    merchant_id = uuid4()
    doc = _make_doc(docs, merchant_id=merchant_id)

    payload = DossierOverridePayload(
        merchant_id=merchant_id,
        document_id=doc.id,
        decision_id=uuid4(),
        original_recommendation="proceed",
        operator_decision="decline",
        reason_code="data_quality_concern",
        reason_detail="balance lines didn't reconcile",
        pattern_false_positives=["mca_stacking"],
        operator_id="dashboard",
        operator_email="op@example.com",
    )
    override_id = record_dossier_override(
        payload, override_repo=override_repo, documents=docs, audit=audit
    )

    # Override row landed with the new shape.
    rows = override_repo.rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["merchant_id"] == str(merchant_id)
    assert row["document_id"] == str(doc.id)
    assert row["deal_id"] == str(doc.id)
    assert row["pattern_false_positives"] == ["mca_stacking"]
    # Legacy singular column mirrored for backwards-compat readers.
    assert row["pattern_false_positive"] == ["mca_stacking"]
    assert row["operator_decision"] == "decline"
    assert row["reason_code"] == "data_quality_concern"
    assert isinstance(UUID(row["id"]), UUID)

    # parse_status flipped.
    refreshed = docs.get_document(doc.id)
    assert refreshed.parse_status == "decline"

    # Audit row emitted with the operator-email actor convention.
    audit_actions = [e["action"] for e in audit.entries]
    assert "deal.operator_override" in audit_actions
    audit_row = next(e for e in audit.entries if e["action"] == "deal.operator_override")
    assert audit_row["actor"] == "operator:op@example.com"
    assert audit_row["actor_email"] == "op@example.com"
    assert audit_row["subject_type"] == "deal"
    assert audit_row["subject_id"] == str(doc.id)
    assert audit_row["details"]["override_id"] == str(override_id)
    assert audit_row["details"]["new_parse_status"] == "decline"
    assert audit_row["details"]["operator_decision"] == "decline"
    assert audit_row["details"]["pattern_false_positives_count"] == 1


def test_dossier_override_approve_maps_to_proceed_parse_status() -> None:
    """An approve decision flips parse_status to ``proceed`` (which it
    already is on a fresh parse) — the round-trip itself is the test:
    the helper accepts an approve operator_decision and lands the row
    correctly, even when the new status equals the current status."""
    override_repo = InMemoryOverrideRepository()
    docs = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()

    merchant_id = uuid4()
    doc = _make_doc(docs, merchant_id=merchant_id)
    # Pretend the doc landed in 'decline' (e.g. parser-side hard-decline)
    # so the operator's 'approve' actually changes the state.
    docs.set_parse_status(doc.id, "decline")

    payload = DossierOverridePayload(
        merchant_id=merchant_id,
        document_id=doc.id,
        original_recommendation="decline",
        operator_decision="approve",
        reason_code="score_too_conservative",
        pattern_false_positives=[],
        operator_id="dashboard",
    )
    record_dossier_override(payload, override_repo=override_repo, documents=docs, audit=audit)

    refreshed = docs.get_document(doc.id)
    assert refreshed.parse_status == "proceed"


def test_dossier_override_with_no_decision_id_round_trips() -> None:
    """Older docs may not have a decisions row. The override write path
    accepts ``decision_id=None`` and the persisted row carries it as
    None — matching migration 072's nullable FK."""
    override_repo = InMemoryOverrideRepository()
    docs = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()

    merchant_id = uuid4()
    doc = _make_doc(docs, merchant_id=merchant_id)

    payload = DossierOverridePayload(
        merchant_id=merchant_id,
        document_id=doc.id,
        decision_id=None,
        original_recommendation="proceed",
        operator_decision="decline",
        reason_code="gut",
        pattern_false_positives=[],
        operator_id="dashboard",
    )
    record_dossier_override(payload, override_repo=override_repo, documents=docs, audit=audit)

    row = override_repo.rows()[0]
    assert row["decision_id"] is None


def test_dossier_override_unknown_document_raises_before_write() -> None:
    """An unknown document_id surfaces as DocumentNotFoundError BEFORE
    any override row is inserted — so a typo in the modal doesn't leave
    a half-written audit trail behind."""
    override_repo = InMemoryOverrideRepository()
    docs = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()

    payload = DossierOverridePayload(
        merchant_id=uuid4(),
        document_id=uuid4(),  # never created
        original_recommendation="proceed",
        operator_decision="approve",
        reason_code="gut",
        pattern_false_positives=[],
        operator_id="dashboard",
    )
    with pytest.raises(DocumentNotFoundError):
        record_dossier_override(payload, override_repo=override_repo, documents=docs, audit=audit)
    assert override_repo.rows() == []
    assert audit.entries == []


def test_dossier_override_rejects_refer_operator_decision() -> None:
    """The dossier modal collapses 'refer' into 'decline'. The
    ``DossierOverridePayload`` literal enforces the {approve, decline}
    set at validation time so a stale form posting 'refer' fails fast."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DossierOverridePayload(
            merchant_id=uuid4(),
            document_id=uuid4(),
            original_recommendation="proceed",
            operator_decision="refer",
            reason_code="gut",
            operator_id="dashboard",
        )


# ---------------------------------------------------------------------------
# Confusion-matrix aggregator (build_reason_code_summary)
# ---------------------------------------------------------------------------


def test_confusion_matrix_buckets_three_overrides_into_two_reasons() -> None:
    """Three overrides across two reason_codes with mixed outcomes.

    Verifies:
      * counts per (reason, outcome) cell are right;
      * NULL outcome falls into ``pending``;
      * rows are sorted by total desc, then alphabetically;
      * total per row equals sum across all outcome columns.
    """
    rows: list[dict[str, Any]] = [
        {"id": "1", "reason_code": "score_too_aggressive", "outcome": "funded"},
        {"id": "2", "reason_code": "score_too_aggressive", "outcome": None},
        {"id": "3", "reason_code": "gut", "outcome": "declined_by_funder"},
    ]
    summary = build_reason_code_summary(rows)
    assert len(summary) == 2

    # Sorted by total desc: score_too_aggressive (2) comes before gut (1).
    first, second = summary
    assert first.reason_code == "score_too_aggressive"
    assert first.total == 2
    assert first.counts["funded"] == 1
    assert first.counts["pending"] == 1
    assert first.counts["declined_by_funder"] == 0

    assert second.reason_code == "gut"
    assert second.total == 1
    assert second.counts["declined_by_funder"] == 1
    assert second.counts["funded"] == 0


def test_confusion_matrix_renders_empty_when_no_overrides() -> None:
    """Day-zero: no overrides yet → empty summary list, endpoint still 200s."""
    assert build_reason_code_summary([]) == []


# ---------------------------------------------------------------------------
# Dossier-override route (POST /ui/merchants/{id}/documents/{doc_id}/override)
# ---------------------------------------------------------------------------


@pytest.fixture
def docs_repo() -> InMemoryDocumentRepository:
    return InMemoryDocumentRepository()


@pytest.fixture
def dossier_client(
    override_repo: InMemoryOverrideRepository,
    audit: InMemoryAuditLog,
    docs_repo: InMemoryDocumentRepository,
) -> Iterator[TestClient]:
    """TestClient with all three repos wired through dependency_overrides."""
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_override_repository] = lambda: override_repo
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_repository] = lambda: docs_repo
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def test_dossier_override_route_writes_row_and_redirects(
    dossier_client: TestClient,
    override_repo: InMemoryOverrideRepository,
    docs_repo: InMemoryDocumentRepository,
) -> None:
    merchant_id = uuid4()
    doc = _make_doc(docs_repo, merchant_id=merchant_id)

    resp = dossier_client.post(
        f"/ui/merchants/{merchant_id}/documents/{doc.id}/override",
        data={
            "operator_decision": "decline",
            "reason_code": "pattern_false_positive",
            "original_recommendation": "proceed",
            "reason_detail": "the stacking detector misread an owner transfer",
            "pattern_false_positives": ["mca_stacking", "wash_deposit_suspected"],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == f"/ui/merchants/{merchant_id}"

    rows = override_repo.rows()
    assert len(rows) == 1
    assert rows[0]["operator_decision"] == "decline"
    assert rows[0]["pattern_false_positives"] == [
        "mca_stacking",
        "wash_deposit_suspected",
    ]
    assert docs_repo.get_document(doc.id).parse_status == "decline"


def test_dossier_override_route_hx_redirect_when_htmx_request(
    dossier_client: TestClient,
    override_repo: InMemoryOverrideRepository,
    docs_repo: InMemoryDocumentRepository,
) -> None:
    """HTMX caller (HX-Request header present) gets a 200 with
    HX-Redirect — the HTMX client follows it without a full page
    reload. Non-HTMX still gets the 303."""
    merchant_id = uuid4()
    doc = _make_doc(docs_repo, merchant_id=merchant_id)
    resp = dossier_client.post(
        f"/ui/merchants/{merchant_id}/documents/{doc.id}/override",
        data={
            "operator_decision": "approve",
            "reason_code": "score_too_conservative",
            "original_recommendation": "proceed",
        },
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["HX-Redirect"] == f"/ui/merchants/{merchant_id}"


def test_dossier_override_route_invalid_reason_returns_400(
    dossier_client: TestClient, docs_repo: InMemoryDocumentRepository
) -> None:
    merchant_id = uuid4()
    doc = _make_doc(docs_repo, merchant_id=merchant_id)
    resp = dossier_client.post(
        f"/ui/merchants/{merchant_id}/documents/{doc.id}/override",
        data={
            "operator_decision": "decline",
            "reason_code": "I-just-want-to",
            "original_recommendation": "proceed",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "invalid override payload" in resp.text


def test_dossier_override_route_unknown_document_returns_404(
    dossier_client: TestClient,
) -> None:
    merchant_id = uuid4()
    bogus_doc = uuid4()
    resp = dossier_client.post(
        f"/ui/merchants/{merchant_id}/documents/{bogus_doc}/override",
        data={
            "operator_decision": "decline",
            "reason_code": "gut",
            "original_recommendation": "proceed",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 404


def test_dossier_override_route_resolves_operator_email_from_cf_header(
    dossier_client: TestClient,
    override_repo: InMemoryOverrideRepository,
    docs_repo: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
) -> None:
    """The /ui route surface is gated by Cloudflare Access upstream
    (no bearer dependency). The route reads the operator identity
    from the ``cf-access-authenticated-user-email`` header and uses
    it as both the ``operator_id`` AND the audit row's ``actor`` /
    ``actor_email``. Absent header → fallback to ``operator_id="dashboard"``
    (tested in the prior route tests). Present header → identity
    propagates per the project's ``audit_actor_shape_by_auth_method``
    convention.
    """
    merchant_id = uuid4()
    doc = _make_doc(docs_repo, merchant_id=merchant_id)
    resp = dossier_client.post(
        f"/ui/merchants/{merchant_id}/documents/{doc.id}/override",
        data={
            "operator_decision": "decline",
            "reason_code": "gut",
            "original_recommendation": "proceed",
        },
        headers={"cf-access-authenticated-user-email": "alice@example.com"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text

    audit_row = next(e for e in audit.entries if e["action"] == "deal.operator_override")
    assert audit_row["actor"] == "operator:alice@example.com"
    assert audit_row["actor_email"] == "alice@example.com"

    row = override_repo.rows()[0]
    assert row["operator_id"] == "alice@example.com"


# ---------------------------------------------------------------------------
# Confusion-matrix endpoint (GET /ui/overrides/summary)
# ---------------------------------------------------------------------------


def test_overrides_summary_endpoint_renders_with_no_overrides(
    dossier_client: TestClient,
) -> None:
    """Day-zero render — empty table, status 200, empty-state copy."""
    resp = dossier_client.get("/ui/overrides/summary")
    assert resp.status_code == 200, resp.text
    assert "No operator overrides captured yet" in resp.text


def test_overrides_summary_endpoint_renders_counts(
    dossier_client: TestClient,
    docs_repo: InMemoryDocumentRepository,
    override_repo: InMemoryOverrideRepository,
    audit: InMemoryAuditLog,
) -> None:
    """Three overrides across two reason_codes → both rows render with
    the right counts in the right columns."""
    # Seed three overrides via the dossier-flow function so the rows
    # land via the in-memory repo's insert_dossier_override path.
    merchant_id = uuid4()
    for _ in range(2):
        doc = _make_doc(docs_repo, merchant_id=merchant_id)
        record_dossier_override(
            DossierOverridePayload(
                merchant_id=merchant_id,
                document_id=doc.id,
                original_recommendation="proceed",
                operator_decision="decline",
                reason_code="score_too_aggressive",
                operator_id="dashboard",
            ),
            override_repo=override_repo,
            documents=docs_repo,
            audit=audit,
        )
    doc = _make_doc(docs_repo, merchant_id=merchant_id)
    record_dossier_override(
        DossierOverridePayload(
            merchant_id=merchant_id,
            document_id=doc.id,
            original_recommendation="proceed",
            operator_decision="decline",
            reason_code="gut",
            operator_id="dashboard",
        ),
        override_repo=override_repo,
        documents=docs_repo,
        audit=audit,
    )

    resp = dossier_client.get("/ui/overrides/summary")
    assert resp.status_code == 200, resp.text
    # Both reason codes appear.
    assert "score_too_aggressive" in resp.text
    assert "gut" in resp.text
    # Top row's total is 2 (sorted by total desc).
    # Table includes the pending column header.
    assert "pending" in resp.text
    # Total count surfaced in the deck.
    assert "3 override" in resp.text
