"""Smoke tests for ``scripts/recover_legacy_docs.py`` (``--reparse-sealed-manual-review``).

Covers the operator-facing flag added in commit ``11fe64d`` (with the
2026-06-24 root-cause leak fix in ``22d3d1e``):

* DRY-RUN with one candidate — no enqueue, no audit, no tempfile.
* --apply with one candidate — tempfile created with mode 0o644 under
  ``upload_dir``, ``_enqueue_parse_jobs`` is invoked exactly once with
  the candidate's (UUID, path) pair, one ``document.reparse_enqueued``
  audit row written.
* --apply where ``pdf_store.fetch_plaintext`` raises — that doc bumps
  the ``issues`` counter; other candidates in the same run still
  process cleanly (fault tolerance).
* --apply with multiple candidates — the pacing ``asyncio.sleep(0.1)``
  fires once per enqueue inside ``_enqueue_parse_jobs``.

``_select_sealed_manual_review_candidates`` is monkeypatched to return a
hardcoded list (avoids a Supabase query). ``_enqueue_parse_jobs`` is
also monkeypatched to a fake async function that records its payload —
the real arq pool is never created.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from aegis.audit import InMemoryAuditLog  # noqa: E402
from aegis.pdf_store.repository import InMemoryPdfStoreRepository  # noqa: E402
from scripts import recover_legacy_docs as recover  # noqa: E402

_SAMPLE_PDF = b"%PDF-1.7\n%fake\n%%EOF\n"


def _make_candidate(
    *,
    doc_id: UUID | None = None,
    filename: str = "stmt.pdf",
    storage_path: str = "merchants/.../doc.pdf.enc",
    merchant_id: UUID | None = None,
) -> dict[str, Any]:
    """Build a candidate row matching the Supabase SELECT shape used by
    ``_select_sealed_manual_review_candidates``."""
    return {
        "id": str(doc_id or uuid4()),
        "original_filename": filename,
        "parse_status": "manual_review",
        "storage_path": storage_path,
        "merchant_id": str(merchant_id or uuid4()),
    }


# ----------------------------------------------------------------------
# DRY-RUN
# ----------------------------------------------------------------------


def test_dry_run_one_candidate_no_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """DRY-RUN with one candidate: no tempfile, no enqueue, no audit."""
    doc_id = uuid4()
    candidate = _make_candidate(doc_id=doc_id, filename="dry-run.pdf")
    monkeypatch.setattr(
        recover,
        "_select_sealed_manual_review_candidates",
        lambda *, merchant_id: [candidate],
    )

    pdf_store = InMemoryPdfStoreRepository()
    pdf_store.store(document_id=doc_id, plaintext=_SAMPLE_PDF)
    audit = InMemoryAuditLog()

    # Track _enqueue_parse_jobs invocation — should NOT be called in dry-run.
    enqueue_calls: list[list[tuple[UUID, str]]] = []

    async def fake_enqueue(payloads: list[tuple[UUID, str]]) -> None:
        enqueue_calls.append(payloads)

    monkeypatch.setattr(recover, "_enqueue_parse_jobs", fake_enqueue)

    candidates, enqueued, issues = recover._reparse_sealed_manual_review(
        merchant_filter=None,
        pdf_store=pdf_store,  # type: ignore[arg-type]
        audit=audit,
        upload_dir=tmp_path,
        apply_writes=False,
    )

    assert candidates == 1
    assert enqueued == 0
    assert issues == 0
    assert enqueue_calls == []  # no enqueue
    assert audit.entries == []  # no audit row
    # No tempfile written in upload_dir.
    assert list(tmp_path.glob("*.pdf")) == []


# ----------------------------------------------------------------------
# --apply
# ----------------------------------------------------------------------


def test_apply_one_candidate_writes_tempfile_with_mode_0o644(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """--apply: tempfile lands in upload_dir at mode 0o644, enqueue
    fires once with the candidate's (UUID, path) pair, one audit row."""
    doc_id = uuid4()
    candidate = _make_candidate(doc_id=doc_id, filename="apply.pdf")
    monkeypatch.setattr(
        recover,
        "_select_sealed_manual_review_candidates",
        lambda *, merchant_id: [candidate],
    )

    pdf_store = InMemoryPdfStoreRepository()
    pdf_store.store(document_id=doc_id, plaintext=_SAMPLE_PDF)
    audit = InMemoryAuditLog()

    captured_payloads: list[tuple[UUID, str]] = []

    async def fake_enqueue(payloads: list[tuple[UUID, str]]) -> None:
        captured_payloads.extend(payloads)

    monkeypatch.setattr(recover, "_enqueue_parse_jobs", fake_enqueue)

    candidates, enqueued, issues = recover._reparse_sealed_manual_review(
        merchant_filter=None,
        pdf_store=pdf_store,  # type: ignore[arg-type]
        audit=audit,
        upload_dir=tmp_path,
        apply_writes=True,
    )

    assert candidates == 1
    assert enqueued == 1
    assert issues == 0

    # The tempfile landed under upload_dir and contains the plaintext.
    pdfs = list(tmp_path.glob("*.pdf"))
    assert len(pdfs) == 1
    assert pdfs[0].read_bytes() == _SAMPLE_PDF

    # Mode 0o644 on POSIX — skip on Windows where chmod semantics differ.
    if os.name == "posix":
        mode = stat.S_IMODE(pdfs[0].stat().st_mode)
        assert mode == 0o644

    # _enqueue_parse_jobs got the right (UUID, path) pair.
    assert len(captured_payloads) == 1
    enq_doc_id, enq_path = captured_payloads[0]
    assert enq_doc_id == doc_id
    assert Path(enq_path) == pdfs[0]

    # One audit row, correct shape.
    assert len(audit.entries) == 1
    a = audit.entries[0]
    assert a["actor"] == "recover_legacy_docs_script"
    assert a["action"] == "document.reparse_enqueued"
    assert a["subject_type"] == "document"
    assert UUID(a["subject_id"]) == doc_id
    assert a["details"]["reason"] == "sealed_manual_review_recovery"


def test_apply_decrypt_failure_increments_issues_and_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """One candidate's decrypt raises → issues=1, other candidate still
    enqueues. Verifies the per-doc fault-tolerance contract."""
    good_doc = uuid4()
    bad_doc = uuid4()
    good = _make_candidate(doc_id=good_doc, filename="good.pdf")
    bad = _make_candidate(doc_id=bad_doc, filename="bad.pdf")
    monkeypatch.setattr(
        recover,
        "_select_sealed_manual_review_candidates",
        lambda *, merchant_id: [good, bad],
    )

    pdf_store = InMemoryPdfStoreRepository()
    pdf_store.store(document_id=good_doc, plaintext=_SAMPLE_PDF)
    # bad_doc is NOT in the store → fetch_plaintext raises
    # PdfStoreNotFoundError, which the function catches into issues.

    audit = InMemoryAuditLog()
    captured_payloads: list[tuple[UUID, str]] = []

    async def fake_enqueue(payloads: list[tuple[UUID, str]]) -> None:
        captured_payloads.extend(payloads)

    monkeypatch.setattr(recover, "_enqueue_parse_jobs", fake_enqueue)

    candidates, enqueued, issues = recover._reparse_sealed_manual_review(
        merchant_filter=None,
        pdf_store=pdf_store,  # type: ignore[arg-type]
        audit=audit,
        upload_dir=tmp_path,
        apply_writes=True,
    )

    assert candidates == 2
    assert enqueued == 1
    assert issues == 1

    # Only the good doc was enqueued.
    assert [d for d, _ in captured_payloads] == [good_doc]
    # Only the good doc got an audit row.
    assert len(audit.entries) == 1
    assert UUID(audit.entries[0]["subject_id"]) == good_doc


# ----------------------------------------------------------------------
# Pacing inside _enqueue_parse_jobs
# ----------------------------------------------------------------------


def test_enqueue_parse_jobs_sleeps_100ms_per_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_enqueue_parse_jobs`` calls ``asyncio.sleep(0.1)`` once per
    enqueue so a 26-job burst doesn't swamp Supabase Storage. We don't
    measure real time — instead monkey-patch ``asyncio.sleep`` itself to
    a no-op recorder and count the calls."""

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    # Replace the arq pool with a stub that mimics the enqueue + close
    # interface. ``create_pool`` is imported inside the function, so
    # we need to monkey-patch the import target at the module level.
    enqueue_call_count = 0

    class _FakePool:
        async def enqueue_job(self, *_args: Any, **_kwargs: Any) -> None:
            nonlocal enqueue_call_count
            enqueue_call_count += 1

        async def close(self) -> None:
            return None

    async def fake_create_pool(_settings: Any) -> _FakePool:
        return _FakePool()

    # arq is imported at function-scope inside _enqueue_parse_jobs; we
    # have to patch the `arq` module attribute itself.
    import arq

    monkeypatch.setattr(arq, "create_pool", fake_create_pool)
    # Patch asyncio.sleep AS SEEN BY recover. recover binds asyncio at
    # import; replacing the module-level attr is enough. Use the dotted-
    # path setattr signature to keep mypy happy (the module's `asyncio`
    # attr isn't in its `__all__`).
    monkeypatch.setattr("scripts.recover_legacy_docs.asyncio.sleep", fake_sleep)

    # File paths are never opened by the fake pool — they're just opaque
    # strings forwarded as enqueue_job's second positional arg. Use a
    # non-/tmp prefix to avoid the S108 lint warning about insecure tmp.
    payloads: list[tuple[UUID, str]] = [
        (uuid4(), "fake-upload-dir/a.pdf"),
        (uuid4(), "fake-upload-dir/b.pdf"),
        (uuid4(), "fake-upload-dir/c.pdf"),
    ]
    asyncio.run(recover._enqueue_parse_jobs(payloads))

    # Three enqueues, three pacing sleeps at 100ms each.
    assert enqueue_call_count == 3
    assert sleep_calls == [0.1, 0.1, 0.1]
