"""Tests for the unified A+B+C dossier panel.

Two layers:

1. **Unit tests** on ``build_unified_tracks_view`` — verify it
   handles the realistic dossier states (no docs, docs but no
   transactions, docs with transactions, fail-verdict docs).
2. **Integration test** on ``GET /ui/merchants/{id}`` — verify the
   partial actually renders into the dossier HTML and the structural
   no-decline guard holds (no decline / score wiring on the
   ``UnifiedTracksView`` schema).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_funder_repository,
    get_merchant_repository,
    get_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.funders.repository import InMemoryFunderRepository
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.parser.models import ClassifiedTransaction
from aegis.scoring_v2.dossier_panel import (
    UnifiedTracksView,
    build_unified_tracks_view,
)
from aegis.storage import DocumentRow, InMemoryDocumentRepository


def _doc(
    *,
    metadata_flags: tuple[str, ...] = (),
    all_flags: tuple[str, ...] = (),
    metadata_score: int = 0,
    parse_status: str = "manual_review",
    uploaded_at: datetime | None = None,
) -> DocumentRow:
    return DocumentRow.model_validate(
        {
            "id": uuid4(),
            "file_hash": "z" * 64,
            "byte_size": 1024,
            "original_filename": "stmt.pdf",
            "parse_status": parse_status,
            "metadata_flags": list(metadata_flags),
            "all_flags": list(all_flags),
            "fraud_score_breakdown": {"metadata": metadata_score},
            "uploaded_at": uploaded_at or datetime.now(UTC),
        }
    )


# ─────────────────────────────────────────────────────────────────────
# Unit tests on build_unified_tracks_view
# ─────────────────────────────────────────────────────────────────────


def test_unified_view_empty_documents_returns_clean_skeleton() -> None:
    """No documents → no integrity verdicts, no risk band, no panel,
    insufficient_data_reason explains why."""
    view = build_unified_tracks_view(
        documents=[],
        list_transactions=lambda _id: [],
    )
    assert view.integrity_verdicts == ()
    assert view.integrity_worst_verdict is None
    assert view.risk_band is None
    assert view.context_panel is None
    assert "no documents" in view.insufficient_data_reason.lower()


def test_unified_view_arkm_shaped_drift_plus_editor_fail() -> None:
    """A&R-shaped: 4 documents each with iText editor + reconciliation
    drift, no persisted transactions → 4 FAIL verdicts, no Track B/C
    (transactions absent)."""
    docs = [
        _doc(
            metadata_flags=("editor_detected: iText 2.1.7 by 1T3XT",),
            all_flags=(
                "[MATH] reconciliation_failed_period: expected 197.45 got 364.12",
                "[MATH] reconciliation_failed_withdrawal_total: …",
            ),
            metadata_score=0,
        )
        for _ in range(4)
    ]
    view = build_unified_tracks_view(
        documents=docs,
        list_transactions=lambda _id: [],
    )
    assert len(view.integrity_verdicts) == 4
    assert view.integrity_worst_verdict == "fail"
    assert "4 fail" in view.integrity_summary
    for v in view.integrity_verdicts:
        assert v.verdict == "fail"
        assert v.branch == "drift_plus_editor"
    # No transactions → Track B/C are None with reason.
    assert view.risk_band is None
    assert view.context_panel is None
    assert "no classified transactions" in view.insufficient_data_reason.lower()


def test_unified_view_mixed_verdicts_summarise_correctly() -> None:
    """Mixed verdicts roll up to the worst-case headline plus a
    count-by-level summary."""
    docs = [
        # FAIL — editor + drift
        _doc(
            metadata_flags=("editor_detected: iText 2.1.7 by 1T3XT",),
            all_flags=("[MATH] reconciliation_failed_period: …",),
        ),
        # REVIEW — drift alone
        _doc(
            metadata_flags=(),
            all_flags=("[MATH] reconciliation_failed_period: …",),
        ),
        # CLEAN — only an innocuous flag so the doc isn't skipped
        _doc(
            metadata_flags=("page_count: 4",),
            all_flags=(),
        ),
    ]
    view = build_unified_tracks_view(
        documents=docs,
        list_transactions=lambda _id: [],
    )
    assert view.integrity_worst_verdict == "fail"
    assert "1 fail" in view.integrity_summary
    assert "1 review" in view.integrity_summary
    assert "1 clean" in view.integrity_summary


def test_unified_view_docs_without_any_signals_are_skipped() -> None:
    """Documents with no metadata, no flags, no score → omitted from
    the panel (don't pollute it with meaningless 'clean' rows)."""
    docs = [
        _doc(metadata_flags=(), all_flags=(), metadata_score=0),
        _doc(metadata_flags=(), all_flags=(), metadata_score=0),
    ]
    view = build_unified_tracks_view(
        documents=docs,
        list_transactions=lambda _id: [],
    )
    assert view.integrity_verdicts == ()
    assert view.integrity_worst_verdict is None


def test_unified_view_with_transactions_computes_b_and_c() -> None:
    """When at least one document has classified transactions, Track B
    and C produce outputs (informational; no decline gate)."""
    doc = _doc(
        metadata_flags=(),
        all_flags=(),
    )
    txns = [
        ClassifiedTransaction(
            id=uuid4(),
            posted_date=date(2026, 3, 1),
            description=(
                "INTERNATIONAL WH DES:SENDER ID:XXX INDN:REDACTED MERCHANT CO ID:X"
            ),
            amount=Decimal("99500.00"),
            running_balance=Decimal("99500.00"),
            source_page=1,
            source_line=1,
            category="ach_credit",
            classification_confidence=100,
        ),
    ]
    view = build_unified_tracks_view(
        documents=[doc],
        list_transactions=lambda _id: txns,
    )
    assert view.risk_band is not None
    assert view.context_panel is not None
    assert view.risk_band.cashflow.true_revenue_total > Decimal("0")


# ─────────────────────────────────────────────────────────────────────
# Structural guard — view schema MUST NOT carry decline fields
# ─────────────────────────────────────────────────────────────────────


def test_unified_view_has_no_decline_or_score_field() -> None:
    """The view is the dossier-rendering shape. Adding a decline
    field would let template code, or a future refactor, wire the
    A/B/C surface into the live decline path. Block it here."""
    fields = set(UnifiedTracksView.model_fields)
    forbidden = {
        "decline",
        "auto_decline",
        "risk_score",
        "fraud_score",
        "score",
        "outcome",
        "verdict_action",
    }
    leaked = fields & forbidden
    assert not leaked, (
        f"UnifiedTracksView must not carry decline/score fields; "
        f"leaked: {leaked}"
    )


# ─────────────────────────────────────────────────────────────────────
# Integration: dossier route renders the partial
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def empty_funder_repo() -> InMemoryFunderRepository:
    return InMemoryFunderRepository()


@pytest.fixture
def repos_with_arkm_shaped_merchant() -> tuple[
    InMemoryMerchantRepository,
    InMemoryDocumentRepository,
    InMemoryAuditLog,
    MerchantRow,
    DocumentRow,
]:
    merchants = InMemoryMerchantRepository()
    docs = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()
    merchant = MerchantRow(
        business_name="ARKM Integrity Probe",
        owner_name="Op",
        state="CA",
    )
    saved = merchants.upsert(merchant)
    # Document with A&R-shaped integrity signature.
    doc_row = DocumentRow.model_validate(
        {
            "id": uuid4(),
            "file_hash": "a" * 64,
            "byte_size": 2048,
            "original_filename": "Lili Monthly Statement 2026-03.pdf",
            "merchant_id": saved.id,
            "parse_status": "manual_review",
            "metadata_flags": ["editor_detected: iText 2.1.7 by 1T3XT"],
            "all_flags": [
                "[MATH] reconciliation_failed_period: "
                "expected 197.45 got 364.12",
                "[MATH] reconciliation_failed_withdrawal_total: "
                "listed 32508.86 vs printed 32167.19",
                "[MATH] reconciliation_failed_intraday: "
                "2026-03-19 p2l1: expected 8047.99 got 8214.66",
            ],
            "fraud_score_breakdown": {"metadata": 0},
            "uploaded_at": datetime.now(UTC),
        }
    )
    docs._docs[doc_row.id] = doc_row
    return merchants, docs, audit, saved, doc_row


@pytest.fixture
def client(
    repos_with_arkm_shaped_merchant: tuple[
        InMemoryMerchantRepository,
        InMemoryDocumentRepository,
        InMemoryAuditLog,
        MerchantRow,
        DocumentRow,
    ],
    empty_funder_repo: InMemoryFunderRepository,
) -> Iterator[TestClient]:
    merchants, docs, audit, _, _ = repos_with_arkm_shaped_merchant
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: merchants
    app.dependency_overrides[get_repository] = lambda: docs
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_funder_repository] = lambda: empty_funder_repo
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def test_dossier_renders_unified_panel_for_arkm_shaped_merchant(
    client: TestClient,
    repos_with_arkm_shaped_merchant: tuple[Any, ...],
) -> None:
    """The full /ui/merchants/{id} render must include the unified
    panel with A's FAIL verdict and the existing score block both."""
    _, _, _, merchant, _ = repos_with_arkm_shaped_merchant
    resp = client.get(f"/ui/merchants/{merchant.id}")
    assert resp.status_code == 200
    html = resp.text

    # Unified panel header is present.
    assert "3-track view" in html
    # Track A section + the iText evidence renders.
    assert "Document integrity" in html
    assert "iText 2.1.7" in html
    # Track A's FAIL chip shows.
    assert ">FAIL<" in html
    # Track B/C sections render even when insufficient data — the
    # underwriter sees the empty-state copy rather than missing panels.
    assert "Business risk band" in html
    assert "Concentration / context" in html
    # The existing score block is NOT removed (additive guarantee).
    # Either the score block or the "Score unavailable" copy appears.
    assert "Score unavailable" in html or "score_result" in html or "Tier" in html


# ─────────────────────────────────────────────────────────────────────
# Wave 2 — Track A signal + branch humanization in the rendered dossier.
#
# ARKM-shaped doc is the drift_plus_editor fail case. Strong-metadata
# fail requires a fresh merchant whose only doc carries a metadata_score
# >= 50 — its own fixture below renders the strong_metadata branch.
# ─────────────────────────────────────────────────────────────────────


def test_dossier_humanizes_drift_plus_editor_branch_and_signals(
    client: TestClient,
    repos_with_arkm_shaped_merchant: tuple[Any, ...],
) -> None:
    """Track A's branch + evidence tokens must render in plain English
    in the dossier body. The raw engineer tokens (e.g.
    ``drift_plus_editor``) belong in ``title=`` tooltips only — they
    should not appear unwrapped in the visible body text."""
    _, _, _, merchant, _ = repos_with_arkm_shaped_merchant
    resp = client.get(f"/ui/merchants/{merchant.id}")
    assert resp.status_code == 200
    html = resp.text

    # Humanized branch label is rendered.
    assert "Editor tampering + reconciliation drift" in html
    # Humanized signal labels are rendered for each EvidenceItem.signal.
    assert "Editor metadata fingerprint" in html
    assert "Reconciliation: period total mismatch" in html
    assert "Reconciliation: withdrawal total mismatch" in html
    assert "Reconciliation: intraday row mismatch" in html

    # Raw branch/signal tokens stay accessible as ``title=`` tooltips so
    # engineer-underwriters can hover for the code-level identifier.
    # The branch token in particular has no other source — confirming it
    # appears in a tooltip proves the template wires the tooltip up.
    # (Signal tokens like ``editor_detected`` also legitimately appear
    # inside the verbatim ``EvidenceItem.detail`` flag strings, e.g.
    # ``editor_detected: iText 2.1.7 by 1T3XT`` — those are evidence
    # detail, not a token leak, so we don't assert global absence.)
    assert 'title="drift_plus_editor"' in html
    assert 'title="editor_detected"' in html
    assert 'title="reconciliation_failed_period"' in html

    # And the branch column does NOT render the bare ``drift_plus_editor``
    # token as its visible body text — the humanized label is what an
    # underwriter reads. We bound this by asserting the humanized label
    # appears before the closing </td> on the branch cell.
    assert ">Editor tampering + reconciliation drift<" in html


@pytest.fixture
def repos_with_strong_metadata_merchant() -> tuple[
    InMemoryMerchantRepository,
    InMemoryDocumentRepository,
    InMemoryAuditLog,
    MerchantRow,
    DocumentRow,
]:
    """Merchant whose single document trips the strong_metadata branch
    (metadata_score >= 50). Used to verify the humanizer renders the
    strong-metadata branch label, not just drift_plus_editor."""
    merchants = InMemoryMerchantRepository()
    docs = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()
    merchant = MerchantRow(
        business_name="Strong Metadata Probe",
        owner_name="Op",
        state="CA",
    )
    saved = merchants.upsert(merchant)
    doc_row = DocumentRow.model_validate(
        {
            "id": uuid4(),
            "file_hash": "b" * 64,
            "byte_size": 4096,
            "original_filename": "stmt-strong-metadata.pdf",
            "merchant_id": saved.id,
            "parse_status": "manual_review",
            # An editor flag plus a high metadata_score together drives
            # branch 1 (strong_metadata) regardless of math drift.
            "metadata_flags": ["editor_detected: Foxit PhantomPDF"],
            "all_flags": [],
            "fraud_score_breakdown": {"metadata": 72},
            "uploaded_at": datetime.now(UTC),
        }
    )
    docs._docs[doc_row.id] = doc_row
    return merchants, docs, audit, saved, doc_row


@pytest.fixture
def strong_metadata_client(
    repos_with_strong_metadata_merchant: tuple[
        InMemoryMerchantRepository,
        InMemoryDocumentRepository,
        InMemoryAuditLog,
        MerchantRow,
        DocumentRow,
    ],
    empty_funder_repo: InMemoryFunderRepository,
) -> Iterator[TestClient]:
    merchants, docs, audit, _, _ = repos_with_strong_metadata_merchant
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: merchants
    app.dependency_overrides[get_repository] = lambda: docs
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_funder_repository] = lambda: empty_funder_repo
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def test_dossier_humanizes_strong_metadata_branch(
    strong_metadata_client: TestClient,
    repos_with_strong_metadata_merchant: tuple[Any, ...],
) -> None:
    """Strong-metadata branch renders ``"Strong metadata anomaly"`` in
    the visible body and keeps the ``strong_metadata`` raw token in a
    title= tooltip."""
    _, _, _, merchant, _ = repos_with_strong_metadata_merchant
    resp = strong_metadata_client.get(f"/ui/merchants/{merchant.id}")
    assert resp.status_code == 200
    html = resp.text

    assert "Strong metadata anomaly" in html
    # metadata_score is one of the EvidenceItem.signal tokens this branch
    # emits — it should render as the humanized label, not the raw token.
    assert "Metadata anomaly score" in html

    # Branch tooltip is wired up + humanized label renders as the
    # visible body text.
    assert 'title="strong_metadata"' in html
    assert ">Strong metadata anomaly<" in html


# ─────────────────────────────────────────────────────────────────────
# Wave 3 §3.2 — end-to-end mutation-test guarantee for F2 (commit c3beb25)
#
# F2 added a ``for f in drift_failures:`` loop to branch 1 of
# ``compute_integrity_verdict`` so that strong-metadata fails ALSO
# surface corroborating reconciliation-drift rows as evidence. Without
# F2, a merchant whose document trips strong_metadata AND has math
# drift would render evidence that's metadata-only — and an
# underwriter looking at a "metadata-only" fail might soften it to
# review without ever seeing the math signal.
#
# Agent D's ``test_dossier_humanizes_strong_metadata_branch`` exercises
# the humanizer on a strong_metadata doc with EMPTY ``all_flags`` —
# its fixture has no drift to surface, so it cannot detect an F2
# regression. The end-to-end test below ships a strong_metadata doc
# WITH math drift and asserts the drift evidence is in the rendered
# dossier body. If F2's drift-loop is ever reverted, this test fails.
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def repos_with_strong_metadata_plus_drift_merchant() -> tuple[
    InMemoryMerchantRepository,
    InMemoryDocumentRepository,
    InMemoryAuditLog,
    MerchantRow,
    DocumentRow,
]:
    """Merchant whose single document trips BOTH branch 1
    (strong_metadata, metadata_score >= 50) AND would have tripped
    branch 2 (drift_plus_editor) had branch 1 not won precedence.

    The competent-fabrication overlap case: editor flag + high
    metadata_score + reconciliation drift all present. Branch 1 wins
    by precedence (the loop F2 added is what makes its evidence list
    include the drift rows that branch 2 would have surfaced)."""
    merchants = InMemoryMerchantRepository()
    docs = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()
    merchant = MerchantRow(
        business_name="Strong Metadata + Drift Probe",
        owner_name="Op",
        state="CA",
    )
    saved = merchants.upsert(merchant)
    doc_row = DocumentRow.model_validate(
        {
            "id": uuid4(),
            "file_hash": "c" * 64,
            "byte_size": 4096,
            "original_filename": "stmt-strong-metadata-plus-drift.pdf",
            "merchant_id": saved.id,
            "parse_status": "manual_review",
            # Editor flag — counts toward both strong_metadata
            # (via score 72) and would corroborate drift_plus_editor.
            "metadata_flags": ["editor_detected: iText 2.1.7 by 1T3XT"],
            # Reconciliation drift — branch 1 must surface this as
            # evidence (F2). Without F2 this row would be dropped.
            "all_flags": [
                "[MATH] reconciliation_failed_period: "
                "expected 1000 got 950",
            ],
            "fraud_score_breakdown": {"metadata": 72},
            "uploaded_at": datetime.now(UTC),
        }
    )
    docs._docs[doc_row.id] = doc_row
    return merchants, docs, audit, saved, doc_row


@pytest.fixture
def strong_metadata_plus_drift_client(
    repos_with_strong_metadata_plus_drift_merchant: tuple[
        InMemoryMerchantRepository,
        InMemoryDocumentRepository,
        InMemoryAuditLog,
        MerchantRow,
        DocumentRow,
    ],
    empty_funder_repo: InMemoryFunderRepository,
) -> Iterator[TestClient]:
    merchants, docs, audit, _, _ = (
        repos_with_strong_metadata_plus_drift_merchant
    )
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: merchants
    app.dependency_overrides[get_repository] = lambda: docs
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_funder_repository] = lambda: empty_funder_repo
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def test_strong_metadata_dossier_surfaces_drift_evidence_end_to_end(
    strong_metadata_plus_drift_client: TestClient,
    repos_with_strong_metadata_plus_drift_merchant: tuple[Any, ...],
) -> None:
    """End-to-end mutation-test guarantee for F2 (commit c3beb25).

    A merchant whose single document trips branch 1 (strong_metadata,
    metadata_score >= 50) AND carries reconciliation drift must render
    the drift evidence in the operator-facing dossier HTML — not just
    the metadata score / editor row.

    Without F2's ``for f in drift_failures:`` loop in branch 1, the
    rendered evidence list would include "Metadata anomaly score" and
    "Editor metadata fingerprint" but NOT "Reconciliation: period total
    mismatch". An underwriter reading the dossier could then mistake
    the verdict for "just metadata noise" and soften the fail to review
    — exactly the failure mode F2 prevents.

    If F2's loop is ever reverted, the assertions in this test that
    look for the drift signal in the body will fail.
    """
    _, _, _, merchant, _ = repos_with_strong_metadata_plus_drift_merchant
    resp = strong_metadata_plus_drift_client.get(
        f"/ui/merchants/{merchant.id}"
    )
    assert resp.status_code == 200
    html = resp.text

    # Branch chip — strong_metadata won precedence over drift_plus_editor.
    assert "Strong metadata anomaly" in html
    assert ">Strong metadata anomaly<" in html
    assert 'title="strong_metadata"' in html

    # Track A's evidence rows — the metadata-side signals must render.
    assert "Metadata anomaly score" in html

    # ── F2 guarantee ────────────────────────────────────────────────
    # The drift evidence MUST surface in the rendered dossier. Without
    # F2's drift-loop in branch 1 of ``compute_integrity_verdict``,
    # ``reconciliation_failed_period`` would not appear in the evidence
    # tuple — and the humanized "Reconciliation: period total mismatch"
    # row would not exist in the rendered HTML.
    assert "Reconciliation: period total mismatch" in html
    # The verbatim failure detail string also renders so the underwriter
    # sees the magnitudes (not just the category label).
    assert "expected 1000 got 950" in html
    # And the raw token is wired through the title= tooltip so the
    # engineer-underwriter can hover for the code-level identifier.
    assert 'title="reconciliation_failed_period"' in html

    # Verdict chip — fail tone. Mirrors the assertion in
    # ``test_dossier_renders_unified_panel_for_arkm_shaped_merchant``.
    assert ">FAIL<" in html

    # ── Defensive: branch precedence guard ──────────────────────────
    # Because metadata_score >= 50, branch 1 (strong_metadata) wins
    # over branch 2 (drift_plus_editor). The branch token rendered in
    # visible body text must be strong_metadata's humanized label, not
    # drift_plus_editor's. Asserts the branch precedence didn't flip.
    assert ">Editor tampering + reconciliation drift<" not in html
