"""U15 — cross-statement pipeline orchestrator + worker call-site tests.

Three concerns covered:

1. The orchestrator (``run_cross_statement_detection``) correctly
   assembles ``PriorDocumentRef`` + ``PriorAnalysisIdentity`` lists
   from the merchant's prior documents + analyses and forwards them
   to the U12 detector.

2. The worker hook (``aegis.workers._run_cross_statement_detection``)
   fires the orchestrator after ``persist_parse_result`` and stashes
   the resulting Pattern list on ``PipelineResult.cross_statement_patterns``.

3. ``AnalysisRow`` persists ``account_holder`` end-to-end so a prior
   upload's holder string survives for the next upload's detector
   run.

Per CLAUDE.md "Decision-boundary changes — shadow-first": every Pattern
emitted by the detector MUST have ``severity == 0``. Asserted on every
fire path.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from aegis.audit import InMemoryAuditLog
from aegis.merchants.cross_statement_pipeline import (
    run_cross_statement_detection,
)
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.merchants.shadow_signals import (
    InMemoryMerchantShadowSignalRepository,
)
from aegis.parser.metadata import MetadataAnalysis
from aegis.parser.models import (
    Aggregates,
    ClassifiedTransaction,
    ExtractedStatement,
    StatementSummary,
    ValidationResult,
    _SourcedInt,
    _SourcedMoney,
)
from aegis.parser.pipeline import PipelineResult
from aegis.storage import AnalysisRow, InMemoryDocumentRepository
from aegis.workers import parse_document

# ---------------------------------------------------------------------------
# Helpers


def _real_file_hash(path: Path) -> str:
    """SHA-256 hex of the file bytes — matches what production records
    in ``documents.file_hash`` at upload time. Without this the
    chunk-B sha256-divergence check routes the worker through the
    dead-letter path which would mask the cross-statement detector
    behavior we're testing."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def fake_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "stmt.pdf"
    p.write_bytes(b"%PDF-1.4\nfake")
    return p


def _build_pipeline_result(
    *,
    bank_name: str | None = "Chase",
    account_holder: str | None = "Acme LLC",
    account_last4: str | None = "1234",
) -> PipelineResult:
    """Construct a complete ``PipelineResult`` with controllable bank
    identity on the summary so the worker hook reads through to the
    U12 detector with the values we want.

    Mirrors ``tests.test_storage._make_pipeline_result`` and
    ``tests.test_workers._pipeline_result_with_account_holder`` but
    accepts all three identity fields so the related-account
    sub-detector has something to drift on.
    """
    tx_id = uuid4()
    summary = StatementSummary(
        bank_name=bank_name,
        account_holder=account_holder,
        account_last4=account_last4,
        beginning_balance=Decimal("1000.00"),
        ending_balance=Decimal("2000.00"),
        deposit_total=Decimal("3000.00"),
        withdrawal_total=Decimal("2000.00"),
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
    )
    classified = [
        ClassifiedTransaction(
            id=tx_id,
            posted_date=date(2026, 1, 5),
            description="DEPOSIT",
            amount=Decimal("3000.00"),
            running_balance=Decimal("4000.00"),
            source_page=1,
            source_line=10,
            category="deposit",
            classification_confidence=95,
        )
    ]
    aggregates = Aggregates(
        avg_daily_balance=_SourcedMoney(value=Decimal("1500.00"), source_ids=[tx_id]),
        true_revenue=_SourcedMoney(value=Decimal("3000.00"), source_ids=[tx_id]),
        num_nsf=_SourcedInt(value=0, source_ids=[]),
        days_negative=_SourcedInt(value=0, source_ids=[]),
        debt_to_revenue=Decimal("0.00"),
        mca_daily_total=_SourcedMoney(value=Decimal("0.00"), source_ids=[]),
    )
    extraction_stub: Any = type(
        "Stub",
        (),
        {
            "statement": ExtractedStatement(
                summary=summary, transactions=classified
            )
        },
    )()
    return PipelineResult(
        parse_status="proceed",
        metadata=MetadataAnalysis(
            pdf_creation_date=None,
            pdf_modification_date=None,
            pdf_producer=None,
            pdf_creator=None,
            pdf_author=None,
            page_count=2,
            file_size_bytes=10240,
            eof_markers=1,
            page_sizes=["LETTER"],
            flags=[],
            fraud_score=0,
        ),
        extraction=extraction_stub,
        validation=ValidationResult(passed=True),
        classified=classified,
        patterns=None,
        aggregates=aggregates,
        fraud_score=10,
        fraud_score_breakdown={
            "metadata_score": 0,
            "math_score": 0,
            "patterns_score": 0,
        },
        all_flags=[],
    )


# ---------------------------------------------------------------------------
# AnalysisRow round-trip (migration 041)


def test_analysis_row_round_trips_account_holder() -> None:
    """``account_holder`` survives ``_analysis_to_db_row`` ->
    ``_db_row_to_analysis``. Without this the U12 detector can't read
    a prior upload's holder string on the next parse."""
    from aegis.storage import _analysis_to_db_row, _db_row_to_analysis

    holder = "ACME LLC"
    analysis = AnalysisRow(
        id=uuid4(),
        document_id=uuid4(),
        merchant_id=uuid4(),
        statement_period_start=date(2026, 1, 1),
        statement_period_end=date(2026, 1, 31),
        statement_days=30,
        beginning_balance=Decimal("1000.00"),
        ending_balance=Decimal("2000.00"),
        avg_daily_balance=Decimal("1500.00"),
        true_revenue=Decimal("3000.00"),
        monthly_revenue=Decimal("3000.00"),
        lowest_balance=Decimal("900.00"),
        num_nsf=0,
        days_negative=0,
        mca_positions=0,
        mca_daily_total=Decimal("0.00"),
        debt_to_revenue=Decimal("0.00"),
        payroll_detected=False,
        bank_name="Chase",
        account_last4="1234",
        account_holder=holder,
    )

    db_row = _analysis_to_db_row(analysis)
    assert db_row["account_holder"] == holder

    restored = _db_row_to_analysis(db_row)
    assert restored.account_holder == holder


def test_analysis_row_preserves_null_account_holder() -> None:
    """``account_holder=None`` (pass-1 failed to recover it) round-trips
    as None — the detector skips holders it can't read so this is the
    safe-default path."""
    from aegis.storage import _analysis_to_db_row, _db_row_to_analysis

    analysis = AnalysisRow(
        id=uuid4(),
        document_id=uuid4(),
        merchant_id=uuid4(),
        statement_period_start=date(2026, 1, 1),
        statement_period_end=date(2026, 1, 31),
        statement_days=30,
        beginning_balance=Decimal("1000.00"),
        ending_balance=Decimal("2000.00"),
        avg_daily_balance=Decimal("1500.00"),
        true_revenue=Decimal("3000.00"),
        monthly_revenue=Decimal("3000.00"),
        lowest_balance=Decimal("900.00"),
        num_nsf=0,
        days_negative=0,
        mca_positions=0,
        mca_daily_total=Decimal("0.00"),
        debt_to_revenue=Decimal("0.00"),
        payroll_detected=False,
    )

    restored = _db_row_to_analysis(_analysis_to_db_row(analysis))
    assert restored.account_holder is None


# ---------------------------------------------------------------------------
# Orchestrator over the in-memory repository


def test_orchestrator_no_priors_returns_empty() -> None:
    """First upload for a merchant — no priors → no flags, no error."""
    repo = InMemoryDocumentRepository()
    merchant_id = uuid4()
    current_doc = repo.create_document(
        file_hash="a" * 64,
        byte_size=10,
        original_filename="stmt.pdf",
        merchant_id=merchant_id,
    )
    flags = run_cross_statement_detection(
        merchant_id=merchant_id,
        current_document_id=current_doc.id,
        current_sha256="a" * 64,
        current_uploaded_at=datetime.now(UTC),
        current_bank_name="Chase",
        current_account_holder="Acme LLC",
        current_account_last4="1234",
        repo=repo,
    )
    assert flags == []


# ---------------------------------------------------------------------------
# Worker hook — end-to-end through parse_document


async def test_worker_no_prior_uploads_emits_no_cross_statement_flags(
    monkeypatch: pytest.MonkeyPatch, fake_pdf: Path
) -> None:
    """First upload for a merchant: no priors → ``cross_statement_patterns``
    is empty on PipelineResult, no log line, parse succeeds normally."""
    docs_repo = InMemoryDocumentRepository()
    merchants_repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = merchants_repo.create_provisional()

    row = docs_repo.create_document(
        file_hash=_real_file_hash(fake_pdf),
        byte_size=fake_pdf.stat().st_size,
        original_filename="stmt.pdf",
        merchant_id=merchant.id,
    )

    fake_result = _build_pipeline_result(account_holder="Acme LLC")
    monkeypatch.setattr(
        "aegis.workers.run_pipeline",
        lambda _p, _l, today=None: fake_result,
    )

    await parse_document(
        {
            "repository": docs_repo,
            "audit": audit,
            "llm": object(),
            "merchants": merchants_repo,
        },
        str(row.id),
        str(fake_pdf),
    )

    assert fake_result.cross_statement_patterns == []


async def test_worker_duplicate_sha_fires_duplicate_pdf_flag(
    monkeypatch: pytest.MonkeyPatch, fake_pdf: Path
) -> None:
    """A prior document with the SAME ``sha256_original`` triggers the
    duplicate_pdf_upload flag (severity 0, shadow-only)."""
    docs_repo = InMemoryDocumentRepository()
    merchants_repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = merchants_repo.create_provisional()

    # Seed a PRIOR document for the same merchant. Production sets
    # sha256_original via the chunk-B storage step; the in-memory repo
    # accepts the same field via persist_storage_metadata.
    shared_sha = "d" * 64
    prior = docs_repo.create_document(
        file_hash="prior" + "0" * 59,
        byte_size=100,
        original_filename="prior.pdf",
        merchant_id=merchant.id,
    )
    docs_repo.persist_storage_metadata(
        prior.id,
        storage_path="merchants/x/documents/prior.pdf.enc",
        sha256_original=shared_sha,
        encryption_key_version=1,
        retention_until=datetime.now(UTC),
    )

    # Current document: byte-identical sha256_original. We populate the
    # current doc's sha256_original by hand BEFORE parse runs so the
    # detector sees a matching pair.
    current = docs_repo.create_document(
        file_hash=_real_file_hash(fake_pdf),
        byte_size=fake_pdf.stat().st_size,
        original_filename="current.pdf",
        merchant_id=merchant.id,
    )
    docs_repo.persist_storage_metadata(
        current.id,
        storage_path="merchants/x/documents/current.pdf.enc",
        sha256_original=shared_sha,
        encryption_key_version=1,
        retention_until=datetime.now(UTC),
    )

    fake_result = _build_pipeline_result(account_holder="Different Co")
    monkeypatch.setattr(
        "aegis.workers.run_pipeline",
        lambda _p, _l, today=None: fake_result,
    )

    await parse_document(
        {
            "repository": docs_repo,
            "audit": audit,
            "llm": object(),
            "merchants": merchants_repo,
        },
        str(current.id),
        str(fake_pdf),
    )

    codes = [p.code for p in fake_result.cross_statement_patterns]
    assert "duplicate_pdf_upload" in codes
    # Shadow-only invariant — U12 contract.
    for p in fake_result.cross_statement_patterns:
        assert p.severity == 0


async def test_worker_same_holder_new_last4_fires_related_account(
    monkeypatch: pytest.MonkeyPatch, fake_pdf: Path
) -> None:
    """Prior analysis with the same ``account_holder`` but DIFFERENT
    ``account_last4`` fires the related_account_suspected flag.

    This is the load-bearing test for migration 041 — the detector
    reads ``account_holder`` off the PRIOR ``AnalysisRow``. Without
    the new column, the prior row has no holder and the detector
    can't match.
    """
    docs_repo = InMemoryDocumentRepository()
    merchants_repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = merchants_repo.create_provisional()

    # Seed a PRIOR document + its analysis. The prior was uploaded with
    # account_last4="9999" and holder "Acme LLC".
    prior_doc = docs_repo.create_document(
        file_hash="prior" + "0" * 59,
        byte_size=100,
        original_filename="prior.pdf",
        merchant_id=merchant.id,
    )
    # Drive the prior's analysis row through the normal persist path so
    # the round-trip exercises the migration-041 field end-to-end.
    prior_result = _build_pipeline_result(
        bank_name="Chase",
        account_holder="Acme LLC",
        account_last4="9999",
    )
    docs_repo.persist_parse_result(
        prior_doc.id, result=prior_result, merchant_id=merchant.id
    )
    prior_analysis = docs_repo.get_analysis(prior_doc.id)
    assert prior_analysis is not None
    assert prior_analysis.account_holder == "Acme LLC"
    assert prior_analysis.account_last4 == "9999"

    # Current upload: SAME holder, NEW last4.
    current = docs_repo.create_document(
        file_hash=_real_file_hash(fake_pdf),
        byte_size=fake_pdf.stat().st_size,
        original_filename="current.pdf",
        merchant_id=merchant.id,
    )

    fake_result = _build_pipeline_result(
        bank_name="Chase",
        account_holder="Acme LLC",
        account_last4="1234",
    )
    monkeypatch.setattr(
        "aegis.workers.run_pipeline",
        lambda _p, _l, today=None: fake_result,
    )

    await parse_document(
        {
            "repository": docs_repo,
            "audit": audit,
            "llm": object(),
            "merchants": merchants_repo,
        },
        str(current.id),
        str(fake_pdf),
    )

    codes = [p.code for p in fake_result.cross_statement_patterns]
    assert "related_account_suspected" in codes
    for p in fake_result.cross_statement_patterns:
        assert p.severity == 0


async def test_worker_both_detectors_fire_together(
    monkeypatch: pytest.MonkeyPatch, fake_pdf: Path
) -> None:
    """Construct a prior + current that fires BOTH sub-detectors at
    once. The orchestrator concatenates the Pattern lists; both codes
    should land on PipelineResult.cross_statement_patterns.

    Setup:
      * Prior doc P1: sha256=SHARED, analysis holder="Acme LLC", last4="9999".
      * Prior doc P2: sha256=other, no analysis (just a doc).
      * Current: sha256=SHARED → duplicate fires off P1.
                 holder="Acme LLC", last4="1234" → related-account fires off P1.
    """
    docs_repo = InMemoryDocumentRepository()
    merchants_repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = merchants_repo.create_provisional()
    shared_sha = "e" * 64

    # Prior P1: parsed analysis with the shared bank identity.
    p1 = docs_repo.create_document(
        file_hash="p1" + "0" * 62,
        byte_size=100,
        original_filename="p1.pdf",
        merchant_id=merchant.id,
    )
    docs_repo.persist_storage_metadata(
        p1.id,
        storage_path="merchants/x/documents/p1.pdf.enc",
        sha256_original=shared_sha,
        encryption_key_version=1,
        retention_until=datetime.now(UTC),
    )
    docs_repo.persist_parse_result(
        p1.id,
        result=_build_pipeline_result(
            account_holder="Acme LLC", account_last4="9999"
        ),
        merchant_id=merchant.id,
    )

    # Current: same sha as P1 + same holder, new last4.
    current = docs_repo.create_document(
        file_hash=_real_file_hash(fake_pdf),
        byte_size=fake_pdf.stat().st_size,
        original_filename="current.pdf",
        merchant_id=merchant.id,
    )
    docs_repo.persist_storage_metadata(
        current.id,
        storage_path="merchants/x/documents/current.pdf.enc",
        sha256_original=shared_sha,
        encryption_key_version=1,
        retention_until=datetime.now(UTC),
    )

    fake_result = _build_pipeline_result(
        account_holder="Acme LLC", account_last4="1234"
    )
    monkeypatch.setattr(
        "aegis.workers.run_pipeline",
        lambda _p, _l, today=None: fake_result,
    )

    await parse_document(
        {
            "repository": docs_repo,
            "audit": audit,
            "llm": object(),
            "merchants": merchants_repo,
        },
        str(current.id),
        str(fake_pdf),
    )

    codes = {p.code for p in fake_result.cross_statement_patterns}
    assert "duplicate_pdf_upload" in codes
    assert "related_account_suspected" in codes
    for p in fake_result.cross_statement_patterns:
        assert p.severity == 0


# ---------------------------------------------------------------------------
# U22 — worker hook persistence to merchants_shadow_signals (migration 044)


async def test_worker_persists_one_shadow_signal_row_per_emitted_pattern(
    monkeypatch: pytest.MonkeyPatch, fake_pdf: Path
) -> None:
    """After the worker hook fires, ``merchants_shadow_signals`` has
    ONE row per emitted Pattern. Setup mirrors
    ``test_worker_both_detectors_fire_together`` so both sub-detectors
    fire; the assertion is on the persisted side rather than the
    in-memory PipelineResult.

    Per U22: code = pattern.code, severity = pattern.severity (always
    0), detail = pattern.detail, source_document_id = current doc,
    source_ids = pattern.source_ids,
    metadata = {"emitted_by": "cross_statement_detector"}.
    """
    docs_repo = InMemoryDocumentRepository()
    merchants_repo = InMemoryMerchantRepository()
    shadow_signals_repo = InMemoryMerchantShadowSignalRepository()
    audit = InMemoryAuditLog()
    merchant = merchants_repo.create_provisional()
    shared_sha = "f" * 64

    # Prior doc with the shared SHA + analysis holder collision.
    p1 = docs_repo.create_document(
        file_hash="p1" + "0" * 62,
        byte_size=100,
        original_filename="p1.pdf",
        merchant_id=merchant.id,
    )
    docs_repo.persist_storage_metadata(
        p1.id,
        storage_path="merchants/x/documents/p1.pdf.enc",
        sha256_original=shared_sha,
        encryption_key_version=1,
        retention_until=datetime.now(UTC),
    )
    docs_repo.persist_parse_result(
        p1.id,
        result=_build_pipeline_result(
            account_holder="Acme LLC", account_last4="9999"
        ),
        merchant_id=merchant.id,
    )

    # Current upload: shared SHA, same holder, NEW last4 → both detectors fire.
    current = docs_repo.create_document(
        file_hash=_real_file_hash(fake_pdf),
        byte_size=fake_pdf.stat().st_size,
        original_filename="current.pdf",
        merchant_id=merchant.id,
    )
    docs_repo.persist_storage_metadata(
        current.id,
        storage_path="merchants/x/documents/current.pdf.enc",
        sha256_original=shared_sha,
        encryption_key_version=1,
        retention_until=datetime.now(UTC),
    )

    fake_result = _build_pipeline_result(
        account_holder="Acme LLC", account_last4="1234"
    )
    monkeypatch.setattr(
        "aegis.workers.run_pipeline",
        lambda _p, _l, today=None: fake_result,
    )

    await parse_document(
        {
            "repository": docs_repo,
            "audit": audit,
            "llm": object(),
            "merchants": merchants_repo,
            "shadow_signals": shadow_signals_repo,
        },
        str(current.id),
        str(fake_pdf),
    )

    rows = shadow_signals_repo.list_by_merchant(merchant_id=merchant.id)
    codes = sorted(r.signal_code for r in rows)
    assert codes == ["duplicate_pdf_upload", "related_account_suspected"]
    for r in rows:
        # Shadow-only contract.
        assert r.signal_severity == 0
        assert r.source_document_id == current.id
        assert r.metadata == {"emitted_by": "cross_statement_detector"}
        # source_ids non-empty — both sub-detectors point at prior docs.
        assert len(r.source_ids) >= 1
        # detail is non-empty per the U12 detector contract.
        assert r.detail is not None and len(r.detail) > 0

    # Audit log carries one shadow_signal_detected entry per row, code-only.
    actions = [
        e for e in audit.entries if e["action"] == "shadow_signal_detected"
    ]
    assert len(actions) == 2
    for entry in actions:
        assert entry["subject_type"] == "merchant"
        assert entry["subject_id"] == str(merchant.id)
        # PII canary: details carries CODE / severity / source_document_id
        # only — never the raw Pattern.detail string.
        assert "detail" not in entry["details"]
        assert "holder" not in repr(entry["details"]).lower()


async def test_worker_persistence_supabase_failure_does_not_abort_upload(
    monkeypatch: pytest.MonkeyPatch, fake_pdf: Path
) -> None:
    """A Supabase blip on ``record_shadow_signal`` MUST NOT raise out of
    the worker. The parse + persist already succeeded by then; the
    cross-statement Pattern list is informational shadow data per the
    U12 contract.

    We inject a shadow-signals repo whose ``record`` always raises and
    assert ``parse_document`` returns normally with the in-memory
    PipelineResult still carrying the Pattern list (so the operator
    surface in this session is degraded but not lost).
    """
    docs_repo = InMemoryDocumentRepository()
    merchants_repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = merchants_repo.create_provisional()
    shared_sha = "c" * 64

    # Seed a duplicate-fire setup.
    prior = docs_repo.create_document(
        file_hash="prior" + "0" * 59,
        byte_size=100,
        original_filename="prior.pdf",
        merchant_id=merchant.id,
    )
    docs_repo.persist_storage_metadata(
        prior.id,
        storage_path="merchants/x/documents/prior.pdf.enc",
        sha256_original=shared_sha,
        encryption_key_version=1,
        retention_until=datetime.now(UTC),
    )

    current = docs_repo.create_document(
        file_hash=_real_file_hash(fake_pdf),
        byte_size=fake_pdf.stat().st_size,
        original_filename="current.pdf",
        merchant_id=merchant.id,
    )
    docs_repo.persist_storage_metadata(
        current.id,
        storage_path="merchants/x/documents/current.pdf.enc",
        sha256_original=shared_sha,
        encryption_key_version=1,
        retention_until=datetime.now(UTC),
    )

    fake_result = _build_pipeline_result(account_holder="Acme LLC")
    monkeypatch.setattr(
        "aegis.workers.run_pipeline",
        lambda _p, _l, today=None: fake_result,
    )

    # Broken repository: every record() raises. The worker hook must
    # swallow and continue.
    class _BrokenRepo:
        def record(self, **_kwargs: Any) -> Any:
            raise RuntimeError("simulated supabase failure")

        def list_by_merchant(self, **_kwargs: Any) -> list[Any]:
            return []

        def list_by_code(self, **_kwargs: Any) -> list[Any]:
            return []

    broken_repo = _BrokenRepo()

    result = await parse_document(
        {
            "repository": docs_repo,
            "audit": audit,
            "llm": object(),
            "merchants": merchants_repo,
            "shadow_signals": broken_repo,
        },
        str(current.id),
        str(fake_pdf),
    )

    # Upload succeeded.
    assert result["parse_status"] == "proceed"
    # In-memory channel still carries the Pattern list — the operator
    # surface degrades gracefully when the durable channel is offline.
    codes = [p.code for p in fake_result.cross_statement_patterns]
    assert "duplicate_pdf_upload" in codes


