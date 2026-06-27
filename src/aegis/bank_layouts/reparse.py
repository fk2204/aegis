"""Bank-layout-triggered reparse of stuck ``manual_review`` documents.

When the operator updates hints for a bank (or auto-hints arrive after
a successful parse for a never-parsed bank), every ``manual_review``
doc for that bank with a sealed ``pdf_store`` blob is a candidate for
re-enqueue — the new hints might let the Bedrock extraction prompt
succeed where it previously failed.

This module owns the candidate selection + decrypt + tempfile + enqueue
side of that workflow. The arq worker entrypoint
``aegis.workers.reparse_bank_manual_review`` is a thin wrapper that
calls ``enqueue_bank_reparse`` so the same code path runs whether the
trigger is the operator's HTMX POST or an auto-fired job.

Mirrors the per-doc reparse logic from
``scripts/recover_legacy_docs.py::_reparse_sealed_manual_review`` —
same chmod-0o644 pattern (root → aegis user readability), same
``keep_local_plaintext=False`` posture, same 100ms inter-enqueue pacing.
Differences from the script:

* No CLI / no dry-run — this is a worker-side helper, the operator's
  ``--dry-run`` parity lives in the script.
* Bank-filtered: only docs whose ``analyses.bank_name`` matches the
  bank are candidates. The case-insensitive match mirrors the bank
  layout repository's ``ilike`` lookup so a hint for ``"Chase"`` covers
  ``"CHASE"`` / ``"chase"`` rows.
* Best-effort audit: a single ``bank_layouts.reparse_enqueue_failed``
  row on whole-batch failure rather than the script's per-doc issue
  counter, because the worker has no operator on the other end to read
  a verbose summary.

Audit-row contract (CLAUDE.md auditability):

* ``bank_layouts.reparse_enqueued`` — one row per re-enqueue. Subject is
  the ``document``; details carry ``bank_name``, ``merchant_id``,
  ``document_id``, ``trigger``.
* ``bank_layouts.reparse_batch_complete`` — one row at end of batch.
  Subject is ``bank_layout`` (synthetic — bank_name carried in details).
* ``bank_layouts.reparse_enqueue_failed`` — only on whole-batch
  failure (Redis down, etc.). Best-effort.
* ``bank_layouts.reparse_operator_triggered`` — additional row written
  by the POST endpoint BEFORE calling ``enqueue_bank_reparse`` so the
  operator action is auditable even if the enqueue throws.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any, Final, cast
from uuid import UUID

from aegis.audit import AuditLog
from aegis.db import get_supabase
from aegis.logger import get_logger
from aegis.pdf_store import PdfStoreNotFoundError, PdfStoreRepository

_log = get_logger(__name__)

# Inter-enqueue pacing — mirrors
# ``scripts/recover_legacy_docs.py::_enqueue_parse_jobs`` (rationale
# documented there: the 2026-06-23 reparse run hit
# ``pdf_store.storage_upload_failed`` 16 times in a 90-second window on
# the un-paced version).
_PACE_SECONDS: Final[float] = 0.1

# Tempfile naming — distinct prefix so an ops grep of
# ``/var/lib/aegis/uploads`` can tell auto-reparse tempfiles from the
# script's ``recover_legacy_docs_script-reparse-`` prefix.
_TEMPFILE_PREFIX: Final[str] = "bank_layouts_reparse-"

# Default upload directory — same path the upload route + the
# recover-legacy script write to. The dir is owned by the ``aegis``
# user in production; the chmod 0o644 below makes the file readable by
# the worker even when this helper runs under root (e.g. when invoked
# by the operator from a CLI-shaped admin tool — the POST endpoint
# itself runs as the FastAPI process, which already runs as aegis).
_DEFAULT_UPLOAD_DIR: Final[Path] = Path("/var/lib/aegis/uploads")


def _select_sealed_manual_review_for_bank(
    *,
    bank_name: str,
) -> list[dict[str, Any]]:
    """Return the manual_review docs sealed in pdf_store for ``bank_name``.

    Two-step query (no SQL join surface from supabase-py's table builder):

      1. Pull every ``manual_review`` doc with ``storage_path IS NOT NULL``.
      2. Pull the matching ``analyses`` rows by ``document_id`` set and
         filter to those whose ``bank_name`` matches case-insensitively.

    The two-step approach is bounded by the manual_review universe
    (currently <200 rows on prod) so the wasted analyses-fetch on docs
    that don't match is cheap and the implementation stays portable
    across the supabase-py + in-memory test backends.

    Empty bank_name returns ``[]`` — defensive against accidental
    enqueue of a bank-wide reparse triggered by an empty-string hint
    write.
    """
    normalized = bank_name.strip()
    if not normalized:
        return []
    sb = get_supabase()
    docs_result = (
        sb.table("documents")
        .select("id, original_filename, storage_path, merchant_id, uploaded_at")
        .eq("parse_status", "manual_review")
        .not_.is_("storage_path", "null")
        .execute()
    )
    docs = cast(list[dict[str, Any]], docs_result.data or [])
    if not docs:
        return []
    doc_ids = [d["id"] for d in docs if d.get("id")]
    if not doc_ids:
        return []
    analyses_result = (
        sb.table("analyses").select("document_id, bank_name").in_("document_id", doc_ids).execute()
    )
    analyses = cast(list[dict[str, Any]], analyses_result.data or [])
    lowered = normalized.lower()
    matching_doc_ids = {
        row["document_id"]
        for row in analyses
        if (row.get("bank_name") or "").strip().lower() == lowered
    }
    return [d for d in docs if d.get("id") in matching_doc_ids]


async def enqueue_bank_reparse(
    *,
    bank_name: str,
    pool: Any,  # noqa: ANN401 — arq.ArqRedis duck-typed for test injection
    audit: AuditLog,
    pdf_store: PdfStoreRepository,
    trigger: str,
    upload_dir: Path | None = None,
    sleep_fn: Any = asyncio.sleep,  # noqa: ANN401 — duck-typed sleep for test pacing
) -> int:
    """Re-enqueue every sealed ``manual_review`` doc for ``bank_name``.

    Per-doc steps:

      1. Decrypt the sealed blob via ``pdf_store.fetch_plaintext``.
      2. Write the plaintext to a UUID-named tempfile under ``upload_dir``.
      3. ``chmod 0o644`` so the ``aegis`` user worker can read a file
         written by root (mirrors ``recover_legacy_docs.py`` regression
         fix at line 2058-2064).
      4. ``pool.enqueue_job("parse_document", doc_id, tmp_path,
         keep_local_plaintext=False)`` — the worker cleans up the
         tempfile (the sealed copy in pdf_store is the source of truth).
      5. Audit row ``bank_layouts.reparse_enqueued`` per success.
      6. ``await sleep_fn(0.1)`` between enqueues to pace Bedrock /
         Supabase Storage. Last enqueue does NOT sleep (no point
         pacing into nothing).

    Returns the count of successfully enqueued docs. Returns 0 with no
    side-effects when there are no candidates or ``pool`` is ``None``
    (test contexts without arq).

    Per-doc decrypt / write / enqueue failures log a
    ``bank_layouts.reparse_enqueue_failed`` audit row carrying the
    document_id + error string and continue to the next doc — a single
    bad blob doesn't cancel the rest of the batch. A whole-batch
    failure (pool unreachable etc.) writes ONE
    ``bank_layouts.reparse_enqueue_failed`` row with no document_id
    and re-raises so the caller knows the operation aborted.

    ``trigger`` is the audit details key documenting why the reparse
    fired — typical values: ``"hints_updated"`` (auto from set_hints
    hook) or ``"operator"`` (POST endpoint).
    """
    if pool is None:
        # Test / no-pool context — best-effort skip. Don't write an
        # audit row because there's no action to audit.
        return 0
    candidates = _select_sealed_manual_review_for_bank(bank_name=bank_name)
    if not candidates:
        audit.record(
            action="bank_layouts.reparse_batch_complete",
            actor="system",
            subject_type="bank_layout",
            details={
                "bank_layout_name": bank_name,
                "trigger": trigger,
                "candidates": 0,
                "enqueued": 0,
            },
        )
        return 0
    target_dir = upload_dir or _DEFAULT_UPLOAD_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    enqueued = 0
    last_index = len(candidates) - 1
    for i, c in enumerate(candidates):
        doc_id_raw = c.get("id")
        try:
            doc_id = UUID(str(doc_id_raw))
        except (ValueError, TypeError) as exc:
            audit.record(
                action="bank_layouts.reparse_enqueue_failed",
                actor="system",
                subject_type="bank_layout",
                details={
                    "bank_layout_name": bank_name,
                    "trigger": trigger,
                    "reason": "malformed_document_id",
                    "raw_id": str(doc_id_raw)[:64],
                    "error": str(exc)[:200],
                },
            )
            continue
        merchant_id_raw = c.get("merchant_id")
        try:
            plaintext = pdf_store.fetch_plaintext(doc_id)
        except PdfStoreNotFoundError as exc:
            audit.record(
                action="bank_layouts.reparse_enqueue_failed",
                actor="system",
                subject_type="document",
                subject_id=doc_id,
                details={
                    "bank_layout_name": bank_name,
                    "trigger": trigger,
                    "reason": "pdf_store_blob_missing",
                    "error": str(exc)[:200],
                },
            )
            continue
        except Exception as exc:
            audit.record(
                action="bank_layouts.reparse_enqueue_failed",
                actor="system",
                subject_type="document",
                subject_id=doc_id,
                details={
                    "bank_layout_name": bank_name,
                    "trigger": trigger,
                    "reason": "pdf_store_fetch_failed",
                    "error": str(exc)[:200],
                },
            )
            continue
        tmp = tempfile.NamedTemporaryFile(
            suffix=".pdf",
            delete=False,
            prefix=_TEMPFILE_PREFIX,
            dir=str(target_dir),
        )
        try:
            tmp.write(plaintext)
        finally:
            tmp.close()
        os.chmod(tmp.name, 0o644)
        try:
            await pool.enqueue_job(
                "parse_document",
                str(doc_id),
                tmp.name,
                keep_local_plaintext=False,
            )
        except Exception as exc:
            # Whole-batch failure (Redis dropped, etc.). Clean up the
            # tempfile we just wrote so we don't leak plaintext, write
            # ONE summary audit row covering the batch state, re-raise.
            Path(tmp.name).unlink(missing_ok=True)
            audit.record(
                action="bank_layouts.reparse_enqueue_failed",
                actor="system",
                subject_type="bank_layout",
                details={
                    "bank_layout_name": bank_name,
                    "trigger": trigger,
                    "reason": "pool_enqueue_failed",
                    "enqueued_before_failure": enqueued,
                    "remaining": len(candidates) - i,
                    "error": str(exc)[:200],
                },
            )
            raise
        audit.record(
            action="bank_layouts.reparse_enqueued",
            actor="system",
            subject_type="document",
            subject_id=doc_id,
            details={
                "bank_layout_name": bank_name,
                "trigger": trigger,
                "merchant_id": str(merchant_id_raw) if merchant_id_raw else None,
                "tmp_path": tmp.name,
            },
        )
        enqueued += 1
        if i < last_index:
            await sleep_fn(_PACE_SECONDS)
    audit.record(
        action="bank_layouts.reparse_batch_complete",
        actor="system",
        subject_type="bank_layout",
        details={
            "bank_layout_name": bank_name,
            "trigger": trigger,
            "candidates": len(candidates),
            "enqueued": enqueued,
        },
    )
    return enqueued


__all__ = [
    "_select_sealed_manual_review_for_bank",
    "enqueue_bank_reparse",
]
