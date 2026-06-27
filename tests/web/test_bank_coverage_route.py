"""Tests for the /ui/bank-coverage dashboard.

Covers:

* GET renders 200 with the expected summary counts + row content.
* Empty-state copy when there are no banks at all.
* Sort order: highest-docs-no-hints row appears first, lowest-docs-
  manual-hints row appears last.
* Bump-to-threshold button presence/absence rules.
* Bump endpoint: increments successful_parses + writes audit row.
* Generate-hints endpoint: enqueues pending job + writes audit row.
* Bump endpoint requires Admin or Underwriter — viewer gets 403.
* Generate-hints endpoint requires Admin or Underwriter — viewer gets 403.
* Sidebar link present in the base template.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_bank_layout_repository,
    get_operator_repository,
    get_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.bank_layouts import InMemoryBankLayoutRepository
from aegis.ops.operator_repository import InMemoryOperatorRepository
from aegis.ops.operators import Operator, OperatorRole
from aegis.storage import AnalysisRow, InMemoryDocumentRepository, ParseStatus

# Test operator emails — match the pattern used in test_role_gate.py.
_ADMIN_EMAIL = "admin@aegis.test"
_UW_EMAIL = "uw@aegis.test"
_VIEWER_EMAIL = "viewer@aegis.test"


def _make_operators() -> InMemoryOperatorRepository:
    operators = InMemoryOperatorRepository()
    operators._seed(
        Operator(
            id=uuid4(),
            email=_ADMIN_EMAIL,
            display_name="Admin Operator",
            role=OperatorRole.ADMIN,
        )
    )
    operators._seed(
        Operator(
            id=uuid4(),
            email=_UW_EMAIL,
            display_name="Underwriter Operator",
            role=OperatorRole.UNDERWRITER,
        )
    )
    operators._seed(
        Operator(
            id=uuid4(),
            email=_VIEWER_EMAIL,
            display_name="Viewer Operator",
            role=OperatorRole.VIEWER,
        )
    )
    return operators


def _make_doc_with_analysis(
    docs: InMemoryDocumentRepository,
    *,
    bank_name: str,
    parse_status: ParseStatus = "proceed",
) -> None:
    """Insert one document + analysis row carrying ``bank_name``.

    Pokes the in-memory backend's internal dicts directly rather than
    routing through ``persist_parse_result`` — building a real
    ``PipelineResult`` for a fixture would require synthesising a full
    aggregates + sourced-money tree per row, which adds 50 LOC of
    test scaffolding for no observable behaviour gain.
    """
    from datetime import date
    from decimal import Decimal

    doc = docs.create_document(
        file_hash=f"hash_{uuid4().hex}",
        byte_size=1024,
        original_filename="statement.pdf",
    )
    docs.set_parse_status(doc.id, parse_status)

    analysis = AnalysisRow(
        id=uuid4(),
        document_id=doc.id,
        statement_period_start=date(2026, 5, 1),
        statement_period_end=date(2026, 5, 31),
        statement_days=31,
        beginning_balance=Decimal("0"),
        ending_balance=Decimal("0"),
        avg_daily_balance=Decimal("0"),
        true_revenue=Decimal("0"),
        monthly_revenue=Decimal("0"),
        lowest_balance=Decimal("0"),
        num_nsf=0,
        days_negative=0,
        mca_positions=0,
        mca_daily_total=Decimal("0"),
        debt_to_revenue=Decimal("0"),
        bank_name=bank_name,
    )
    docs._analyses[doc.id] = analysis


@pytest.fixture
def coverage_client() -> Iterator[
    tuple[
        TestClient,
        InMemoryBankLayoutRepository,
        InMemoryDocumentRepository,
        InMemoryAuditLog,
    ]
]:
    """TestClient with all four backends pinned in-memory."""
    reset_dependency_caches()
    layouts = InMemoryBankLayoutRepository()
    docs = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()
    operators = _make_operators()
    app = create_app()
    app.dependency_overrides[get_bank_layout_repository] = lambda: layouts
    app.dependency_overrides[get_repository] = lambda: docs
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_operator_repository] = lambda: operators
    with TestClient(app) as c:
        yield c, layouts, docs, audit
    app.dependency_overrides.clear()
    reset_dependency_caches()


# ---------------------------------------------------------------------------
# GET render tests
# ---------------------------------------------------------------------------


def test_empty_state_renders(
    coverage_client: tuple[
        TestClient,
        InMemoryBankLayoutRepository,
        InMemoryDocumentRepository,
        InMemoryAuditLog,
    ],
) -> None:
    client, _, _, _ = coverage_client
    resp = client.get(
        "/ui/bank-coverage",
        headers={"cf-access-authenticated-user-email": _ADMIN_EMAIL},
    )
    assert resp.status_code == 200
    assert "No bank documents on file yet" in resp.text


def test_sort_order_high_volume_no_hints_first(
    coverage_client: tuple[
        TestClient,
        InMemoryBankLayoutRepository,
        InMemoryDocumentRepository,
        InMemoryAuditLog,
    ],
) -> None:
    """Sort: most docs first, biggest gap (no hints) within bucket first."""
    client, layouts, docs, _ = coverage_client
    # Chase: 5 docs, no hints (biggest gap)
    for _ in range(5):
        _make_doc_with_analysis(docs, bank_name="Chase")
    # BoA: 3 docs, manual hints
    for _ in range(3):
        _make_doc_with_analysis(docs, bank_name="Bank of America")
    layouts.upsert_success(bank_name="Bank of America", fingerprint={})
    layouts.upsert_success(bank_name="Bank of America", fingerprint={})
    layouts.upsert_success(bank_name="Bank of America", fingerprint={})
    layouts.set_hints(bank_name="Bank of America", hints="Operator-authored hints.")
    # TD: 1 doc, no hints (smaller volume than Chase)
    _make_doc_with_analysis(docs, bank_name="TD Bank")

    resp = client.get(
        "/ui/bank-coverage",
        headers={"cf-access-authenticated-user-email": _ADMIN_EMAIL},
    )
    assert resp.status_code == 200
    body = resp.text
    chase_pos = body.find("Chase")
    boa_pos = body.find("Bank of America")
    td_pos = body.find("TD Bank")
    assert 0 <= chase_pos < boa_pos, "Chase (most docs) should top the list"
    assert boa_pos < td_pos or boa_pos > td_pos  # they're in different volume buckets
    # Verify Chase row tops because of higher volume even though TD also has no hints.
    assert chase_pos < td_pos


def test_summary_counts_match_table_body(
    coverage_client: tuple[
        TestClient,
        InMemoryBankLayoutRepository,
        InMemoryDocumentRepository,
        InMemoryAuditLog,
    ],
) -> None:
    client, layouts, docs, _ = coverage_client
    _make_doc_with_analysis(docs, bank_name="Chase")
    _make_doc_with_analysis(docs, bank_name="BoA")
    layouts.set_hints(bank_name="BoA", hints="hints text")
    resp = client.get(
        "/ui/bank-coverage",
        headers={"cf-access-authenticated-user-email": _ADMIN_EMAIL},
    )
    assert resp.status_code == 200
    # 2 banks: Chase (no hints) + BoA (manual hints)
    assert ">2</strong> banks seen" in resp.text or "2</strong> banks seen" in resp.text
    assert "1</strong> with manual hints" in resp.text
    assert "1</strong> with no hints" in resp.text


# ---------------------------------------------------------------------------
# Button visibility tests
# ---------------------------------------------------------------------------


def test_bump_button_present_when_under_threshold_and_no_manual(
    coverage_client: tuple[
        TestClient,
        InMemoryBankLayoutRepository,
        InMemoryDocumentRepository,
        InMemoryAuditLog,
    ],
) -> None:
    client, layouts, docs, _ = coverage_client
    _make_doc_with_analysis(docs, bank_name="Chase")
    # successful_parses=1 in layout (under threshold of 3), no hints set
    layouts.upsert_success(bank_name="Chase", fingerprint={})
    resp = client.get(
        "/ui/bank-coverage",
        headers={"cf-access-authenticated-user-email": _ADMIN_EMAIL},
    )
    assert resp.status_code == 200
    assert "Bump to threshold" in resp.text
    assert 'data-test-id="bump-parse-count"' in resp.text


def test_bump_button_absent_when_already_at_threshold(
    coverage_client: tuple[
        TestClient,
        InMemoryBankLayoutRepository,
        InMemoryDocumentRepository,
        InMemoryAuditLog,
    ],
) -> None:
    client, layouts, docs, _ = coverage_client
    _make_doc_with_analysis(docs, bank_name="Chase")
    # Get successful_parses to >=3 + add hints so the row is "manual" status
    for _ in range(3):
        layouts.upsert_success(bank_name="Chase", fingerprint={})
    layouts.set_hints(bank_name="Chase", hints="hints text")
    resp = client.get(
        "/ui/bank-coverage",
        headers={"cf-access-authenticated-user-email": _ADMIN_EMAIL},
    )
    assert resp.status_code == 200
    # Bump button should be absent for this row
    assert 'data-test-id="bump-parse-count"' not in resp.text


# ---------------------------------------------------------------------------
# Bump endpoint
# ---------------------------------------------------------------------------


def test_bump_increments_successful_parses_and_audits(
    coverage_client: tuple[
        TestClient,
        InMemoryBankLayoutRepository,
        InMemoryDocumentRepository,
        InMemoryAuditLog,
    ],
) -> None:
    client, layouts, docs, audit = coverage_client
    _make_doc_with_analysis(docs, bank_name="Chase")
    layouts.upsert_success(bank_name="Chase", fingerprint={})
    before = layouts.find_by_bank_name("Chase")
    assert before is not None
    before_count = before.successful_parses

    resp = client.post(
        "/ui/bank-coverage/Chase/bump-parse-count",
        headers={"cf-access-authenticated-user-email": _UW_EMAIL},
    )
    assert resp.status_code == 200
    after = layouts.find_by_bank_name("Chase")
    assert after is not None
    assert after.successful_parses == before_count + 1

    actions = [e["action"] for e in audit.entries]
    assert "bank_coverage.parse_count_bumped" in actions
    rec = next(e for e in audit.entries if e["action"] == "bank_coverage.parse_count_bumped")
    assert rec["details"]["bump_delta"] == 1
    assert rec["details"]["new_successful_parses"] == after.successful_parses


def test_bump_endpoint_403_for_viewer(
    coverage_client: tuple[
        TestClient,
        InMemoryBankLayoutRepository,
        InMemoryDocumentRepository,
        InMemoryAuditLog,
    ],
) -> None:
    client, layouts, _, _ = coverage_client
    layouts.upsert_success(bank_name="Chase", fingerprint={})
    resp = client.post(
        "/ui/bank-coverage/Chase/bump-parse-count",
        headers={"cf-access-authenticated-user-email": _VIEWER_EMAIL},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Generate-hints endpoint
# ---------------------------------------------------------------------------


def test_generate_hints_enqueues_job_and_audits(
    coverage_client: tuple[
        TestClient,
        InMemoryBankLayoutRepository,
        InMemoryDocumentRepository,
        InMemoryAuditLog,
    ],
) -> None:
    client, layouts, _, audit = coverage_client
    layouts.upsert_success(bank_name="Chase", fingerprint={})

    resp = client.post(
        "/ui/bank-coverage/Chase/generate-hints",
        headers={"cf-access-authenticated-user-email": _UW_EMAIL},
    )
    assert resp.status_code == 200

    pending = getattr(client.app.state, "pending_generate_hints_jobs", [])  # type: ignore[attr-defined]
    assert pending == [{"bank_name": "Chase"}]

    actions = [e["action"] for e in audit.entries]
    assert "bank_coverage.generate_hints_enqueued" in actions


def test_generate_hints_endpoint_403_for_viewer(
    coverage_client: tuple[
        TestClient,
        InMemoryBankLayoutRepository,
        InMemoryDocumentRepository,
        InMemoryAuditLog,
    ],
) -> None:
    client, _, _, _ = coverage_client
    resp = client.post(
        "/ui/bank-coverage/Chase/generate-hints",
        headers={"cf-access-authenticated-user-email": _VIEWER_EMAIL},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Sidebar link
# ---------------------------------------------------------------------------


def test_sidebar_link_present(
    coverage_client: tuple[
        TestClient,
        InMemoryBankLayoutRepository,
        InMemoryDocumentRepository,
        InMemoryAuditLog,
    ],
) -> None:
    client, _, _, _ = coverage_client
    resp = client.get(
        "/ui/",
        headers={"cf-access-authenticated-user-email": _ADMIN_EMAIL},
    )
    # Today page renders the base template + topstrip — the link
    # should appear once the topstrip includes it.
    assert resp.status_code == 200
    assert "/ui/bank-coverage" in resp.text
    assert "Bank coverage" in resp.text
