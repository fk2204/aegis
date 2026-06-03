"""arq worker — runs the parser pipeline off the API process.

One job: ``parse_document(document_id, pdf_path)``. The job:

  1. Loads the document row (fail fast if missing).
  2. Runs the full pipeline (metadata → extract → validate → classify →
     patterns → aggregate).
  3. Persists transactions + analyses + status via the repository.
  4. Audits ``document.parse.complete`` (or ``document.parse.error``).
  5. Deletes the on-disk PDF in a finally block — PDFs are NEVER stored
     long-term per CLAUDE.md.

Boot: ``make worker`` runs ``arq aegis.workers.WorkerSettings``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from arq import cron
from redis.exceptions import RedisError

if TYPE_CHECKING:
    from arq.connections import RedisSettings

from aegis import storage_objects
from aegis.api.deps import (
    get_audit,
    get_close_client,
    get_funder_reply_repository,
    get_llm,
    get_merchant_repository,
    get_repository,
)
from aegis.api.routes.upload import EnqueueParse, persist_pdf_upload
from aegis.audit import AuditLog
from aegis.audit_archiver import run_archive_cron
from aegis.close.client import (
    CloseAttachment,
    CloseAuthError,
    CloseClient,
    CloseError,
)
from aegis.close.field_map import filename_matches_statement_filter
from aegis.config import get_settings
from aegis.crypto import CryptoConfigError, current_key_version, encrypt_pdf
from aegis.funders.replies import (
    FunderReplyError,
    FunderReplyPayload,
    FunderReplyRepository,
    IngestSource,
    ReplyStatus,
    ReplyTerms,
    ingest_reply,
)
from aegis.funders.reply_extract import (
    FunderReplyExtractionError,
    extract_funder_reply,
)
from aegis.llm import BedrockClient, LLMClient
from aegis.logger import configure_logging, get_logger
from aegis.merchants.repository import MerchantRepository
from aegis.ops.cost_tracking import CostTrackingBedrockClient
from aegis.parser.pipeline import PipelineResult, run_pipeline
from aegis.parser.processor import (
    ProcessorPipelineResult,
    detect_processor,
    run_processor_pipeline,
)
from aegis.storage import DocumentNotFoundError, DocumentRepository

_log = get_logger(__name__)


async def parse_document(
    ctx: dict[str, Any],
    document_id_str: str,
    pdf_path: str,
) -> dict[str, Any]:
    """arq job entrypoint. ``ctx`` is provided by arq, unused for now.

    Returns a small dict so the operator can spot-check job logs.
    PDF is deleted in the finally block — present even on error paths.
    """
    document_id = UUID(document_id_str)
    repository: DocumentRepository = ctx.get("repository") or get_repository()
    audit: AuditLog = ctx.get("audit") or get_audit()
    llm: LLMClient = ctx.get("llm") or get_llm()

    # Wrap the production BedrockClient with cost tracking so every
    # Bedrock call writes one bedrock.usage audit row tagged with this
    # document_id. Wrapping is gated on isinstance because the wrapper
    # re-issues calls through inner._client.messages — fake LLMClients
    # used in tests don't expose that surface. (mp Phase 11 #2.)
    if isinstance(llm, BedrockClient):
        llm = CostTrackingBedrockClient(
            inner=llm, audit=audit, document_id=document_id,
        )

    try:
        # Verify the row exists before doing expensive parser work.
        repository.get_document(document_id)
    except DocumentNotFoundError:
        _log.error("worker.parse.unknown_document document_id=%s", document_id)
        _safe_unlink(pdf_path)
        raise

    audit.record(
        actor="worker",
        action="document.parse.start",
        subject_type="document",
        subject_id=document_id,
        details={"pdf_path": pdf_path},
    )

    # Brand detection — Stripe / Square statements take a different
    # pipeline than bank statements. ``ambiguous`` fails closed; the
    # operator handles documents that look like both processors at
    # once rather than the parser guessing. (mp Phase 6.6 / Stage 2C.)
    detection = await asyncio.to_thread(detect_processor, pdf_path)
    if detection.brand == "ambiguous":
        _log.error("worker.parse.ambiguous_processor document_id=%s", document_id)
        audit.record(
            actor="worker",
            action="document.parse.error",
            subject_type="document",
            subject_id=document_id,
            details={
                "error": "AmbiguousProcessor",
                "message": (
                    f"detector saw both stripe ({detection.stripe_hits}) and "
                    f"square ({detection.square_hits}) signatures; "
                    "operator must classify manually"
                ),
            },
        )
        if hasattr(repository, "mark_error"):
            repository.mark_error(
                document_id,
                "AmbiguousProcessor: stripe + square signatures present",
            )
        _safe_unlink(pdf_path)
        return {
            "document_id": str(document_id),
            "parse_status": "manual_review",
            "fraud_score": 0,
        }
    if detection.brand in ("stripe", "square"):
        return await _run_processor_branch(
            document_id=document_id,
            pdf_path=pdf_path,
            brand=detection.brand,
            llm=llm,
            audit=audit,
            repository=repository,
        )

    try:
        result: PipelineResult = await asyncio.to_thread(run_pipeline, pdf_path, llm)
    except asyncio.CancelledError:
        # arq's job-timeout path: ``arq.worker.run_job`` wraps the
        # task in ``asyncio.wait_for(task, AEGIS_WORKER_JOB_TIMEOUT)``
        # and raises ``CancelledError`` into us when the budget expires
        # (verified 2026-06-03 against the 3df15a58 zombie).
        # ``CancelledError`` inherits from ``BaseException`` (not
        # ``Exception``) on Python 3.12+, so without this handler the
        # ``except Exception`` below silently misses it — leaving
        # ``parse_status`` stuck at ``"pending"`` AND the plaintext PDF
        # on disk forever. That breaks the day-one plaintext-at-rest
        # invariant the entire retention design exists to enforce.
        #
        # Cleanup mirrors the ``except Exception`` handler exactly so
        # operators see the same row/audit shape regardless of which
        # failure mode hit. Then re-raise so arq still counts the
        # cancellation and emits its own TimeoutError on the queue.
        _log.warning(
            "worker.parse.cancelled document_id=%s — likely arq job timeout",
            document_id,
        )
        audit.record(
            actor="worker",
            action="document.parse.error",
            subject_type="document",
            subject_id=document_id,
            details={
                "error": "CancelledError",
                "reason": "timeout",
                "message": "arq job timeout — parse cancelled before completion",
            },
        )
        if hasattr(repository, "mark_error"):
            repository.mark_error(
                document_id,
                "CancelledError: parse cancelled (likely arq job timeout)",
            )
        _safe_unlink(pdf_path)
        raise
    except Exception as exc:
        _log.exception("worker.parse.failed document_id=%s", document_id)
        audit.record(
            actor="worker",
            action="document.parse.error",
            subject_type="document",
            subject_id=document_id,
            details={"error": type(exc).__name__, "message": str(exc)[:500]},
        )
        if hasattr(repository, "mark_error"):
            repository.mark_error(document_id, f"{type(exc).__name__}: {exc}")
        # Parse failed: delete local plaintext (no storage step ran;
        # nothing to encrypt or audit beyond the parse error). Day-one
        # disk-hygiene rule preserved on the failure path.
        _safe_unlink(pdf_path)
        raise

    # Parse SUCCEEDED. Persist + audit the parse outcome BEFORE the
    # storage step runs. The storage step is best-effort: a failure
    # there must not erase the fact that we have a valid parse on
    # record.
    repository.persist_parse_result(document_id, result=result)

    audit.record(
        actor="worker",
        action="document.parse.complete",
        subject_type="document",
        subject_id=document_id,
        details={
            "parse_status": result.parse_status,
            "fraud_score": result.fraud_score,
            "transaction_count": len(result.classified),
            "flag_count": len(result.all_flags),
        },
    )

    # PDF retention chunk B — encrypted-storage step.
    # Reads the plaintext one more time, computes sha256_original,
    # encrypts under the current key version, uploads to Supabase
    # Storage, and persists the four storage columns atomically. Manages
    # local-file cleanup in EVERY outcome (success / transient
    # quarantine / terminal dead-letter) so the day-one "no plaintext
    # at rest past parse" rule is preserved across the failure paths.
    # Re-fetches the doc row so the storage_path picks up any
    # merchant_id that persist_parse_result associated.
    doc_after_persist = repository.get_document(document_id)
    await _try_encrypted_storage_step(
        pdf_path=pdf_path,
        document_id=document_id,
        merchant_id=doc_after_persist.merchant_id,
        file_hash=doc_after_persist.file_hash,
        repository=repository,
        audit=audit,
    )

    return {
        "document_id": str(document_id),
        "parse_status": result.parse_status,
        "fraud_score": result.fraud_score,
    }


async def _run_processor_branch(
    *,
    document_id: UUID,
    pdf_path: str,
    brand: str,
    llm: LLMClient,
    audit: AuditLog,
    repository: DocumentRepository,
) -> dict[str, Any]:
    """Run the processor pipeline + audit the result (mp Phase 6.6).

    Persistence to the ``processor_statements`` table needs a
    repository method that doesn't exist yet — punted to a follow-up
    commit on this branch. For now we audit the aggregates onto
    ``audit_log`` so the operator can see the parsed result in the
    dashboard's recent-activity feed, and mark the document's
    parse_status so downstream queries can filter on it.
    """
    try:
        pdf_bytes = await asyncio.to_thread(Path(pdf_path).read_bytes)
        result: ProcessorPipelineResult = await asyncio.to_thread(
            run_processor_pipeline, pdf_path, pdf_bytes, llm, brand=brand,  # type: ignore[arg-type]
        )
    except asyncio.CancelledError:
        # Same Python-3.12 CancelledError-is-BaseException case as the
        # bank-statement path above. arq's job timeout cancels the
        # processor pipeline mid-flight too (Stripe/Square statements
        # are LLM-bound for the same reasons bank statements are);
        # without this handler the row stayed at "pending" and the
        # plaintext lingered on disk. Cleanup mirrors the
        # ``except Exception`` handler; re-raise so arq counts the
        # cancellation.
        _log.warning(
            "worker.processor.cancelled document_id=%s brand=%s — likely arq job timeout",
            document_id,
            brand,
        )
        audit.record(
            actor="worker",
            action="document.parse.error",
            subject_type="document",
            subject_id=document_id,
            details={
                "error": "CancelledError",
                "reason": "timeout",
                "message": "arq job timeout — processor parse cancelled",
                "brand": brand,
            },
        )
        if hasattr(repository, "mark_error"):
            repository.mark_error(
                document_id,
                "CancelledError: processor parse cancelled (likely arq job timeout)",
            )
        _safe_unlink(pdf_path)
        raise
    except Exception as exc:
        _log.exception(
            "worker.processor.failed document_id=%s brand=%s", document_id, brand
        )
        audit.record(
            actor="worker",
            action="document.parse.error",
            subject_type="document",
            subject_id=document_id,
            details={
                "error": type(exc).__name__,
                "message": str(exc)[:500],
                "brand": brand,
            },
        )
        if hasattr(repository, "mark_error"):
            repository.mark_error(document_id, f"{type(exc).__name__}: {exc}")
        _safe_unlink(pdf_path)
        raise

    details: dict[str, Any] = {
        "brand": brand,
        "parse_status": result.parse_status,
        "validation_passed": result.validation.passed,
        "failure_count": len(result.validation.failures),
    }
    if result.aggregates is not None:
        details.update(
            {
                "gross_volume": str(result.aggregates.gross_volume.value),
                "refunds_total": str(result.aggregates.refunds_total.value),
                "chargebacks_total": str(result.aggregates.chargebacks_total.value),
                "fees_total": str(result.aggregates.fees_total.value),
                "payouts_total": str(result.aggregates.payouts_total.value),
                "net_revenue": str(result.aggregates.net_revenue.value),
                "chargeback_ratio": str(result.aggregates.chargeback_ratio),
                "transaction_count": result.aggregates.transaction_count.value,
            }
        )

    audit.record(
        actor="worker",
        action="document.parse.processor_complete",
        subject_type="document",
        subject_id=document_id,
        details=details,
    )

    if hasattr(repository, "mark_processor_parsed"):
        # Future repository method — when it lands, we'll persist the
        # aggregates to the processor_statements table here.
        repository.mark_processor_parsed(document_id, parse_status=result.parse_status)

    # PDF retention chunk B — encrypted-storage step (processor branch).
    # Same outcome contract as parse_document's bank-statement path:
    # success → blob uploaded + metadata persisted + plaintext deleted;
    # transient failure → ciphertext quarantined + plaintext deleted;
    # terminal failure → dead-letter + plaintext deleted. Passes
    # ``plaintext_bytes`` since the processor pipeline already read the
    # PDF at the top of this function — avoids a second disk read.
    doc_after = repository.get_document(document_id)
    await _try_encrypted_storage_step(
        pdf_path=pdf_path,
        document_id=document_id,
        merchant_id=doc_after.merchant_id,
        file_hash=doc_after.file_hash,
        repository=repository,
        audit=audit,
        plaintext_bytes=pdf_bytes,
    )

    return {
        "document_id": str(document_id),
        "parse_status": result.parse_status,
        "fraud_score": 0,  # processor pipeline doesn't compute a fraud_score
    }


def _safe_unlink(pdf_path: str) -> None:
    """Delete the temp PDF; log (but don't raise) on filesystem failure.

    The PDF is the secret here — leaving it behind is the bigger risk
    than failing a single job because of a stale handle.

    Chunk B: this helper is now called explicitly at the end of each
    parse outcome path (success after storage step, parse failure,
    storage transient quarantine, storage terminal dead-letter). The
    pre-chunk-B unconditional ``finally: _safe_unlink`` block has been
    removed — having the unlink in finally would have deleted the
    plaintext BEFORE the storage step could read it.
    """
    p = Path(pdf_path)
    if not p.exists():
        return
    try:
        p.unlink()
    except OSError:
        _log.warning("worker.cleanup_failed path=%s", pdf_path)


# ---------------------------------------------------------------------------
# PDF retention chunk B — encrypted storage step
# ---------------------------------------------------------------------------
#
# Called once per successful parse (bank or processor pipeline) AFTER
# ``persist_parse_result`` and the ``document.parse.complete`` audit
# row. Manages plaintext cleanup in EVERY outcome path so the day-one
# "no plaintext at rest past parse" disk-hygiene rule is preserved.
#
# Three outcome classes:
#
#   SUCCESS:    encrypt → upload → persist_storage_metadata succeed;
#               audit ``document.original_stored``; plaintext unlinked.
#
#   TRANSIENT:  ``storage_objects.upload`` raises (network / 5xx /
#               timeout / auth) OR ``persist_storage_metadata`` raises
#               (DB blip). Ciphertext (already in memory) is written
#               to ``quarantine/{document_id}.pdf.enc`` + ``.meta``
#               sidecar; audit ``document.original_storage_failed``
#               (reason=upload_failed or persist_failed); plaintext
#               unlinked. The chunk-E reconcile cron retries from the
#               quarantined ciphertext — NEVER re-reads plaintext,
#               NEVER re-encrypts (it's already sealed).
#
#   TERMINAL:  sha256(plaintext) != documents.file_hash (the on-disk
#               file changed between upload and parse-complete) OR
#               ``encrypt_pdf`` raises ``CryptoConfigError`` (env-var
#               key missing/malformed). Artifacts written to
#               ``quarantine/dead-letter/`` so the reconcile cron
#               (which scans ``quarantine/`` NON-RECURSIVELY) NEVER
#               picks them up for retry. The audit row's
#               ``outcome: dead_letter`` field is the operator-facing
#               signal that manual review is required.
#
# Never raises — the parse is already persisted by the time this runs
# and the worker shouldn't fail the job over a storage problem. The
# return value is informational only (used by tests + future
# observability metrics).


def _build_storage_path(merchant_id: UUID | None, document_id: UUID) -> str:
    """Stable Supabase-Storage path. Orphan docs (merchant_id=None at
    parse-complete time) land under ``unassigned/``; merchant-linked
    docs under ``merchants/{merchant_id}/``. Path is recorded
    verbatim in ``documents.storage_path`` and is the lookup key for
    the chunk-C view route + chunk-E sweep cron."""
    if merchant_id is None:
        return f"unassigned/documents/{document_id}.pdf.enc"
    return f"merchants/{merchant_id}/documents/{document_id}.pdf.enc"


def _quarantine_dir() -> Path:
    """Quarantine directory — same filesystem as plaintext upload dir
    so a future quarantine-to-disk write doesn't pay for a cross-FS
    move. Reconcile cron scans this directory NON-RECURSIVELY."""
    return get_settings().aegis_upload_dir / "quarantine"


def _dead_letter_dir() -> Path:
    """Dead-letter directory — subdirectory of ``quarantine/`` so the
    reconcile cron's non-recursive scan naturally skips terminal
    failures. Operator inspects these manually."""
    return _quarantine_dir() / "dead-letter"


def _make_chunk_b_dir(path: Path) -> Path:
    """Create a chunk-B working directory with restrictive 0700 mode.

    The .meta sidecars in quarantine/ and quarantine/dead-letter/
    carry storage-layout metadata (storage_path, sha256_original,
    key_version) — must NOT be world-readable. We apply 0700 on
    BOTH create (covers new directories) AND post-create chmod
    (covers pre-existing directories created with default systemd
    umask — typically 0755 on Linux without
    ``UMask=0077`` in the unit). Both belt and suspenders.
    """
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        # Best-effort. Some FSes (e.g. a mounted Windows share in a
        # dev environment) don't fully honor chmod. The log surfaces
        # the failure but doesn't block the worker — without the
        # chmod the data still goes to the right path.
        _log.warning("worker.chunk_b.chmod_failed path=%s", path)
    return path


def _write_quarantine(
    document_id: UUID,
    *,
    ciphertext: bytes,
    meta: dict[str, Any],
) -> None:
    """Write ciphertext blob + ``.meta`` sidecar to ``quarantine/``.

    CIPHERTEXT ONLY — never plaintext. The whole project exists
    because plaintext-at-rest was the failure mode; quarantining the
    plaintext to retry the upload would silently reintroduce it.
    The sidecar carries everything reconcile needs to retry the
    storage step without re-reading plaintext: sha256_original,
    encryption_key_version, storage_path, retention_until, plus a
    forensic record of the failure reason.
    """
    qdir = _make_chunk_b_dir(_quarantine_dir())
    blob_path = qdir / f"{document_id}.pdf.enc"
    meta_path = qdir / f"{document_id}.meta.json"
    blob_path.write_bytes(ciphertext)
    meta_path.write_text(
        json.dumps({"document_id": str(document_id), **meta}, sort_keys=True)
    )


def _write_dead_letter(
    document_id: UUID,
    *,
    ciphertext: bytes | None,
    meta: dict[str, Any],
) -> None:
    """Write dead-letter artifacts to ``quarantine/dead-letter/``.

    ``ciphertext=None`` cases: sha256_divergence (bytes can't be
    trusted, never encrypted) and encryption_error (couldn't encrypt
    at all). Either way the ``.meta`` sidecar lands so the operator
    has the forensic record. Reconcile cron NEVER retries from this
    directory.
    """
    # Ensure the parent quarantine/ exists at 0700 too — dead-letter/
    # alone with 0700 still leaves the parent traversal-permissioned
    # at whatever umask default applied on first create.
    _make_chunk_b_dir(_quarantine_dir())
    ddir = _make_chunk_b_dir(_dead_letter_dir())
    if ciphertext is not None:
        (ddir / f"{document_id}.pdf.enc").write_bytes(ciphertext)
    meta_path = ddir / f"{document_id}.meta.json"
    meta_path.write_text(
        json.dumps({"document_id": str(document_id), **meta}, sort_keys=True)
    )


async def _try_encrypted_storage_step(
    *,
    pdf_path: str,
    document_id: UUID,
    merchant_id: UUID | None,
    file_hash: str,
    repository: DocumentRepository,
    audit: AuditLog,
    plaintext_bytes: bytes | None = None,
) -> bool:
    """Run encrypt + upload + persist + audit, manage plaintext cleanup.

    See the module-level chunk-B comment above for the outcome
    contract. Returns ``True`` on success, ``False`` on any failure
    path — the caller doesn't act on the return value (every outcome
    is already audited + the plaintext is already cleaned up by the
    time this returns).

    ``plaintext_bytes`` is an optimization for the processor pipeline
    which already read the file at the top of ``_run_processor_branch``
    — passing them avoids a redundant disk read. Bank pipeline passes
    None and we read here.
    """
    try:
        # Read plaintext + compute integrity hash
        if plaintext_bytes is None:
            plaintext_bytes = await asyncio.to_thread(
                Path(pdf_path).read_bytes
            )
        sha256_original = hashlib.sha256(plaintext_bytes).hexdigest()

        # TERMINAL CHECK — sha256 divergence. The bytes on disk
        # don't match what was recorded at upload (``documents.file_hash``).
        # Storing this blob would silently misrepresent the archived
        # original. Dead-letter; reconcile NEVER retries.
        if sha256_original != file_hash:
            _write_dead_letter(
                document_id,
                ciphertext=None,
                meta={
                    "reason": "sha256_divergence",
                    "sha256_original": sha256_original,
                    "file_hash": file_hash,
                    "byte_size": len(plaintext_bytes),
                },
            )
            audit.record(
                actor="worker",
                action="document.original_storage_failed",
                subject_type="document",
                subject_id=document_id,
                details={
                    "reason": "sha256_divergence",
                    "sha256_original": sha256_original,
                    "file_hash": file_hash,
                    "outcome": "dead_letter",
                },
            )
            _safe_unlink(pdf_path)
            return False

        # Encrypt
        try:
            key_version = current_key_version()
            ciphertext = encrypt_pdf(plaintext_bytes, key_version=key_version)
        except CryptoConfigError as exc:
            # TERMINAL — key missing/malformed, operator must fix
            # /etc/aegis/aegis.env; auto-retry can't help.
            _write_dead_letter(
                document_id,
                ciphertext=None,
                meta={
                    "reason": "encryption_error",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc)[:500],
                    "sha256_original": sha256_original,
                    "byte_size": len(plaintext_bytes),
                },
            )
            audit.record(
                actor="worker",
                action="document.original_storage_failed",
                subject_type="document",
                subject_id=document_id,
                details={
                    "reason": "encryption_error",
                    "error_type": type(exc).__name__,
                    "outcome": "dead_letter",
                },
            )
            _safe_unlink(pdf_path)
            return False

        storage_path = _build_storage_path(merchant_id, document_id)
        retention_until = datetime.now(UTC) + timedelta(days=365 * 7)

        # Upload — TRANSIENT-CAPABLE
        try:
            await asyncio.to_thread(
                storage_objects.upload, storage_path, ciphertext
            )
        except Exception as exc:
            _write_quarantine(
                document_id,
                ciphertext=ciphertext,
                meta={
                    "reason": "upload_failed",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc)[:500],
                    "sha256_original": sha256_original,
                    "encryption_key_version": key_version,
                    "storage_path": storage_path,
                    "retention_until": retention_until.isoformat(),
                    "merchant_id": (
                        str(merchant_id) if merchant_id else None
                    ),
                    "byte_size": len(plaintext_bytes),
                },
            )
            audit.record(
                actor="worker",
                action="document.original_storage_failed",
                subject_type="document",
                subject_id=document_id,
                details={
                    "reason": "upload_failed",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc)[:500],
                    "storage_path": storage_path,
                    "outcome": "quarantine",
                },
            )
            _safe_unlink(pdf_path)
            return False

        # Persist storage metadata — TRANSIENT-CAPABLE (DB blip).
        # If this fails after the blob is uploaded, the blob is on
        # Supabase but documents.storage_path is NULL — an orphan
        # that reconcile fixes by re-running the (idempotent) upload
        # + persist sequence.
        try:
            await asyncio.to_thread(
                repository.persist_storage_metadata,
                document_id,
                storage_path=storage_path,
                sha256_original=sha256_original,
                encryption_key_version=key_version,
                retention_until=retention_until,
            )
        except Exception as exc:
            _write_quarantine(
                document_id,
                ciphertext=ciphertext,
                meta={
                    "reason": "persist_failed",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc)[:500],
                    "sha256_original": sha256_original,
                    "encryption_key_version": key_version,
                    "storage_path": storage_path,
                    "retention_until": retention_until.isoformat(),
                    "merchant_id": (
                        str(merchant_id) if merchant_id else None
                    ),
                    "byte_size": len(plaintext_bytes),
                },
            )
            audit.record(
                actor="worker",
                action="document.original_storage_failed",
                subject_type="document",
                subject_id=document_id,
                details={
                    "reason": "persist_failed",
                    "error_type": type(exc).__name__,
                    "outcome": "quarantine",
                },
            )
            _safe_unlink(pdf_path)
            return False

        # SUCCESS — blob uploaded + metadata persisted. Plaintext
        # safe to delete; audit the success.
        audit.record(
            actor="worker",
            action="document.original_stored",
            subject_type="document",
            subject_id=document_id,
            details={
                "storage_path": storage_path,
                "encryption_key_version": key_version,
                "byte_size": len(plaintext_bytes),
                "retention_until": retention_until.isoformat(),
            },
        )
        _safe_unlink(pdf_path)
        return True

    except Exception as exc:
        # 5th-path catch-all — any exception not handled by the three
        # explicit terminal/transient branches above. Real reachable
        # causes given those explicit handlers (per chunk-B review):
        #
        #   * ``Path.read_bytes`` OSError — plaintext file disappeared
        #     between persist_parse_result and storage step (rare;
        #     would imply another process running on /var/lib/aegis/
        #     uploads/, which shouldn't happen)
        #   * ``_write_quarantine`` / ``_write_dead_letter`` OSError —
        #     disk full or directory permission failure (we couldn't
        #     persist the recovery artifact AT ALL)
        #   * ``audit.record`` raising AuditWriteError — Supabase
        #     Postgres unreachable AT THE MOMENT OF AUDIT (chunk A's
        #     bucket-private path proved we don't fail boot when
        #     Supabase is down; the parse already-completed audit row
        #     would have failed first, so we likely never reach here
        #     while audit is broken — but in principle possible)
        #   * MemoryError on encrypt_pdf for a 25MB PDF — extreme;
        #     catching wouldn't help the worker recover
        #   * Anything unmapped in cryptography / asyncio (defense-in-
        #     depth against future library version surprises)
        #
        # ALERT LOUD per operator chunk-B review: CRITICAL log so the
        # journal-priority fix surfaces this in ``journalctl -p err``
        # AND ``-p crit`` immediately.
        #
        # CLEANUP DECISION — discriminated by exception class:
        #
        #   * ``OSError`` (and subclasses: PermissionError, ENOSPC,
        #     FileNotFoundError, etc.) → write/IO failure path. The
        #     recovery-artifact write into quarantine/ or
        #     dead-letter/ failed, which means the plaintext on disk
        #     is THE LAST COPY of this document's bytes. Deleting it
        #     would be data loss — disk full + delete = the worst
        #     possible outcome. PRESERVE the plaintext; the operator
        #     frees space / fixes permissions, then re-runs
        #     ``scripts/_reparse_one.py`` to retry.
        #
        #   * anything else (RuntimeError, MemoryError, asyncio
        #     internals, unmapped library exceptions) → genuine-leak
        #     path. The file's status is unknown; best-effort unlink
        #     preserves the disk-hygiene rule.
        #
        # ``_safe_unlink`` itself catches OSError, so the unlink call
        # below can't raise even on the file-already-gone case.
        is_write_failure = isinstance(exc, OSError)
        _log.critical(
            "ops.worker.encrypted_storage.unknown document_id=%s "
            "is_write_failure=%s",
            document_id, is_write_failure,
            exc_info=True,
        )
        try:
            audit.record(
                actor="worker",
                action="document.original_storage_failed",
                subject_type="document",
                subject_id=document_id,
                details={
                    "reason": "unknown",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc)[:500],
                    "outcome": (
                        "write_failure_preserved"
                        if is_write_failure
                        else "best_effort_cleanup"
                    ),
                },
            )
        except Exception:
            _log.critical(
                "ops.worker.encrypted_storage.audit_also_failed "
                "document_id=%s",
                document_id,
                exc_info=True,
            )
        if is_write_failure:
            # PRESERVE plaintext — operator-recovery copy. Loud CRITICAL
            # log above is the tripwire so this state doesn't go
            # unnoticed.
            return False
        # Best-effort plaintext unlink for non-IO unmapped exceptions.
        # The file is presumed orphaned by definition (something
        # unexpected happened during the storage step we didn't model).
        _safe_unlink(pdf_path)
        return False


async def _on_startup(ctx: dict[str, Any]) -> None:
    configure_logging()
    _log.info("worker.startup max_jobs=%s", ctx.get("max_jobs"))


async def _on_shutdown(ctx: dict[str, Any]) -> None:
    _log.info("worker.shutdown")


# ---------------------------------------------------------------------------
# Phase 10 — funder-reply ingestion task (mp §20 / Stage 2D-main).
# ---------------------------------------------------------------------------
#
# Two-pass LLM extraction over the raw email body, then the existing
# ``ingest_reply`` path (deterministic math gate + funder_replies
# insert + override stamping, idempotent per refinement (5)).
#
# Payload contract: arq receives a JSON string with the inbound message
# metadata + raw email body. See ``_decode_reply_payload``.


_VALID_INGEST_SOURCES: frozenset[str] = frozenset({"webhook", "operator_paste"})


def _decode_reply_payload(raw_json: str) -> dict[str, Any]:
    """Parse + validate the worker's input payload.

    Shape:
        {
          "deal_id":      "<uuid>",
          "funder_id":    "<uuid>",
          "raw_text":     "<email body>",
          "ingested_via": "webhook" | "operator_paste"
        }

    All four fields are required. Missing/malformed input raises
    ``ValueError`` so the arq job fails loud rather than persisting
    an empty reply row.
    """
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"reply payload is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(
            f"reply payload top-level must be an object; got {type(payload).__name__}"
        )
    for required in ("deal_id", "funder_id", "raw_text", "ingested_via"):
        if required not in payload:
            raise ValueError(f"reply payload missing required field: {required}")
    if payload["ingested_via"] not in _VALID_INGEST_SOURCES:
        raise ValueError(
            f"reply payload ingested_via must be one of {sorted(_VALID_INGEST_SOURCES)};"
            f" got {payload['ingested_via']!r}"
        )
    if not isinstance(payload["raw_text"], str) or not payload["raw_text"].strip():
        raise ValueError("reply payload raw_text must be a non-empty string")
    return payload


async def process_funder_reply(
    ctx: dict[str, Any],
    reply_payload_json: str,
) -> dict[str, Any]:
    """arq task — ingest one funder-reply email via the two-pass LLM extractor.

    Pipeline:
      1. Decode the JSON payload (deal_id, funder_id, raw_text, ingested_via).
      2. Run the two-pass LLM extractor (``extract_funder_reply``).
         Pass-1 emits candidate fields; pass-2 re-prompts iff pass-1 fails
         Pydantic strict validation.
      3. Map the extractor's status to a ``ReplyStatus`` understood by
         ``ingest_reply``. An "unknown" extraction is audited and dropped
         (no persistence) — the operator handles it via the paste UI.
      4. Build a ``FunderReplyPayload`` and call ``ingest_reply``. That
         function runs the deterministic math gate (amount * factor ≈
         payback +/- $0.01), persists the reply, and stamps the matching
         open override iff the gate passed and an override exists.

    The override-stamping idempotency contract lives entirely in
    ``ingest_reply`` + the repository layer; this worker is a thin
    orchestrator. That keeps the operator-paste HTTP path and the
    worker path mathematically identical.
    """
    payload = _decode_reply_payload(reply_payload_json)
    document_id = UUID(payload["deal_id"])
    funder_id = UUID(payload["funder_id"])
    raw_text: str = payload["raw_text"]
    ingested_via_raw: str = payload["ingested_via"]
    # Narrow to the IngestSource literal — _decode_reply_payload guards
    # the value set above, but mypy can't see through that runtime check.
    ingested_via: IngestSource = (
        "webhook" if ingested_via_raw == "webhook" else "operator_paste"
    )

    audit: AuditLog = ctx.get("audit") or get_audit()
    llm: LLMClient = ctx.get("llm") or get_llm()
    reply_repo: FunderReplyRepository = (
        ctx.get("funder_reply_repository") or get_funder_reply_repository()
    )

    # Cost-tracking wrapper, mirroring parse_document. Only wraps the
    # real BedrockClient; test stubs keep their bare interface.
    if isinstance(llm, BedrockClient):
        llm = CostTrackingBedrockClient(
            inner=llm, audit=audit, document_id=document_id,
        )

    audit.record(
        actor="worker",
        action="funder_reply.process.start",
        subject_type="deal",
        subject_id=document_id,
        details={"funder_id": str(funder_id), "ingested_via": ingested_via},
    )

    try:
        extraction = await asyncio.to_thread(extract_funder_reply, raw_text, llm)
    except FunderReplyExtractionError as exc:
        # Both LLM passes failed. Audit + raise so the inbound surfaces
        # in the operator's error queue — the reply is NOT persisted to
        # funder_replies because we don't know what the status is, and
        # writing a row with status='unknown' would corrupt the
        # confusion-matrix axes.
        _log.error(
            "worker.funder_reply.extraction_failed deal_id=%s funder_id=%s",
            document_id,
            funder_id,
        )
        audit.record(
            actor="worker",
            action="funder_reply.process.error",
            subject_type="deal",
            subject_id=document_id,
            details={
                "funder_id": str(funder_id),
                "ingested_via": ingested_via,
                "error": "FunderReplyExtractionError",
                "message": str(exc)[:500],
            },
        )
        raise

    if extraction.status == "unknown":
        # Unknown status → don't persist. Audit so the operator sees
        # the inbound on the dashboard and can hand-classify via the
        # paste UI. Stamping requires a definite status.
        audit.record(
            actor="worker",
            action="funder_reply.process.unknown",
            subject_type="deal",
            subject_id=document_id,
            details={
                "funder_id": str(funder_id),
                "ingested_via": ingested_via,
                "parsed_confidence": extraction.parsed_confidence,
                "reprompted": extraction.reprompted,
            },
        )
        return {
            "deal_id": str(document_id),
            "funder_id": str(funder_id),
            "status": "unknown",
            "persisted": False,
            "stamped_override_id": None,
        }

    # Build the strict payload + persist via the shared ingest path.
    # ``extract_funder_reply`` already ran the LLM-side validation gate;
    # ``ingest_reply`` runs the deterministic math gate and lowers
    # parsed_confidence to 0 on mismatch (the row persists for operator
    # hand-correction; the override is NOT stamped on math failure).
    reply_status: ReplyStatus = extraction.status
    persistence_payload = FunderReplyPayload(
        deal_id=document_id,
        funder_id=funder_id,
        status=reply_status,
        raw_text=raw_text,
        ingested_via=ingested_via,
        terms=extraction.terms or ReplyTerms(),
        parsed_confidence=extraction.parsed_confidence,
    )

    try:
        result = ingest_reply(persistence_payload, repo=reply_repo, audit=audit)
    except FunderReplyError as exc:
        _log.error(
            "worker.funder_reply.persist_failed deal_id=%s funder_id=%s",
            document_id,
            funder_id,
        )
        audit.record(
            actor="worker",
            action="funder_reply.process.error",
            subject_type="deal",
            subject_id=document_id,
            details={
                "funder_id": str(funder_id),
                "ingested_via": ingested_via,
                "error": "FunderReplyError",
                "message": str(exc)[:500],
            },
        )
        raise

    audit.record(
        actor="worker",
        action="funder_reply.process.complete",
        subject_type="deal",
        subject_id=document_id,
        details={
            "funder_id": str(funder_id),
            "ingested_via": ingested_via,
            "reply_id": str(result.reply_id),
            "status": reply_status,
            "validation_passed": result.validation.passed,
            "parsed_confidence": extraction.parsed_confidence,
            "stamped_override_id": (
                str(result.stamped_override_id)
                if result.stamped_override_id is not None
                else None
            ),
            "reprompted": extraction.reprompted,
        },
    )

    return {
        "deal_id": str(document_id),
        "funder_id": str(funder_id),
        "status": reply_status,
        "persisted": True,
        "reply_id": str(result.reply_id),
        "validation_passed": result.validation.passed,
        "stamped_override_id": (
            str(result.stamped_override_id)
            if result.stamped_override_id is not None
            else None
        ),
    }


# ---------------------------------------------------------------------------
# Feature 2 — Close attachment auto-flow orchestrator (chunk 2/5).
# ---------------------------------------------------------------------------
#
# After the inbound /webhooks/close upserts a merchant, the webhook
# fire-and-forgets this job (chunk 3). The job enumerates every
# attachment Close has on the Lead, filename-filters out the obvious
# non-statements, and pushes each statement through the existing
# persist_pdf_upload path — same SHA256 dedup, same audit shape, same
# parse-enqueue. Per-attachment errors are isolated; one bad file does
# NOT kill the batch.
#
# Filename filter + cap defaults live in ``aegis.config.Settings`` and
# the filename helper lives in ``aegis.close.field_map`` (chunk 4 of
# the close-attachment auto-flow). The orchestrator reads both at call
# time so an env override is picked up after a restart without code
# changes.


def _orchestrator_enqueue(ctx: dict[str, Any]) -> EnqueueParse:
    """Build an :data:`EnqueueParse` callable from the arq worker ctx.

    arq exposes its Redis connection as ``ctx['redis']``; that ArqRedis
    instance has ``enqueue_job``. Falling back to a no-op when the key
    is absent keeps unit tests that inject a fake ctx straightforward —
    tests can also override by passing their own ``enqueue_parse`` via
    ``ctx['enqueue_parse']`` (preferred for assertions on enqueue
    behavior).
    """
    override = ctx.get("enqueue_parse")
    if callable(override):
        return override  # type: ignore[no-any-return]

    redis = ctx.get("redis")

    async def _enqueue(document_id: UUID, pdf_path: str) -> None:
        if redis is None:
            # In-process / test path. Mirror upload._enqueue_parse_job's
            # pending_jobs fallback so the test harness can drain.
            pending = ctx.setdefault("pending_jobs", [])
            pending.append({"document_id": str(document_id), "pdf_path": pdf_path})
            return
        await redis.enqueue_job("parse_document", str(document_id), pdf_path)

    return _enqueue


async def process_close_attachments(
    ctx: dict[str, Any],
    close_lead_id: str,
    trigger: str,
    *,
    actor_email: str | None = None,
    override_cap: bool = False,
) -> dict[str, Any]:
    """List attachments on a Close Lead, filename-filter, persist each
    statement through :func:`persist_pdf_upload`.

    ``trigger`` is "webhook" (auto from /webhooks/close, chunk 3) or
    "rescan" (operator-clicked, chunk 5). Audited so the operator can
    distinguish auto vs. manual runs.

    ``override_cap`` is set by the rescan-with-override UI button when
    a previous run hit ``_ATTACHMENTS_HARD_CAP``. Default False — the
    cap protects against the "wrong-folder, 47 PDFs" mistake burning a
    Bedrock call per file.

    Per-attachment errors (download_failed, pydantic validation, etc.)
    are isolated and logged via audit rows; the loop walks the full
    list before returning a summary dict. The summary lands in arq's
    job log and (for ``trigger='rescan'``) drives the cap-override UI
    in chunk 5.

    Idempotency: the SHA256 dedup inside ``persist_pdf_upload`` short-
    circuits before any parse enqueue or Bedrock cost. A redelivered
    webhook + a manual rescan + the original fire all converge on the
    same documents row per attachment. Confirmed at
    upload.persist_pdf_upload's ``find_by_hash`` gate.
    """
    merchants: MerchantRepository = (
        ctx.get("merchants") or get_merchant_repository()
    )
    repository: DocumentRepository = ctx.get("repository") or get_repository()
    audit: AuditLog = ctx.get("audit") or get_audit()
    close_client: CloseClient = ctx.get("close_client") or get_close_client()
    enqueue_parse = _orchestrator_enqueue(ctx)
    settings = get_settings()
    filename_filters = settings.close_attachment_filename_filters
    warn_threshold = settings.close_attachment_warn_threshold
    hard_cap = settings.close_attachment_hard_cap

    summary: dict[str, Any] = {
        "trigger": trigger,
        "close_lead_id": close_lead_id,
        "total": 0,
        "fetched": 0,
        "duplicates": 0,
        "skipped": 0,
        "failed": 0,
        "capped": False,
        "override_cap": override_cap,
    }

    merchant = merchants.find_by_close_lead_id(close_lead_id)
    if merchant is None:
        audit.record(
            actor="worker",
            actor_email=actor_email,
            action="close.orchestration.no_merchant",
            details={"close_lead_id": close_lead_id, "trigger": trigger},
        )
        return summary

    try:
        attachments = close_client.list_lead_attachments(close_lead_id)
    except (CloseAuthError, CloseError) as exc:
        audit.record(
            actor="worker",
            actor_email=actor_email,
            action="close.orchestration.list_failed",
            subject_type="merchant",
            subject_id=merchant.id,
            details={
                "close_lead_id": close_lead_id,
                "trigger": trigger,
                "error": type(exc).__name__,
                "message": str(exc)[:500],
            },
        )
        raise

    summary["total"] = len(attachments)

    # Soft cap accounting — record which attachments survive filename
    # filtering, slice at the cap unless override is set, audit the
    # over-cap names so the rescan-with-override UI can surface them.
    statement_candidates: list[CloseAttachment] = []
    for att in attachments:
        if filename_matches_statement_filter(att.name, filename_filters):
            statement_candidates.append(att)
            continue
        summary["skipped"] += 1
        audit.record(
            actor="worker",
            actor_email=actor_email,
            action="close.attachment.skipped",
            subject_type="merchant",
            subject_id=merchant.id,
            details={
                "attachment_id": att.id,
                "filename": att.name,
                "reason": "filename_prefix_mismatch",
                "close_lead_id": close_lead_id,
            },
        )

    if len(statement_candidates) >= warn_threshold:
        audit.record(
            actor="worker",
            actor_email=actor_email,
            action="close.orchestration.warn_high_attachment_count",
            subject_type="merchant",
            subject_id=merchant.id,
            details={
                "close_lead_id": close_lead_id,
                "candidate_count": len(statement_candidates),
                "warn_threshold": warn_threshold,
            },
        )

    skipped_by_cap: list[dict[str, str]] = []
    if not override_cap and len(statement_candidates) > hard_cap:
        skipped_by_cap = [
            {"attachment_id": att.id, "filename": att.name}
            for att in statement_candidates[hard_cap:]
        ]
        statement_candidates = statement_candidates[:hard_cap]
        summary["capped"] = True
        audit.record(
            actor="worker",
            actor_email=actor_email,
            action="close.orchestration.capped",
            subject_type="merchant",
            subject_id=merchant.id,
            details={
                "close_lead_id": close_lead_id,
                "hard_cap": hard_cap,
                "deferred": skipped_by_cap,
                "deferred_count": len(skipped_by_cap),
            },
        )
    elif override_cap and len(attachments) > hard_cap:
        audit.record(
            actor="worker",
            actor_email=actor_email,
            action="close.orchestration.cap_overridden",
            subject_type="merchant",
            subject_id=merchant.id,
            details={
                "close_lead_id": close_lead_id,
                "hard_cap": hard_cap,
                "candidate_count": len(statement_candidates),
            },
        )

    for att in statement_candidates:
        try:
            file_bytes, filename = close_client.download_attachment(att.id)
        except (CloseAuthError, CloseError) as exc:
            summary["failed"] += 1
            audit.record(
                actor="worker",
                actor_email=actor_email,
                action="close.attachment.fetch_failed",
                subject_type="merchant",
                subject_id=merchant.id,
                details={
                    "attachment_id": att.id,
                    "filename": att.name,
                    "close_lead_id": close_lead_id,
                    "error": type(exc).__name__,
                    "message": str(exc)[:500],
                },
            )
            continue

        try:
            upload_response = await persist_pdf_upload(
                enqueue_parse=enqueue_parse,
                body=file_bytes,
                original_filename=filename,
                repository=repository,
                audit=audit,
                actor="worker",
                actor_email=actor_email,
                merchant_id=merchant.id,
                close_lead_id=close_lead_id,
            )
        except Exception as exc:
            # persist_pdf_upload raises HTTPException for size / non-PDF.
            # Treat as a per-attachment fail; do not abort the batch.
            summary["failed"] += 1
            audit.record(
                actor="worker",
                actor_email=actor_email,
                action="close.attachment.fetch_failed",
                subject_type="merchant",
                subject_id=merchant.id,
                details={
                    "attachment_id": att.id,
                    "filename": filename,
                    "close_lead_id": close_lead_id,
                    "error": type(exc).__name__,
                    "message": str(exc)[:500],
                },
            )
            continue

        is_duplicate = upload_response.duplicate_of_existing
        summary["fetched"] += 1
        if is_duplicate:
            summary["duplicates"] += 1
        audit.record(
            actor="worker",
            actor_email=actor_email,
            action="close.attachment.fetched",
            subject_type="document",
            subject_id=upload_response.document_id,
            details={
                "attachment_id": att.id,
                "filename": filename,
                "close_lead_id": close_lead_id,
                "duplicate": is_duplicate,
                "trigger": trigger,
            },
        )

    audit.record(
        actor="worker",
        actor_email=actor_email,
        action="close.orchestration.complete",
        subject_type="merchant",
        subject_id=merchant.id,
        details=dict(summary),
    )
    return summary


class WorkerSettings:
    """arq config. Reads concurrency + timeout from env via Settings."""

    functions = (parse_document, process_funder_reply, process_close_attachments)
    # Nightly at 02:00 UTC — low-traffic window. Per master plan §17,
    # the archive cron runs daily and is the only cron registered here.
    cron_jobs = (
        cron(run_archive_cron, hour=2, minute=0, run_at_startup=False),
    )
    on_startup = _on_startup
    on_shutdown = _on_shutdown

    @staticmethod
    def _from_settings() -> dict[str, Any]:
        s = get_settings()
        return {
            "max_jobs": s.aegis_worker_max_concurrent,
            "job_timeout": s.aegis_worker_job_timeout,
            "redis_settings_url": s.redis_url,
        }


def build_redis_settings() -> RedisSettings:
    """Construct an ``arq.connections.RedisSettings`` from REDIS_URL."""
    from arq.connections import RedisSettings as _RedisSettings

    return _RedisSettings.from_dsn(get_settings().redis_url)


# Mirror the static class attributes arq actually inspects, populated from
# settings at import time. arq reads class attributes (max_jobs, job_timeout,
# redis_settings) — keeping them as class attributes means a misconfigured
# env still raises at import (fail-closed) rather than at the first job.
def _populate_worker_attributes() -> None:
    s = get_settings()
    WorkerSettings.max_jobs = s.aegis_worker_max_concurrent  # type: ignore[attr-defined]
    WorkerSettings.job_timeout = s.aegis_worker_job_timeout  # type: ignore[attr-defined]
    WorkerSettings.redis_settings = build_redis_settings()  # type: ignore[attr-defined]


# Allow import when Redis is unreachable (tests inject their own ctx, and
# CI machines without a local Redis still need to import the module). We
# narrowly catch only redis-connection problems so that genuine config
# validation errors (e.g. ``DataResidencyError`` from ``get_settings``)
# still propagate at import — that's the fail-closed contract from
# CLAUDE.md.
try:
    _populate_worker_attributes()
except (RedisError, ConnectionError):
    _log.debug("worker.attributes_deferred", exc_info=True)


__all__ = [
    "WorkerSettings",
    "parse_document",
    "process_close_attachments",
    "process_funder_reply",
]
