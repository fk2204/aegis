"""Unit tests for the auto-trigger narrator path.

Covers both halves of :mod:`aegis.scoring_v2.narrator_job`:

* :func:`enqueue_narrator_summary_from_worker` — the fire-and-forget
  helper :func:`aegis.workers.parse_document` calls at the tail of every
  successful parse.
* :func:`generate_narrator_summary` — the arq worker function that
  builds a NarratorContext, calls Bedrock, persists the result.

Bedrock is mocked via direct monkeypatch of :func:`narrate_deal` so no
real LLM traffic is touched. The InMemory document + merchant
repositories are real (no Supabase) so the analyses persistence path
exercises the same code prod uses.
"""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from aegis.audit import InMemoryAuditLog
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.parser.pipeline import PipelineResult
from aegis.scoring_v2.narrator import (
    NarratorError,
    NarratorSummary,
    RecommendedAction,
)
from aegis.scoring_v2.narrator_job import (
    enqueue_narrator_summary_from_worker,
    generate_narrator_summary,
)
from aegis.storage import InMemoryDocumentRepository
from aegis.workers import parse_document
from tests.test_storage import _make_pipeline_result

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def docs() -> InMemoryDocumentRepository:
    return InMemoryDocumentRepository()


@pytest.fixture
def merchants() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture
def fake_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "f.pdf"
    p.write_bytes(b"%PDF-1.4\nfake")
    return p


def _real_file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_merchant(repo: InMemoryMerchantRepository) -> MerchantRow:
    row = MerchantRow(
        id=uuid4(),
        business_name="Acme Industries LLC",
        state="CA",
        owner_name="A. Operator",
    )
    repo.upsert(row)
    return row


def _seed_doc_with_analysis(
    repo: InMemoryDocumentRepository,
    merchant_id: UUID,
    *,
    narrator_summary: dict[str, Any] | None = None,
) -> UUID:
    row = repo.create_document(
        file_hash="a" * 64,
        byte_size=4096,
        original_filename="stmt.pdf",
        merchant_id=merchant_id,
    )
    repo.persist_parse_result(row.id, result=_make_pipeline_result(), merchant_id=merchant_id)
    if narrator_summary is not None:
        repo.set_narrator_summary(row.id, narrator_summary)
    return row.id


def _fake_summary() -> NarratorSummary:
    return NarratorSummary(
        deal_summary="Sample deal summary for tests.",
        flag_explanations=(),
        recommended_action=RecommendedAction(
            action="submit_now",
            next_step="Send to top funder match.",
        ),
        model_id="us.anthropic.claude-sonnet-4-6",
        generated_at=datetime.now(UTC),
        version=1,
    )


# ===========================================================================
# enqueue_narrator_summary_from_worker — direct unit tests
# ===========================================================================


@pytest.mark.asyncio
async def test_enqueue_appends_to_pending_when_no_redis_in_ctx(
    audit: InMemoryAuditLog,
) -> None:
    """No ``redis`` key in ctx → helper writes to ``pending_narrator_jobs``
    and audits ``narrator.enqueued`` exactly once. Mirrors the in-process
    fallback pattern used by ``_orchestrator_enqueue``."""
    ctx: dict[str, Any] = {}
    doc_id = uuid4()
    merch_id = uuid4()

    ok = await enqueue_narrator_summary_from_worker(
        ctx=ctx,
        document_id=doc_id,
        merchant_id=merch_id,
        audit=audit,
    )

    assert ok is True
    assert ctx["pending_narrator_jobs"] == [
        {"document_id": str(doc_id), "merchant_id": str(merch_id)},
    ]
    enqueued = [e for e in audit.entries if e["action"] == "narrator.enqueued"]
    assert len(enqueued) == 1
    assert enqueued[0]["details"]["merchant_id"] == str(merch_id)


@pytest.mark.asyncio
async def test_enqueue_audits_failure_when_redis_raises(
    audit: InMemoryAuditLog,
) -> None:
    """Redis present but raises on ``enqueue_job`` → helper writes a
    ``narrator.enqueue_failed`` row and returns False without re-raising.
    Parse return path must stay unaffected."""

    class _BoomRedis:
        async def enqueue_job(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("redis down")

    ctx: dict[str, Any] = {"redis": _BoomRedis()}

    ok = await enqueue_narrator_summary_from_worker(
        ctx=ctx,
        document_id=uuid4(),
        merchant_id=uuid4(),
        audit=audit,
    )

    assert ok is False
    failures = [e for e in audit.entries if e["action"] == "narrator.enqueue_failed"]
    assert len(failures) == 1
    assert failures[0]["details"]["error"] == "RuntimeError"


# ===========================================================================
# parse_document call-site gating — proceed vs manual_review vs error
# ===========================================================================


@pytest.mark.asyncio
async def test_parse_proceed_enqueues_narrator_via_pending_jobs(
    monkeypatch: pytest.MonkeyPatch,
    audit: InMemoryAuditLog,
    merchants: InMemoryMerchantRepository,
    fake_pdf: Path,
) -> None:
    """End-to-end through ``parse_document``: a proceed-status parse on a
    merchant-attached doc lands a pending narrator job + a
    ``narrator.enqueued`` audit row."""
    repo = InMemoryDocumentRepository()
    merchant = _seed_merchant(merchants)
    row = repo.create_document(
        file_hash=_real_file_hash(fake_pdf),
        byte_size=fake_pdf.stat().st_size,
        original_filename="f.pdf",
        merchant_id=merchant.id,
    )

    fake_result = _make_pipeline_result()  # parse_status="proceed"
    monkeypatch.setattr(
        "aegis.workers.run_pipeline",
        lambda _path, _llm, today=None: fake_result,
    )

    ctx: dict[str, Any] = {
        "repository": repo,
        "audit": audit,
        "llm": object(),
        "merchants": merchants,
    }
    out = await parse_document(ctx, str(row.id), str(fake_pdf))

    assert out["parse_status"] == "proceed"
    pending = ctx.get("pending_narrator_jobs") or []
    assert len(pending) == 1
    assert pending[0]["document_id"] == str(row.id)
    assert pending[0]["merchant_id"] == str(merchant.id)
    enqueued = [e for e in audit.entries if e["action"] == "narrator.enqueued"]
    assert len(enqueued) == 1


@pytest.mark.asyncio
async def test_parse_manual_review_does_not_enqueue_narrator(
    monkeypatch: pytest.MonkeyPatch,
    audit: InMemoryAuditLog,
    merchants: InMemoryMerchantRepository,
    fake_pdf: Path,
) -> None:
    """A ``manual_review`` outcome MUST NOT enqueue the narrator (we don't
    auto-narrate on questionable data — operator opens the dossier and
    clicks Refresh narrator manually)."""
    repo = InMemoryDocumentRepository()
    merchant = _seed_merchant(merchants)
    row = repo.create_document(
        file_hash=_real_file_hash(fake_pdf),
        byte_size=fake_pdf.stat().st_size,
        original_filename="f.pdf",
        merchant_id=merchant.id,
    )

    base = _make_pipeline_result()
    manual_result = dataclasses.replace(base, parse_status="manual_review")
    monkeypatch.setattr(
        "aegis.workers.run_pipeline",
        lambda _path, _llm, today=None: manual_result,
    )

    ctx: dict[str, Any] = {
        "repository": repo,
        "audit": audit,
        "llm": object(),
        "merchants": merchants,
    }
    out = await parse_document(ctx, str(row.id), str(fake_pdf))

    assert out["parse_status"] == "manual_review"
    assert "pending_narrator_jobs" not in ctx or ctx["pending_narrator_jobs"] == []
    enqueued = [e for e in audit.entries if e["action"] == "narrator.enqueued"]
    assert enqueued == []


@pytest.mark.asyncio
async def test_parse_error_does_not_enqueue_narrator(
    monkeypatch: pytest.MonkeyPatch,
    audit: InMemoryAuditLog,
    merchants: InMemoryMerchantRepository,
    fake_pdf: Path,
) -> None:
    """Pipeline raises → ``parse_status='error'`` recorded, helper not
    reached at all. (The exception itself propagates per the existing
    contract; we just assert no narrator side-effects landed.)"""
    repo = InMemoryDocumentRepository()
    merchant = _seed_merchant(merchants)
    row = repo.create_document(
        file_hash="i" * 64,
        byte_size=10,
        original_filename="f.pdf",
        merchant_id=merchant.id,
    )

    def _boom(_path: str, _llm: object, today: date | None = None) -> PipelineResult:
        raise RuntimeError("parser blew up")

    monkeypatch.setattr("aegis.workers.run_pipeline", _boom)

    ctx: dict[str, Any] = {
        "repository": repo,
        "audit": audit,
        "llm": object(),
        "merchants": merchants,
    }
    with pytest.raises(RuntimeError, match="parser blew up"):
        await parse_document(ctx, str(row.id), str(fake_pdf))

    assert "pending_narrator_jobs" not in ctx or ctx["pending_narrator_jobs"] == []
    enqueued = [e for e in audit.entries if e["action"] == "narrator.enqueued"]
    assert enqueued == []


# ===========================================================================
# generate_narrator_summary — worker-side behavior
# ===========================================================================


@pytest.mark.asyncio
async def test_generate_summary_skipped_when_already_set(
    audit: InMemoryAuditLog,
    docs: InMemoryDocumentRepository,
    merchants: InMemoryMerchantRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-running the job on a document that already has a non-null
    ``narrator_summary`` MUST skip silently — audit ``narrator.skipped_existing``
    written, no Bedrock call."""
    merchant = _seed_merchant(merchants)
    doc_id = _seed_doc_with_analysis(
        docs,
        merchant.id,
        narrator_summary={"deal_summary": "already done", "version": 1},
    )

    narrate_calls: list[object] = []

    def _stub_narrate(*_args: object, **_kwargs: object) -> NarratorSummary:
        narrate_calls.append("called")
        return _fake_summary()

    monkeypatch.setattr("aegis.scoring_v2.narrator_job.narrate_deal", _stub_narrate)

    ctx: dict[str, Any] = {
        "audit": audit,
        "docs": docs,
        "merchants": merchants,
        "bedrock": object(),
    }
    result = await generate_narrator_summary(ctx, str(doc_id), str(merchant.id))

    assert result["skipped"] is True
    assert result["reason"] == "narrator_already_set"
    assert narrate_calls == []
    skipped = [e for e in audit.entries if e["action"] == "narrator.skipped_existing"]
    assert len(skipped) == 1
    # And NO ``narrator.generated`` row.
    assert not [e for e in audit.entries if e["action"] == "narrator.generated"]


@pytest.mark.asyncio
async def test_generate_summary_invalid_uuid_returns_early(
    audit: InMemoryAuditLog,
    docs: InMemoryDocumentRepository,
    merchants: InMemoryMerchantRepository,
) -> None:
    """Bad UUID strings (poisoned payload) short-circuit without
    raising or auditing."""
    ctx: dict[str, Any] = {"audit": audit, "docs": docs, "merchants": merchants}
    result = await generate_narrator_summary(ctx, "not-a-uuid", "still-not")

    assert result["skipped"] is True
    assert result["reason"] == "invalid_uuid"
    assert audit.entries == []


@pytest.mark.asyncio
async def test_generate_summary_audits_failure_on_narrator_error(
    audit: InMemoryAuditLog,
    docs: InMemoryDocumentRepository,
    merchants: InMemoryMerchantRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """:class:`NarratorError` from ``narrate_deal`` MUST audit
    ``narrator.failed`` and return normally — the calling parse worker's
    ``parse_status`` is durable and must never be retroactively affected."""
    merchant = _seed_merchant(merchants)
    doc_id = _seed_doc_with_analysis(docs, merchant.id)

    def _raise(*_args: object, **_kwargs: object) -> NarratorSummary:
        raise NarratorError("bedrock_call_failed: 500")

    monkeypatch.setattr("aegis.scoring_v2.narrator_job.narrate_deal", _raise)

    # Stub the helpers that build the context — the test wants to assert
    # the failure-handling boundary, not the score-input plumbing.
    monkeypatch.setattr(
        "aegis.scoring_v2.narrator_job._collect_analyzed_for_merchant"
        if False
        else "aegis.web._router_helpers._collect_analyzed_for_merchant",
        lambda *_args, **_kw: [(docs.get_document(doc_id), docs.get_analysis(doc_id))],
    )
    monkeypatch.setattr(
        "aegis.web.routers.merchants._dossier_pattern_analysis",
        lambda *_args, **_kw: None,
    )
    monkeypatch.setattr(
        "aegis.scoring.multi_month.score_input_multi_month",
        lambda *_args, **_kw: object(),
    )
    monkeypatch.setattr(
        "aegis.scoring_v2.score_deal_inputs.compute_score_deal_track_inputs",
        lambda **_kw: (None, None),
    )
    monkeypatch.setattr(
        "aegis.scoring.score.score_deal",
        lambda *_args, **_kw: object(),
    )
    monkeypatch.setattr(
        "aegis.scoring_v2.mca_stack.aggregate_mca_stack",
        lambda **_kw: None,
    )
    monkeypatch.setattr(
        "aegis.scoring_v2.balance_health.compute_balance_health",
        lambda **_kw: None,
    )
    monkeypatch.setattr(
        "aegis.scoring_v2.industry.industry_risk_tier",
        lambda _industry: 0,
    )

    ctx: dict[str, Any] = {
        "audit": audit,
        "docs": docs,
        "merchants": merchants,
        "bedrock": object(),
    }
    result = await generate_narrator_summary(ctx, str(doc_id), str(merchant.id))

    assert result["generated"] is False
    assert result["reason"] == "narrator_error"
    failed = [e for e in audit.entries if e["action"] == "narrator.failed"]
    assert len(failed) == 1
    assert failed[0]["details"]["error"] == "NarratorError"
    # The previously-stored narrator_summary (none in this fixture) must
    # NOT have been written. Confirm via the repo.
    analysis = docs.get_analysis(doc_id)
    assert analysis is not None
    assert analysis.narrator_summary is None


@pytest.mark.asyncio
async def test_generate_summary_no_analysis_row_audits_and_returns(
    audit: InMemoryAuditLog,
    docs: InMemoryDocumentRepository,
    merchants: InMemoryMerchantRepository,
) -> None:
    """Race condition: enqueue happened but the analyses row write didn't
    land yet. Worker audits ``narrator.skipped_no_analysis`` and exits."""
    merchant = _seed_merchant(merchants)
    # Create a document but NO analysis (don't call persist_parse_result).
    row = docs.create_document(
        file_hash="b" * 64,
        byte_size=4096,
        original_filename="stmt.pdf",
        merchant_id=merchant.id,
    )

    ctx: dict[str, Any] = {"audit": audit, "docs": docs, "merchants": merchants}
    result = await generate_narrator_summary(ctx, str(row.id), str(merchant.id))

    assert result["skipped"] is True
    assert result["reason"] == "no_analysis"
    skipped = [e for e in audit.entries if e["action"] == "narrator.skipped_no_analysis"]
    assert len(skipped) == 1
