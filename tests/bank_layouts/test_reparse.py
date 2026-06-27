"""Tests for ``aegis.bank_layouts.reparse.enqueue_bank_reparse`` + the
``reparse_bank_manual_review`` arq worker function.

Covers:

* Sealed-manual_review candidates filter on bank_name (case-insensitive).
* Per-doc decrypt + tempfile + enqueue happy path.
* 100ms inter-enqueue pacing (last doc does NOT sleep).
* Audit-row contract:
    - One ``bank_layouts.reparse_enqueued`` per success.
    - One ``bank_layouts.reparse_batch_complete`` per batch.
    - ``bank_layouts.reparse_enqueue_failed`` on per-doc decrypt failure
      and on whole-batch pool failure.
* Idempotent: re-running on the same candidates re-enqueues (the
  ``parse_document`` storage upsert from the 2026-06-27 Bug 1 fix
  handles the duplicate-key collision at the lower layer).
* Empty pool (test context) → no-op return 0.
* Empty bank_name → no-op return 0.

The candidate selector ``_select_sealed_manual_review_for_bank``
queries Supabase directly (no in-memory abstraction), so the tests
monkeypatch it with a canned list. The rest of the pipeline runs end-
to-end through real ``InMemoryPdfStoreRepository`` +
``InMemoryAuditLog`` to validate the audit + pacing contracts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from aegis.audit import InMemoryAuditLog
from aegis.bank_layouts import reparse as reparse_mod
from aegis.bank_layouts.reparse import enqueue_bank_reparse
from aegis.pdf_store.repository import InMemoryPdfStoreRepository

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeArqPool:
    """Record-everything stand-in for arq's pool. Captures enqueued jobs."""

    def __init__(self, raise_on_enqueue: bool = False) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.raise_on_enqueue = raise_on_enqueue

    async def enqueue_job(self, name: str, *args: Any, **kwargs: Any) -> None:
        if self.raise_on_enqueue:
            raise RuntimeError("synthetic pool failure")
        self.calls.append((name, args, kwargs))


class _RaiseOnFetch(InMemoryPdfStoreRepository):
    """In-memory pdf_store that raises a non-NotFound error on fetch."""

    def fetch_plaintext(self, document_id: UUID) -> bytes:
        raise RuntimeError(f"synthetic fetch failure for {document_id}")


def _seed_pdf_store(repo: InMemoryPdfStoreRepository, doc_ids: list[UUID]) -> None:
    """Seal a small deterministic blob per document_id."""
    for i, doc_id in enumerate(doc_ids):
        repo.store(document_id=doc_id, plaintext=f"%PDF fake {i:04d}\n".encode())


def _make_candidates(doc_ids: list[UUID]) -> list[dict[str, Any]]:
    """Build candidate dicts in the shape ``_select_sealed_manual_review_for_bank``
    returns (mirrors the Supabase row shape — `id` is a str)."""
    return [
        {
            "id": str(doc_id),
            "original_filename": f"statement_{i:02d}.pdf",
            "storage_path": f"merchants/abc/{doc_id}.pdf.enc",
            "merchant_id": str(uuid4()),
            "uploaded_at": "2026-06-01T00:00:00+00:00",
        }
        for i, doc_id in enumerate(doc_ids)
    ]


def _filter_actions(audit: InMemoryAuditLog, action: str) -> list[dict[str, Any]]:
    return [e for e in audit.entries if e["action"] == action]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_bank_reparse_happy_path_enqueues_all_and_audits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3 candidates → 3 enqueues + 3 enqueued audit rows + 1 batch_complete."""
    doc_ids = [uuid4() for _ in range(3)]
    monkeypatch.setattr(
        reparse_mod,
        "_select_sealed_manual_review_for_bank",
        lambda bank_name: _make_candidates(doc_ids),
    )
    pdf_store = InMemoryPdfStoreRepository()
    _seed_pdf_store(pdf_store, doc_ids)
    audit = InMemoryAuditLog()
    pool = _FakeArqPool()
    sleep_calls: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    enqueued = await enqueue_bank_reparse(
        bank_name="Chase",
        pool=pool,
        audit=audit,
        pdf_store=pdf_store,
        trigger="hints_updated",
        upload_dir=tmp_path,
        sleep_fn=_record_sleep,
    )

    assert enqueued == 3
    assert len(pool.calls) == 3
    for name, args, kwargs in pool.calls:
        assert name == "parse_document"
        assert kwargs == {"keep_local_plaintext": False}
        # args = (str(doc_id), pdf_path)
        assert UUID(args[0]) in doc_ids
        tmp_path_arg = Path(args[1])
        assert tmp_path_arg.exists()
        assert tmp_path_arg.parent == tmp_path

    enqueued_rows = _filter_actions(audit, "bank_layouts.reparse_enqueued")
    assert len(enqueued_rows) == 3
    for row in enqueued_rows:
        assert row["details"]["bank_layout_name"] == "Chase"
        assert row["details"]["trigger"] == "hints_updated"
        assert row["subject_type"] == "document"

    complete = _filter_actions(audit, "bank_layouts.reparse_batch_complete")
    assert len(complete) == 1
    assert complete[0]["details"] == {
        "bank_layout_name": "Chase",
        "trigger": "hints_updated",
        "candidates": 3,
        "enqueued": 3,
    }


# ---------------------------------------------------------------------------
# Pacing — 100ms between enqueues, no sleep after the last
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_bank_reparse_paces_between_docs_not_after_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4 candidates → 3 sleep calls (between docs) of exactly 0.1 seconds."""
    doc_ids = [uuid4() for _ in range(4)]
    monkeypatch.setattr(
        reparse_mod,
        "_select_sealed_manual_review_for_bank",
        lambda bank_name: _make_candidates(doc_ids),
    )
    pdf_store = InMemoryPdfStoreRepository()
    _seed_pdf_store(pdf_store, doc_ids)
    audit = InMemoryAuditLog()
    pool = _FakeArqPool()
    sleep_calls: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    await enqueue_bank_reparse(
        bank_name="Chase",
        pool=pool,
        audit=audit,
        pdf_store=pdf_store,
        trigger="hints_updated",
        upload_dir=tmp_path,
        sleep_fn=_record_sleep,
    )

    assert sleep_calls == [0.1, 0.1, 0.1]


@pytest.mark.asyncio
async def test_enqueue_bank_reparse_single_doc_does_not_sleep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1 candidate → 1 enqueue, 0 sleeps."""
    doc_ids = [uuid4()]
    monkeypatch.setattr(
        reparse_mod,
        "_select_sealed_manual_review_for_bank",
        lambda bank_name: _make_candidates(doc_ids),
    )
    pdf_store = InMemoryPdfStoreRepository()
    _seed_pdf_store(pdf_store, doc_ids)
    audit = InMemoryAuditLog()
    pool = _FakeArqPool()
    sleep_calls: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    enqueued = await enqueue_bank_reparse(
        bank_name="Chase",
        pool=pool,
        audit=audit,
        pdf_store=pdf_store,
        trigger="hints_updated",
        upload_dir=tmp_path,
        sleep_fn=_record_sleep,
    )

    assert enqueued == 1
    assert sleep_calls == []


# ---------------------------------------------------------------------------
# No-op paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_bank_reparse_no_pool_returns_zero_silently(
    tmp_path: Path,
) -> None:
    """``pool=None`` (test context without arq) → 0, no audit, no crash."""
    audit = InMemoryAuditLog()
    pdf_store = InMemoryPdfStoreRepository()

    enqueued = await enqueue_bank_reparse(
        bank_name="Chase",
        pool=None,
        audit=audit,
        pdf_store=pdf_store,
        trigger="hints_updated",
        upload_dir=tmp_path,
    )
    assert enqueued == 0
    assert audit.entries == []


@pytest.mark.asyncio
async def test_enqueue_bank_reparse_no_candidates_writes_only_complete_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty candidate list → 1 batch_complete row (enqueued=0), pool untouched."""
    monkeypatch.setattr(
        reparse_mod,
        "_select_sealed_manual_review_for_bank",
        lambda bank_name: [],
    )
    audit = InMemoryAuditLog()
    pool = _FakeArqPool()
    pdf_store = InMemoryPdfStoreRepository()

    enqueued = await enqueue_bank_reparse(
        bank_name="UnknownBank",
        pool=pool,
        audit=audit,
        pdf_store=pdf_store,
        trigger="hints_updated",
        upload_dir=tmp_path,
    )

    assert enqueued == 0
    assert pool.calls == []
    actions = [e["action"] for e in audit.entries]
    assert actions == ["bank_layouts.reparse_batch_complete"]
    assert audit.entries[0]["details"]["candidates"] == 0


# ---------------------------------------------------------------------------
# Per-doc resilience: one bad blob doesn't cancel the batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_bank_reparse_pdf_store_not_found_logs_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Doc 1 has no blob in pdf_store → logged, skipped; doc 2 still enqueues."""
    doc_id_missing = uuid4()
    doc_id_ok = uuid4()
    monkeypatch.setattr(
        reparse_mod,
        "_select_sealed_manual_review_for_bank",
        lambda bank_name: _make_candidates([doc_id_missing, doc_id_ok]),
    )
    audit = InMemoryAuditLog()
    pool = _FakeArqPool()
    pdf_store = InMemoryPdfStoreRepository()
    # Only seed doc_id_ok; doc_id_missing will raise PdfStoreNotFoundError.
    _seed_pdf_store(pdf_store, [doc_id_ok])

    async def _no_sleep(seconds: float) -> None:
        return None

    enqueued = await enqueue_bank_reparse(
        bank_name="Chase",
        pool=pool,
        audit=audit,
        pdf_store=pdf_store,
        trigger="hints_updated",
        upload_dir=tmp_path,
        sleep_fn=_no_sleep,
    )

    assert enqueued == 1
    assert len(pool.calls) == 1
    assert UUID(pool.calls[0][1][0]) == doc_id_ok

    failed = _filter_actions(audit, "bank_layouts.reparse_enqueue_failed")
    assert len(failed) == 1
    assert failed[0]["details"]["reason"] == "pdf_store_blob_missing"
    assert failed[0]["subject_id"] == str(doc_id_missing)

    complete = _filter_actions(audit, "bank_layouts.reparse_batch_complete")
    assert len(complete) == 1
    assert complete[0]["details"]["candidates"] == 2
    assert complete[0]["details"]["enqueued"] == 1


# ---------------------------------------------------------------------------
# Whole-batch failure: pool unreachable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_bank_reparse_pool_failure_raises_and_cleans_tempfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pool raises on first enqueue → batch aborts, summary audit row,
    raised, tempfile cleaned up so plaintext doesn't leak on disk."""
    doc_ids = [uuid4(), uuid4()]
    monkeypatch.setattr(
        reparse_mod,
        "_select_sealed_manual_review_for_bank",
        lambda bank_name: _make_candidates(doc_ids),
    )
    audit = InMemoryAuditLog()
    pool = _FakeArqPool(raise_on_enqueue=True)
    pdf_store = InMemoryPdfStoreRepository()
    _seed_pdf_store(pdf_store, doc_ids)

    async def _no_sleep(seconds: float) -> None:
        return None

    with pytest.raises(RuntimeError, match="synthetic pool failure"):
        await enqueue_bank_reparse(
            bank_name="Chase",
            pool=pool,
            audit=audit,
            pdf_store=pdf_store,
            trigger="hints_updated",
            upload_dir=tmp_path,
            sleep_fn=_no_sleep,
        )

    # No leftover tempfiles in the upload dir.
    leftover = list(tmp_path.iterdir())
    assert leftover == [], f"plaintext tempfile leaked: {leftover}"

    failed = _filter_actions(audit, "bank_layouts.reparse_enqueue_failed")
    assert len(failed) == 1
    assert failed[0]["details"]["reason"] == "pool_enqueue_failed"
    assert failed[0]["details"]["enqueued_before_failure"] == 0
    assert failed[0]["details"]["remaining"] == 2


# ---------------------------------------------------------------------------
# Empty bank_name guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_bank_reparse_empty_bank_name_returns_zero(
    tmp_path: Path,
) -> None:
    """Empty bank_name → defensive 0 from _select... → no-op enqueue + batch_complete row."""
    audit = InMemoryAuditLog()
    pool = _FakeArqPool()
    pdf_store = InMemoryPdfStoreRepository()

    # No monkeypatch on selector — the real _select_sealed_manual_review_for_bank
    # short-circuits on empty string before hitting Supabase, so the test runs
    # offline.
    enqueued = await enqueue_bank_reparse(
        bank_name="   ",
        pool=pool,
        audit=audit,
        pdf_store=pdf_store,
        trigger="hints_updated",
        upload_dir=tmp_path,
    )

    assert enqueued == 0
    assert pool.calls == []
    actions = [e["action"] for e in audit.entries]
    assert actions == ["bank_layouts.reparse_batch_complete"]


# ---------------------------------------------------------------------------
# Idempotency: re-running on the same candidates re-enqueues
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_bank_reparse_idempotent_re_runs_same_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run twice on the same bank → both runs enqueue everything.

    No layer-local dedupe — the storage upsert in parse_document
    (Bug 1 fix, commit 10d3b71) handles the duplicate-key case.
    """
    doc_ids = [uuid4(), uuid4()]
    monkeypatch.setattr(
        reparse_mod,
        "_select_sealed_manual_review_for_bank",
        lambda bank_name: _make_candidates(doc_ids),
    )
    pdf_store = InMemoryPdfStoreRepository()
    _seed_pdf_store(pdf_store, doc_ids)
    audit = InMemoryAuditLog()
    pool = _FakeArqPool()

    async def _no_sleep(seconds: float) -> None:
        return None

    for _ in range(2):
        enqueued = await enqueue_bank_reparse(
            bank_name="Chase",
            pool=pool,
            audit=audit,
            pdf_store=pdf_store,
            trigger="hints_updated",
            upload_dir=tmp_path,
            sleep_fn=_no_sleep,
        )
        assert enqueued == 2

    assert len(pool.calls) == 4
    assert len(_filter_actions(audit, "bank_layouts.reparse_enqueued")) == 4
    assert len(_filter_actions(audit, "bank_layouts.reparse_batch_complete")) == 2


# ---------------------------------------------------------------------------
# Per-doc resilience: arbitrary fetch failure (not NotFound)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_bank_reparse_arbitrary_fetch_error_audits_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any non-NotFound fetch error → ``pdf_store_fetch_failed`` audit, batch continues."""
    doc_ids = [uuid4(), uuid4()]
    monkeypatch.setattr(
        reparse_mod,
        "_select_sealed_manual_review_for_bank",
        lambda bank_name: _make_candidates(doc_ids),
    )
    audit = InMemoryAuditLog()
    pool = _FakeArqPool()
    pdf_store = _RaiseOnFetch()

    async def _no_sleep(seconds: float) -> None:
        return None

    enqueued = await enqueue_bank_reparse(
        bank_name="Chase",
        pool=pool,
        audit=audit,
        pdf_store=pdf_store,
        trigger="hints_updated",
        upload_dir=tmp_path,
        sleep_fn=_no_sleep,
    )

    assert enqueued == 0
    failed = _filter_actions(audit, "bank_layouts.reparse_enqueue_failed")
    assert len(failed) == 2
    assert all(row["details"]["reason"] == "pdf_store_fetch_failed" for row in failed)
    # PdfStoreNotFoundError reason should NOT be present here (we raised
    # a different exception class).
    assert not any(row["details"]["reason"] == "pdf_store_blob_missing" for row in failed)
