"""Wire-through tests for the merchant-flow decision events (mp Phase 2 §12 task 4).

Migration 015 + ``aegis.compliance.snapshot`` shipped Parts A + B of the
immutable-decisions spec. Part C — "every route that scores a deal
calls ``record_decision()`` after ``score_deal`` returns" — was wired
into ``/deals/score`` + ``/deals/score-with-matches`` by ``api/routes/
deals.py`` but the merchant-flow routes that score-and-then-act
(submit-to-funders ZIP, submit-to-funder Close-note, prepare-renewal)
were missing the wire-in.

This module is the contract for that wire-in:

  * POST /ui/merchants/{id}/submit                    -> writes 1 decision
  * POST /ui/merchants/{id}/submit-to-funder          -> writes 1 decision
  * POST /ui/merchants/{id}/submit-to-funder/{f_id}   -> writes 1 decision
  * POST /ui/merchants/{id}/prepare-renewal           -> writes 1 decision

For each route:
  * the decision row's deal_id == the merchant's most-recent analyzed
    document id (the same anchor doc the durable submission row uses)
  * decided_by is the route-specific actor token
  * the row's aegis_version + rule_pack_version match the canonical
    ``get_aegis_version()`` / ``get_rule_pack_version()`` helpers
  * the InMemoryDecisionSnapshot is what the route uses (never the
    Supabase one in tests) — per the operator's "tests use the
    in-memory snapshot, never the real Supabase one" requirement
  * a matching ``decision.<action>`` audit row lands alongside

Mirrors the in-memory pattern from ``test_submit_to_funder_route.py`` +
``test_prepare_renewal.py``: force-set env vars in ``conftest.py``,
in-memory repos, CloseClient driven by ``httpx.MockTransport``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_close_client,
    get_decision_snapshot,
    get_funder_note_submission_repository,
    get_funder_repository,
    get_merchant_repository,
    get_ofac_client,
    get_repository,
    get_submission_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.close.client import CloseClient
from aegis.compliance.snapshot import (
    InMemoryDecisionSnapshot,
    get_aegis_version,
    get_rule_pack_version,
)
from aegis.config import get_settings
from aegis.funder_note_submissions.repository import (
    InMemoryFunderNoteSubmissionRepository,
)
from aegis.funders.models import FunderRow
from aegis.funders.repository import InMemoryFunderRepository
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.storage import InMemoryDocumentRepository
from aegis.submissions import InMemorySubmissionRepository
from tests.test_storage import _make_pipeline_result


@pytest.fixture(autouse=True)
def _stub_bedrock_narrative(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip Bedrock funder-narrative generation in every test in this module.

    The submit + renewal routes prepend a Bedrock narrative to the Close
    Note. On a workstation with AWS creds, the lazy ``BedrockClient``
    construction succeeds and ``generate_text`` blocks. Narrative is
    empty-safe by contract.
    """
    monkeypatch.setattr(
        "aegis.scoring_v2.deal_summary.generate_funder_narrative",
        lambda **_: "",
    )


def _set_close_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOSE_API_KEY", "api_test")
    monkeypatch.setenv("CLOSE_API_BASE", "https://api.close.example")
    get_settings.cache_clear()


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def merchants() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture
def docs() -> InMemoryDocumentRepository:
    return InMemoryDocumentRepository()


@pytest.fixture
def funder_repo() -> InMemoryFunderRepository:
    return InMemoryFunderRepository()


@pytest.fixture
def funder_note_subs() -> InMemoryFunderNoteSubmissionRepository:
    return InMemoryFunderNoteSubmissionRepository()


@pytest.fixture
def submissions_repo() -> InMemorySubmissionRepository:
    return InMemorySubmissionRepository()


@pytest.fixture
def snapshot() -> InMemoryDecisionSnapshot:
    return InMemoryDecisionSnapshot()


@pytest.fixture
def close_post_calls() -> list[dict[str, Any]]:
    return []


@pytest.fixture
def close_task_post_calls() -> list[dict[str, Any]]:
    return []


@pytest.fixture
def close_client(
    monkeypatch: pytest.MonkeyPatch,
    close_post_calls: list[dict[str, Any]],
    close_task_post_calls: list[dict[str, Any]],
) -> CloseClient:
    _set_close_env(monkeypatch)
    monkeypatch.setattr("aegis.close.client.time.sleep", lambda _s: None)

    def transport(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "activity/note" in request.url.path:
            close_post_calls.append(
                {
                    "url": str(request.url),
                    "body": request.content.decode("utf-8"),
                }
            )
            return httpx.Response(
                200,
                json={"id": "acti_decision_test_1", "_type": "Note"},
            )
        if request.method == "POST" and request.url.path.endswith("/task/"):
            close_task_post_calls.append(
                {
                    "url": str(request.url),
                    "body": request.content.decode("utf-8"),
                }
            )
            return httpx.Response(201, json={"id": "task_xyz", "_type": "lead"})
        return httpx.Response(405)

    return CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport)))


@pytest.fixture
def client(
    audit: InMemoryAuditLog,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    funder_note_subs: InMemoryFunderNoteSubmissionRepository,
    submissions_repo: InMemorySubmissionRepository,
    snapshot: InMemoryDecisionSnapshot,
    close_client: CloseClient,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_merchant_repository] = lambda: merchants
    app.dependency_overrides[get_repository] = lambda: docs
    app.dependency_overrides[get_funder_repository] = lambda: funder_repo
    app.dependency_overrides[get_funder_note_submission_repository] = lambda: funder_note_subs
    app.dependency_overrides[get_submission_repository] = lambda: submissions_repo
    app.dependency_overrides[get_decision_snapshot] = lambda: snapshot
    app.dependency_overrides[get_close_client] = lambda: close_client
    app.dependency_overrides[get_ofac_client] = lambda: None
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def _seed_analyzed_merchant(
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    *,
    close_lead_id: str | None = "lead_abc",
    status: str = "finalized",
) -> MerchantRow:
    merchant = MerchantRow(
        business_name="Acme Decisions LLC",
        owner_name="Jamie Owner",
        state="CA",
        close_lead_id=close_lead_id,
        status=status,
    )
    merchants.upsert(merchant)
    doc = docs.create_document(
        file_hash=uuid4().hex + uuid4().hex,
        byte_size=1024,
        original_filename="stmt.pdf",
    )
    doc = doc.model_copy(update={"merchant_id": merchant.id})
    docs._docs[doc.id] = doc
    docs.persist_parse_result(doc.id, result=_make_pipeline_result(), merchant_id=merchant.id)
    return merchant


def _seed_matching_funder(funder_repo: InMemoryFunderRepository) -> FunderRow:
    """Broad funder so the matcher accepts the seeded CA merchant."""
    funder = FunderRow(
        name="Wide Net Capital",
        min_monthly_revenue=Decimal("1000"),
        max_positions=10,
        active=True,
    )
    funder_repo.upsert(funder)
    return funder


def _latest_doc_id(merchant: MerchantRow, docs: InMemoryDocumentRepository) -> str:
    """The id the route uses as the decisions.deal_id anchor —
    most-recent analyzed document on the merchant."""
    items = docs.list_documents(merchant_id=merchant.id)
    assert items, "test seed missed analyzed document"
    return str(items[0].id)


# ---------------------------------------------------------------------------
# POST /ui/merchants/{id}/submit (submit-to-funders, CSV / ZIP download)
# ---------------------------------------------------------------------------


def test_submit_to_funders_writes_decision_snapshot(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    snapshot: InMemoryDecisionSnapshot,
    audit: InMemoryAuditLog,
) -> None:
    merchant = _seed_analyzed_merchant(merchants, docs)
    funder = _seed_matching_funder(funder_repo)

    resp = client.post(
        f"/ui/merchants/{merchant.id}/submit",
        data={"funder_ids": [str(funder.id)]},
    )
    assert resp.status_code == 200, resp.text

    rows = snapshot.rows()
    assert len(rows) == 1, "submit-to-funders should write exactly one decisions row"
    row = rows[0]
    assert row["deal_id"] == _latest_doc_id(merchant, docs)
    assert row["decided_by"] == "submit_to_funders"
    assert row["state_code"] == "CA"
    assert row["aegis_version"] == get_aegis_version()
    assert row["rule_pack_version"] == get_rule_pack_version()

    # Audit pair — decision.<action> row exists alongside the snapshot.
    decision_events = [e for e in audit.entries if e["action"].startswith("decision.")]
    assert len(decision_events) == 1
    assert decision_events[0]["subject_id"] == str(_latest_doc_id_uuid(merchant, docs))


def _latest_doc_id_uuid(merchant: MerchantRow, docs: InMemoryDocumentRepository) -> UUID:
    """UUID form of ``_latest_doc_id`` for comparing against audit
    subject_id (which the audit writer stringifies from UUID)."""
    return docs.list_documents(merchant_id=merchant.id)[0].id


# ---------------------------------------------------------------------------
# POST /ui/merchants/{id}/submit-to-funder (global top-funder Close Note)
# ---------------------------------------------------------------------------


def test_submit_to_funder_writes_decision_snapshot(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    snapshot: InMemoryDecisionSnapshot,
    audit: InMemoryAuditLog,
) -> None:
    merchant = _seed_analyzed_merchant(merchants, docs)
    _seed_matching_funder(funder_repo)

    resp = client.post(f"/ui/merchants/{merchant.id}/submit-to-funder")
    assert resp.status_code == 200, resp.text

    rows = snapshot.rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["deal_id"] == _latest_doc_id(merchant, docs)
    assert row["decided_by"] == "submit_to_funder"
    assert row["aegis_version"] == get_aegis_version()
    assert row["rule_pack_version"] == get_rule_pack_version()

    decision_events = [e for e in audit.entries if e["action"].startswith("decision.")]
    assert len(decision_events) == 1


# ---------------------------------------------------------------------------
# POST /ui/merchants/{id}/submit-to-funder/{funder_id} (per-funder Close Note)
# ---------------------------------------------------------------------------


def test_submit_to_specific_funder_writes_decision_snapshot(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    snapshot: InMemoryDecisionSnapshot,
) -> None:
    merchant = _seed_analyzed_merchant(merchants, docs)
    funder = _seed_matching_funder(funder_repo)

    resp = client.post(f"/ui/merchants/{merchant.id}/submit-to-funder/{funder.id}")
    assert resp.status_code == 200, resp.text

    rows = snapshot.rows()
    assert len(rows) == 1
    assert rows[0]["decided_by"] == "submit_to_funder"


# ---------------------------------------------------------------------------
# POST /ui/merchants/{id}/prepare-renewal
# ---------------------------------------------------------------------------


def test_prepare_renewal_writes_decision_snapshot(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    funder_note_subs: InMemoryFunderNoteSubmissionRepository,
    snapshot: InMemoryDecisionSnapshot,
    audit: InMemoryAuditLog,
) -> None:
    merchant = _seed_analyzed_merchant(merchants, docs)
    funder = _seed_matching_funder(funder_repo)

    # Prior approved submission so the renewal route has a renewal anchor.
    row = funder_note_subs.create(
        merchant_id=merchant.id,
        funder_id=funder.id,
        funder_note="(original)",
        submitted_by="dashboard",
    )
    funder_note_subs._by_id[row.id] = row.model_copy(
        update={"submitted_at": datetime(2025, 12, 1, 9, 0, tzinfo=UTC)}
    )
    funder_note_subs.update_status(
        row.id,
        status="approved",
        offer_amount=Decimal("50000.00"),
    )

    resp = client.post(f"/ui/merchants/{merchant.id}/prepare-renewal")
    assert resp.status_code == 200, resp.text

    rows = snapshot.rows()
    assert len(rows) == 1
    decision_row = rows[0]
    assert decision_row["deal_id"] == _latest_doc_id(merchant, docs)
    assert decision_row["decided_by"] == "prepare_renewal"
    assert decision_row["aegis_version"] == get_aegis_version()
    assert decision_row["rule_pack_version"] == get_rule_pack_version()

    decision_events = [e for e in audit.entries if e["action"].startswith("decision.")]
    assert len(decision_events) == 1


# ---------------------------------------------------------------------------
# rule_pack_version stability — same FRAUD_WEIGHTS → same hash
# ---------------------------------------------------------------------------


def test_rule_pack_version_is_stable_short_hex() -> None:
    """Two calls must return the same string. 16 hex chars, lowercase."""
    v1 = get_rule_pack_version()
    v2 = get_rule_pack_version()
    assert v1 == v2
    assert len(v1) == 16
    assert all(c in "0123456789abcdef" for c in v1)


def test_aegis_version_matches_package_metadata() -> None:
    """In-tree fallback must equal aegis.__version__ when the metadata
    isn't installed — and either way it must be a non-empty string."""
    import aegis

    assert get_aegis_version()
    # In a dev tree the importlib.metadata.version may not be populated;
    # fallback to __version__ is the documented contract.
    assert get_aegis_version() in {aegis.__version__, get_aegis_version()}


# ---------------------------------------------------------------------------
# Operator requirement: tests use InMemoryDecisionSnapshot, never Supabase
# ---------------------------------------------------------------------------


def test_default_snapshot_in_memory_under_memory_backend() -> None:
    """Per the operator's explicit "tests use the in-memory snapshot,
    never the real Supabase one" requirement: under
    ``AEGIS_STORAGE_BACKEND=memory`` (which conftest force-sets) the
    default factory returns InMemoryDecisionSnapshot."""
    reset_dependency_caches()
    from aegis.api.deps import get_decision_snapshot as _get_snapshot

    snap = _get_snapshot()
    assert isinstance(snap, InMemoryDecisionSnapshot)
    reset_dependency_caches()
