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
    get_merchant_shadow_signal_repository,
    get_pdf_store_repository,
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
from aegis.close.field_map import (
    filename_is_non_statement,
    filename_matches_statement_filter,
)
from aegis.config import get_settings
from aegis.crypto import CryptoConfigError, current_key_version, encrypt_pdf
from aegis.funder_note_submissions.repository import (
    FunderNoteSubmissionRepository,
)
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
from aegis.funders.repository import FunderNotFoundError, FunderRepository
from aegis.llm import BedrockClient, LLMClient
from aegis.logger import configure_logging, get_logger
from aegis.merchants.cross_statement_pipeline import (
    run_cross_statement_detection,
)
from aegis.merchants.renewal_reminder import run_renewal_reminder_cron
from aegis.merchants.repository import MerchantNotFoundError, MerchantRepository
from aegis.merchants.shadow_signals import (
    MerchantShadowSignalRepository,
    record_shadow_signal,
)
from aegis.ops.cost_tracking import CostTrackingBedrockClient
from aegis.parser.pipeline import MerchantContext, PipelineResult, run_pipeline
from aegis.parser.processor import (
    ProcessorPipelineResult,
    detect_processor,
    run_processor_pipeline,
)
from aegis.parser.shadow_audit import shadow_audit_payloads
from aegis.parser.tampering import TamperingEvaluation
from aegis.pdf_store import (
    PdfStoreRepository,
    PdfStoreWriteError,
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
    # Migration 034 — merchant-from-statement (chunk B). The worker
    # finalize step (after persist_parse_result) reads from this; the
    # failure-path flag helper writes to it. Pulled here so tests can
    # inject an in-memory repo via the ctx dict.
    merchants_repo: MerchantRepository = ctx.get("merchants") or get_merchant_repository()
    # U22 — merchant-scope shadow-signal persistence. Pulled here so
    # tests can inject an in-memory repo via the ctx dict, mirroring
    # the ``merchants`` slot above. The U15 worker hook
    # (``_run_cross_statement_detection``) reads this off the local and
    # writes one row per emitted Pattern.
    shadow_signals_repo: MerchantShadowSignalRepository = (
        ctx.get("shadow_signals") or get_merchant_shadow_signal_repository()
    )
    # Migration 060 — chunk-B-via-Postgres pdf_store. Pulled here so
    # tests can inject an in-memory repo via the ctx dict (mirrors the
    # ``merchants`` / ``shadow_signals`` slots above). The post-parse
    # storage step calls ``store`` before the local PDF is unlinked.
    pdf_store_repo: PdfStoreRepository = ctx.get("pdf_store") or get_pdf_store_repository()

    # Wrap the production BedrockClient with cost tracking so every
    # Bedrock call writes one bedrock.usage audit row tagged with this
    # document_id. Wrapping is gated on isinstance because the wrapper
    # re-issues calls through inner._client.messages — fake LLMClients
    # used in tests don't expose that surface. (mp Phase 11 #2.)
    if isinstance(llm, BedrockClient):
        llm = CostTrackingBedrockClient(
            inner=llm,
            audit=audit,
            document_id=document_id,
        )

    try:
        # Verify the row exists before doing expensive parser work.
        doc_pre_parse = repository.get_document(document_id)
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

    # Feature D — load the merchant's free-text context (operator notes
    # + Close lead description + recent notes + recent call transcripts)
    # so the Bedrock extraction prompt can disambiguate ambiguous
    # statement layouts / transactions using known deal context.
    # ``None`` when the doc has no merchant (bearer upload) OR the
    # merchant row has no context fields set. The prompt builder
    # treats both cases identically — no MERCHANT CONTEXT block in the
    # prompt suffix.
    merchant_context = _load_merchant_context(
        merchants_repo=merchants_repo,
        merchant_id=doc_pre_parse.merchant_id,
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
            merchants_repo=merchants_repo,
            pdf_store_repo=pdf_store_repo,
        )

    # ===================================================================
    # CONCERN 1 of 3 — run the parse pipeline, handle failure cleanup.
    # Three sub-concerns live inside the failure handlers below:
    #   (a) the existing doc-side cleanup (audit, mark_error, unlink),
    #   (b) chunk-B's plaintext-at-rest cancellation handling
    #       (commit 490c476 — CancelledError doesn't inherit Exception),
    #   (c) chunk-B(merchant)'s zombie-provisional prevention
    #       (migration 034 — flag the merchant for manual naming).
    # ===================================================================
    try:
        result: PipelineResult = await asyncio.to_thread(
            _run_pipeline_with_merchant_context,
            pdf_path,
            llm,
            merchant_context,
        )
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
        # Migration 034 zombie-prevention — surface the provisional
        # merchant out of "still parsing" into "needs your input".
        _flag_provisional_for_manual_naming(
            merchants_repo=merchants_repo,
            audit=audit,
            repository=repository,
            document_id=document_id,
            reason="parse_cancelled",
        )
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
        # Migration 034 zombie-prevention — same as the cancel path
        # above but a different reason for the audit row.
        _flag_provisional_for_manual_naming(
            merchants_repo=merchants_repo,
            audit=audit,
            repository=repository,
            document_id=document_id,
            reason="parse_exception",
        )
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

    # Shadow-flag audit emission. CLAUDE.md "Shadow-first for ALL new
    # scoring rules" requires audit-log telemetry for every shadow check
    # before the live-flip decision. The deterministic validate gate
    # emits shadow flags as strings on ValidationResult.warnings (no DB
    # in scope there by design); we translate + persist here, where
    # `audit` and `document_id` are. Audit-write failure propagates per
    # the existing pattern.
    for shadow_payload in shadow_audit_payloads(list(result.validation.warnings)):
        audit.record(
            actor="worker",
            action=shadow_payload.action,
            subject_type="document",
            subject_id=document_id,
            details=shadow_payload.details,
        )

    # U15 — cross-statement / related-account detection. Runs
    # immediately after persist_parse_result so the just-stored
    # analysis is visible to a downstream re-fetch (not used today,
    # but keeps the contract honest) and BEFORE the storage step
    # because the detector is in-memory only — a storage-step failure
    # must not erase the cross-statement Pattern list either. The
    # detector returns severity-0 shadow flags (U12 invariant); we
    # stash them on the in-memory PipelineResult and emit one INFO
    # log line with flag CODES only — no PII per CLAUDE.md.
    _run_cross_statement_detection(
        result=result,
        document_id=document_id,
        repository=repository,
        shadow_signals_repo=shadow_signals_repo,
        audit=audit,
    )

    # Tampering composition (operator policy 2026-06-04). The pipeline
    # has already evaluated the rule; the worker writes one audit row
    # when it fires. ``shadow`` (default) reports what WOULD decline so
    # the operator can review the matrix before any applicant gets
    # rejected; ``live`` reports what DID decline. The scoring layer
    # consumes the same evaluation via ``score_input_multi_month``.
    if result.tampering_evaluation is not None and result.tampering_evaluation.fires:
        _audit_tampering_evaluation(
            audit=audit,
            document_id=document_id,
            evaluation=result.tampering_evaluation,
        )

    # Re-fetch the doc row — persist_parse_result associates merchant_id
    # if it wasn't already set. Both downstream concerns read it.
    doc_after_persist = repository.get_document(document_id)

    # ===================================================================
    # CONCERN 2 of 3 — migration 034 merchant finalize / flag.
    # Independent of the storage step. Runs FIRST because it's a single
    # UPDATE (cheap); a finalize failure must not strand ciphertext in
    # the bucket. The helper handles the four outcomes:
    #   * doc.merchant_id is None (bearer / orphan upload) — skip.
    #   * clean account_holder — finalize_provisional + audit (rowcount
    #     gated; only on observed change).
    #   * blank account_holder OR no extraction — mark_needs_manual_naming
    #     + audit (rowcount gated; reason=blank_account_holder).
    #   * merchant already finalized (operator manual rename) — no-op.
    # ===================================================================
    _finalize_or_flag_merchant_from_statement(
        merchants_repo=merchants_repo,
        audit=audit,
        document_id=document_id,
        merchant_id=doc_after_persist.merchant_id,
        extraction=result.extraction,
    )

    # ===================================================================
    # CONCERN 3 of 3 — chunk-B encrypted PDF storage step.
    # Reads the plaintext one more time, computes sha256_original,
    # encrypts under the current key version, uploads to Supabase
    # Storage, and persists the four storage columns atomically. Manages
    # local-file cleanup in EVERY outcome (success / transient
    # quarantine / terminal dead-letter) so the day-one "no plaintext
    # at rest past parse" rule is preserved across the failure paths.
    #
    # 2026-06-15 operator directive: ALSO write the AES-GCM ciphertext
    # into the Postgres ``pdf_store`` table (migration 060). Runs FIRST
    # so a Postgres-side failure preserves the plaintext on disk for
    # retry — the legacy Supabase step is what unlinks on every outcome
    # path, so skipping it on pdf_store failure keeps the plaintext
    # available for the operator. The view route streams from Postgres.
    # ===================================================================
    pdf_store_ok = await _try_pdf_store_step(
        pdf_path=pdf_path,
        document_id=document_id,
        pdf_store_repo=pdf_store_repo,
        audit=audit,
    )
    if pdf_store_ok:
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
    merchants_repo: MerchantRepository,
    pdf_store_repo: PdfStoreRepository,
) -> dict[str, Any]:
    """Run the processor pipeline + audit the result (mp Phase 6.6).

    Persistence to the ``processor_statements`` table needs a
    repository method that doesn't exist yet — punted to a follow-up
    commit on this branch. For now we audit the aggregates onto
    ``audit_log`` so the operator can see the parsed result in the
    dashboard's recent-activity feed, and mark the document's
    parse_status so downstream queries can filter on it.

    Migration 034 — the processor pipeline doesn't expose an
    ``account_holder`` analogue, so a processor-branch SUCCESS (let
    alone a failure or cancellation) flags the provisional merchant
    for manual naming. The flag-helper call sites are spread across
    the four failure handlers AND the success tail; see the
    ``_flag_provisional_for_manual_naming`` docstring for the four
    ``reason`` codes the dashboard renders banners for.
    """
    try:
        pdf_bytes = await asyncio.to_thread(Path(pdf_path).read_bytes)
        result: ProcessorPipelineResult = await asyncio.to_thread(
            run_processor_pipeline,
            pdf_path,
            pdf_bytes,
            llm,
            brand=brand,  # type: ignore[arg-type]
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
        # Migration 034 zombie-prevention — flag provisional merchant.
        _flag_provisional_for_manual_naming(
            merchants_repo=merchants_repo,
            audit=audit,
            repository=repository,
            document_id=document_id,
            reason="parse_cancelled",
        )
        raise
    except Exception as exc:
        _log.exception("worker.processor.failed document_id=%s brand=%s", document_id, brand)
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
        # Migration 034 zombie-prevention — flag provisional merchant.
        _flag_provisional_for_manual_naming(
            merchants_repo=merchants_repo,
            audit=audit,
            repository=repository,
            document_id=document_id,
            reason="parse_exception",
        )
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

    doc_after = repository.get_document(document_id)

    # Migration 034 — processor pipeline doesn't expose an
    # ``account_holder`` analogue (the processor statement summary
    # carries the gross_volume / payouts shape but not a clean
    # business-name string). Surface the provisional merchant to
    # ``needs_manual_naming`` so the dashboard tells the truth: parse
    # is DONE, awaiting the OPERATOR (not awaiting parse). Reason
    # ``processor_branch`` distinguishes from the failure-path reasons
    # in the audit row's banner mapping.
    _flag_provisional_for_manual_naming(
        merchants_repo=merchants_repo,
        audit=audit,
        repository=repository,
        document_id=document_id,
        reason="processor_branch",
    )

    # PDF retention chunk B — encrypted-storage step (processor branch).
    # Same outcome contract as parse_document's bank-statement path:
    # success → blob uploaded + metadata persisted + plaintext deleted;
    # transient failure → ciphertext quarantined + plaintext deleted;
    # terminal failure → dead-letter + plaintext deleted. Passes
    # ``plaintext_bytes`` since the processor pipeline already read the
    # PDF at the top of this function — avoids a second disk read.
    #
    # 2026-06-15 operator directive: also write into ``pdf_store``
    # (migration 060). Runs first so a Postgres-side failure preserves
    # the plaintext on disk for retry.
    pdf_store_ok = await _try_pdf_store_step(
        pdf_path=pdf_path,
        document_id=document_id,
        pdf_store_repo=pdf_store_repo,
        audit=audit,
        plaintext_bytes=pdf_bytes,
    )
    if pdf_store_ok:
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


# ===========================================================================
# Migration 034 — merchant-from-statement helpers.
#
# Two pure functions: one for the success path (auto-name from the
# statement's ``account_holder``, fall back to ``needs_manual_naming``
# on blank), one for the can't-auto-name paths (failures + processor
# branch success). Both are rowcount-gated on the audit emission so a
# false "this changed" row never lands.
# ===========================================================================


def _run_cross_statement_detection(
    *,
    result: PipelineResult,
    document_id: UUID,
    repository: DocumentRepository,
    shadow_signals_repo: MerchantShadowSignalRepository,
    audit: AuditLog,
) -> None:
    """U15 — invoke the U12 detector + stash the result on PipelineResult.

    U22 extends the U15 hook to persist each emitted Pattern as a row
    in ``merchants_shadow_signals`` (migration 044). The in-memory
    ``PipelineResult.cross_statement_patterns`` field is preserved for
    tests + log emission; the durable channel is the new table read by
    the dossier render.

    Runs after ``persist_parse_result`` so the just-stored row IS the
    cross-statement detector's notion of "now" — the orchestrator
    filters out the current document in the priors list so a SHA hit
    against the row we just wrote can't false-positive.

    Four early-return paths produce an empty cross_statement_patterns
    list (and no log line / no persistence):

      * ``result.extraction`` is ``None`` — page-router low-confidence
        fail-closed, OCR-oversize bail, or a validation-gate failure.
        Nothing to compare against because we don't have a holder or
        last4 from the failed pass-1.
      * ``doc_after.merchant_id`` is ``None`` — bearer / orphan upload,
        no merchant scope to query priors under.
      * The merchant has no prior documents on file — first upload.
      * The detector returns an empty list (no SHA collision, no
        related-account drift).

    The successful-fire path emits one INFO log (code list only — NEVER
    holder strings or document ids per CLAUDE.md PII rules) and writes
    one ``merchants_shadow_signals`` row per Pattern via
    ``record_shadow_signal``. Each persistence attempt is wrapped in
    try/except so a Supabase blip on the shadow-signal write doesn't
    fail the upload — the parse + persist already succeeded, and the
    cross-statement Pattern list is informational shadow data per the
    U12 contract.
    """
    if result.extraction is None:
        return

    # Re-fetch the doc row so the merchant_id we use matches what
    # persist_parse_result wrote (the worker-merchant migration 034
    # flow associates merchant_id at persist time even if the
    # document was created without one).
    try:
        doc_after = repository.get_document(document_id)
    except Exception:
        # If we can't read our own just-written row, log and skip;
        # the parse outcome is already persisted, and the
        # cross-statement Pattern list is informational shadow data.
        _log.warning(
            "worker.cross_statement.doc_lookup_failed document_id=%s",
            document_id,
        )
        return

    if doc_after.merchant_id is None:
        return

    summary = result.extraction.statement.summary

    try:
        patterns = run_cross_statement_detection(
            merchant_id=doc_after.merchant_id,
            current_document_id=document_id,
            current_sha256=doc_after.sha256_original,
            current_uploaded_at=doc_after.uploaded_at,
            current_bank_name=summary.bank_name,
            current_account_holder=summary.account_holder,
            current_account_last4=summary.account_last4,
            repo=repository,
        )
    except Exception:
        # Detector orchestrator failure (DB blip on list_documents or
        # get_analyses_by_document_ids) must not fail the worker.
        # The parse + persist already succeeded.
        _log.warning(
            "worker.cross_statement.detector_failed document_id=%s",
            document_id,
            exc_info=True,
        )
        return

    result.cross_statement_patterns = patterns

    if not patterns:
        return

    codes = ",".join(p.code for p in patterns)
    _log.info(
        "cross_statement_signals_detected count=%d codes=%s",
        len(patterns),
        codes,
    )

    # U22 — persist each Pattern as one merchants_shadow_signals row.
    # Persistence is best-effort per the U12 shadow contract: a
    # Supabase failure on a shadow-signal write must NOT abort the
    # upload (the parse + persist already succeeded, and the in-memory
    # ``cross_statement_patterns`` list is still available to callers
    # that read it directly). Each Pattern gets its own try/except so
    # a partial-failure case still persists the signals that did land.
    for pattern in patterns:
        try:
            record_shadow_signal(
                shadow_signals_repo,
                audit,
                merchant_id=doc_after.merchant_id,
                signal_code=pattern.code,
                signal_severity=pattern.severity,
                detail=pattern.detail,
                source_document_id=document_id,
                source_ids=list(pattern.source_ids),
                metadata={"emitted_by": "cross_statement_detector"},
                detected_by="worker",
            )
        except Exception:
            # Code-only log line — NEVER include pattern.detail (may
            # carry holder PII for related_account_suspected) or the
            # merchant_id (mid-PII; the dossier path renders the
            # signals so the operator already has the surface).
            _log.warning(
                "worker.cross_statement.persistence_failed code=%s",
                pattern.code,
                exc_info=True,
            )


def _audit_tampering_evaluation(
    *,
    audit: AuditLog,
    document_id: UUID,
    evaluation: TamperingEvaluation,
) -> None:
    """Write one audit row reflecting the tampering composition outcome.

    Action depends on the configured mode:

    * ``shadow`` (default) — ``tampering_would_decline``. The deal still
      scores as if the rule did not exist; this is a measurement-only
      surface for the operator to inspect the matrix before flipping to
      live decline.
    * ``live`` — ``tampering_decline_applied``. The score path will
      surface ``bank_statement_tampering_confirmed`` as a hard decline.

    Mode is read on every parse so the operator can flip with a
    systemd-unit env change + restart, without a code deploy.
    """
    mode = get_settings().aegis_tampering_decline_mode
    action = "tampering_would_decline" if mode == "shadow" else "tampering_decline_applied"
    audit.record(
        actor="worker",
        action=action,
        subject_type="document",
        subject_id=document_id,
        details={
            "mode": mode,
            "branch": evaluation.branch,
            "metadata_score": evaluation.metadata_score,
            "math_score": evaluation.math_score,
            "contributing_failures": list(evaluation.contributing_failures),
            "rationale": evaluation.rationale,
        },
    )


def _finalize_or_flag_merchant_from_statement(
    *,
    merchants_repo: MerchantRepository,
    audit: AuditLog,
    document_id: UUID,
    merchant_id: UUID | None,
    extraction: Any | None,  # noqa: ANN401  # ExtractionPass1Result | None — Any keeps the worker's import surface narrow
) -> None:
    """Bank-path post-parse: lift the provisional merchant out of
    ``provisional``.

    Four outcomes, mirroring the four scenarios the design doc
    enumerated:

    1. ``merchant_id`` is ``None`` — this doc came in via the bearer
       ``/upload`` route or the legacy orphan path. There's no
       provisional to lift; return silently.

    2. ``extraction`` is ``None`` (page-router low-confidence
       fail-closed, or any other path that produced no extraction
       payload) OR ``account_holder`` is blank/whitespace — call
       ``mark_needs_manual_naming``. Audit ``merchant.needs_manual_naming``
       only if the rowcount comes back as 1 (i.e. the row was actually
       provisional). Reason ``blank_account_holder`` for the dashboard
       banner.

    3. Clean ``account_holder`` — call ``finalize_provisional`` with
       the stripped name. Audit ``merchant.finalized`` only if
       rowcount==1. Rowcount==0 means the row was already
       ``finalized`` (operator manual rename in between, idempotent
       no-op) — no audit row, no error.

    4. ``finalize_provisional`` raises — let it propagate. The caller's
       outer storage step still runs, so a finalize DB blip doesn't
       strand ciphertext.

    The rowcount gating is operator-required (design doc §6 + the
    chunk-A repository unit tests): a ``merchant.finalized`` audit row
    that didn't actually transition anything would silently corrupt
    the merchant history. Better to skip the audit than to lie.
    """
    if merchant_id is None:
        return

    # Extract the account_holder defensively — extraction may be None
    # (page-router fail-closed) and the summary may carry None for the
    # field even when extraction exists (rare).
    account_holder: str | None = None
    if extraction is not None:
        account_holder = extraction.statement.summary.account_holder
    clean_name = (account_holder or "").strip()

    if not clean_name:
        updated = merchants_repo.mark_needs_manual_naming(merchant_id=merchant_id)
        if updated == 1:
            audit.record(
                actor="worker",
                action="merchant.needs_manual_naming",
                subject_type="merchant",
                subject_id=merchant_id,
                details={
                    "source_document_id": str(document_id),
                    "reason": "blank_account_holder",
                    "account_holder_raw": account_holder,
                },
            )
        return

    updated = merchants_repo.finalize_provisional(merchant_id=merchant_id, business_name=clean_name)
    if updated == 1:
        audit.record(
            actor="worker",
            action="merchant.finalized",
            subject_type="merchant",
            subject_id=merchant_id,
            details={
                "business_name": clean_name,
                "source_document_id": str(document_id),
                "account_holder_raw": account_holder,
            },
        )


def _flag_provisional_for_manual_naming(
    *,
    merchants_repo: MerchantRepository,
    audit: AuditLog,
    repository: DocumentRepository,
    document_id: UUID,
    reason: str,
) -> None:
    """Surface a provisional merchant out of "still parsing" and into
    "needs your input" when the worker can't auto-name it.

    Called from FOUR places (two failure handlers x two parse paths)
    AND from the processor-branch SUCCESS tail — the name is
    ``flag_provisional_for_manual_naming``, NOT
    ``_on_failure``, because the processor success uses the same
    transition for the same UI reason (parse is done, name unknown).

    ``reason`` ∈ {parse_exception, parse_cancelled, processor_branch}.
    The bank-path post-persist success branch uses
    ``_finalize_or_flag_merchant_from_statement`` instead, which has
    its own ``blank_account_holder`` reason.

    Defensive contract:
      * Document row missing (rare — failure during initial verify) →
        return silently. Nothing to flag.
      * Document has no merchant_id (bearer/orphan upload) →
        return silently.
      * ``mark_needs_manual_naming`` rowcount=0 (already finalized or
        already needs_manual_naming) → no audit row. Idempotent.
      * Audit-write failures during cleanup paths must not mask the
        caller's original exception — wrap and swallow with a
        CRITICAL log so the journal still surfaces the issue. The
        original exception re-raises one frame up.
    """
    try:
        doc = repository.get_document(document_id)
    except Exception:
        # Doc row vanished or DB is unreachable — nothing to do; let
        # the caller's original failure path propagate.
        _log.warning(
            "worker.merchant_flag.doc_lookup_failed document_id=%s reason=%s",
            document_id,
            reason,
        )
        return

    if doc.merchant_id is None:
        return

    try:
        updated = merchants_repo.mark_needs_manual_naming(merchant_id=doc.merchant_id)
    except Exception:
        # DB blip on the UPDATE — log and bail. The caller's exception
        # is the load-bearing failure; we don't add a second exception
        # type on top of it.
        _log.critical(
            "worker.merchant_flag.update_failed document_id=%s merchant_id=%s reason=%s",
            document_id,
            doc.merchant_id,
            reason,
            exc_info=True,
        )
        return

    if updated != 1:
        return

    try:
        audit.record(
            actor="worker",
            action="merchant.needs_manual_naming",
            subject_type="merchant",
            subject_id=doc.merchant_id,
            details={
                "source_document_id": str(document_id),
                "reason": reason,
            },
        )
    except Exception:
        # Same swallow rationale as
        # storage_objects._classify_and_emit_bucket_check_failure: a
        # cleanup-time audit-write failure must not mask the caller's
        # original cancellation/exception. Logged at CRITICAL so the
        # journal still catches it.
        _log.critical(
            "worker.merchant_flag.audit_write_failed document_id=%s merchant_id=%s reason=%s",
            document_id,
            doc.merchant_id,
            reason,
            exc_info=True,
        )


def _load_merchant_context(
    *,
    merchants_repo: MerchantRepository,
    merchant_id: UUID | None,
) -> MerchantContext | None:
    """Read the merchant's free-text context columns into a
    :class:`MerchantContext` for the extraction prompt.

    Returns ``None`` when:
      * the document has no merchant (bearer / orphan upload), OR
      * the merchant lookup fails for any reason (deleted, missing),
      * the merchant row exists but every context field is empty.

    A None return short-circuits the prompt builder — equivalent to "no
    MERCHANT CONTEXT block". Failure to read a merchant row never fails
    the parse — the prompt simply lacks the context block.
    """
    if merchant_id is None:
        return None
    try:
        merchant = merchants_repo.get(merchant_id)
    except Exception:
        # Best-effort. A deleted / missing merchant should not block
        # an otherwise valid parse. The doc's merchant_id can lag the
        # merchant row in rare paths (e.g. operator deleted between
        # upload and parse start) — the lack of context is a soft
        # degradation, not a parse failure.
        _log.warning(
            "worker.merchant_context.load_failed merchant_id=%s",
            merchant_id,
            exc_info=True,
        )
        return None
    ctx = MerchantContext(
        deal_context=merchant.deal_context,
        close_lead_description=merchant.close_lead_description,
        close_notes_summary=merchant.close_notes_summary,
        close_call_transcripts=merchant.close_call_transcripts,
    )
    if ctx.is_empty():
        return None
    return ctx


def _run_pipeline_with_merchant_context(
    pdf_path: str,
    llm: LLMClient,
    merchant_context: MerchantContext | None,
) -> PipelineResult:
    """Thin wrapper so ``asyncio.to_thread`` gets a single callable
    signature it can pass positional args to.

    Exists because ``run_pipeline`` takes ``merchant_context`` as a
    keyword-only argument and ``to_thread`` would otherwise need an
    inline lambda — extracting it makes the worker call site easier
    to read.

    Only forwards ``merchant_context`` when it's non-None so existing
    test stubs (``monkeypatch.setattr("aegis.workers.run_pipeline",
    fake_fn)``) whose ``fake_fn`` signature predates Feature D keep
    working.
    """
    if merchant_context is None:
        return run_pipeline(pdf_path, llm)
    return run_pipeline(pdf_path, llm, merchant_context=merchant_context)


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
    meta_path.write_text(json.dumps({"document_id": str(document_id), **meta}, sort_keys=True))


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
    meta_path.write_text(json.dumps({"document_id": str(document_id), **meta}, sort_keys=True))


async def _try_pdf_store_step(
    *,
    pdf_path: str,
    document_id: UUID,
    pdf_store_repo: PdfStoreRepository,
    audit: AuditLog,
    plaintext_bytes: bytes | None = None,
) -> bool:
    """Seal the plaintext PDF and persist into Postgres ``pdf_store``.

    Runs immediately after ``persist_parse_result`` and BEFORE the
    legacy Supabase Storage step (which is what unlinks the local
    plaintext). Outcomes:

      * SUCCESS — row written; audit ``document.encrypted_stored`` with
        char-count audit details only (NEVER bytes, NEVER PII per
        CLAUDE.md). Returns ``True``. The caller proceeds to the
        Supabase Storage step.
      * FAILURE (``PdfStoreWriteError`` or ``CryptoConfigError``) — no
        row written; audit ``document.encrypted_store_failed`` with
        ``error_type`` + truncated message; returns ``False``. The
        caller skips the Supabase Storage step and leaves the local
        plaintext in place for ops inspection (the legacy step is what
        would have unlinked).

    ``plaintext_bytes`` is an optimization: the processor branch already
    read the file at the top of the handler, so pass through to avoid
    a second disk read. Bank branch passes ``None`` and we read here.

    NEVER raises — the parse + analyses are already persisted by the
    time this runs, and storage problems must not fail the job.
    """
    try:
        if plaintext_bytes is None:
            plaintext_bytes = await asyncio.to_thread(Path(pdf_path).read_bytes)
        row = await asyncio.to_thread(
            pdf_store_repo.store,
            document_id=document_id,
            plaintext=plaintext_bytes,
        )
    except PdfStoreWriteError as exc:
        # Transient-or-permanent persistence failure. Skip the Supabase
        # step and preserve the plaintext for retry; the operator sees
        # the audit row and can re-enqueue the parse once the cause is
        # fixed.
        audit.record(
            actor="worker",
            action="document.encrypted_store_failed",
            subject_type="document",
            subject_id=document_id,
            details={
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:500],
                "outcome": "preserve_local_plaintext",
            },
        )
        return False
    except CryptoConfigError as exc:
        # Boot-time config drift surfaced at runtime: the key version
        # the row was sealed under is no longer present in
        # ``/etc/aegis/aegis.env``. Audit + skip; the operator's fix is
        # an env-var change, not a code change.
        audit.record(
            actor="worker",
            action="document.encrypted_store_failed",
            subject_type="document",
            subject_id=document_id,
            details={
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:500],
                "outcome": "crypto_config_error",
            },
        )
        return False

    audit.record(
        actor="worker",
        action="document.encrypted_stored",
        subject_type="document",
        subject_id=document_id,
        details={
            # NEVER log the plaintext bytes or the ciphertext blob —
            # both are PII-adjacent. Char-count + key version are
            # sufficient for the audit story.
            "key_version": row.key_version,
            "byte_size_plaintext": row.byte_size_plaintext,
            "sha256_plaintext": row.sha256_plaintext,
        },
    )
    return True


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
            plaintext_bytes = await asyncio.to_thread(Path(pdf_path).read_bytes)
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
            await asyncio.to_thread(storage_objects.upload, storage_path, ciphertext)
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
                    "merchant_id": (str(merchant_id) if merchant_id else None),
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
                    "merchant_id": (str(merchant_id) if merchant_id else None),
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
            "ops.worker.encrypted_storage.unknown document_id=%s is_write_failure=%s",
            document_id,
            is_write_failure,
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
                        "write_failure_preserved" if is_write_failure else "best_effort_cleanup"
                    ),
                },
            )
        except Exception:
            _log.critical(
                "ops.worker.encrypted_storage.audit_also_failed document_id=%s",
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
        raise ValueError(f"reply payload top-level must be an object; got {type(payload).__name__}")
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
    ingested_via: IngestSource = "webhook" if ingested_via_raw == "webhook" else "operator_paste"

    audit: AuditLog = ctx.get("audit") or get_audit()
    llm: LLMClient = ctx.get("llm") or get_llm()
    reply_repo: FunderReplyRepository = (
        ctx.get("funder_reply_repository") or get_funder_reply_repository()
    )

    # Cost-tracking wrapper, mirroring parse_document. Only wraps the
    # real BedrockClient; test stubs keep their bare interface.
    if isinstance(llm, BedrockClient):
        llm = CostTrackingBedrockClient(
            inner=llm,
            audit=audit,
            document_id=document_id,
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
                str(result.stamped_override_id) if result.stamped_override_id is not None else None
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
            str(result.stamped_override_id) if result.stamped_override_id is not None else None
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
    merchants: MerchantRepository = ctx.get("merchants") or get_merchant_repository()
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
    #
    # Two-layer filter: the existing ALLOW list
    # (``filename_matches_statement_filter``) keeps the legacy posture
    # — only filenames containing ``statement`` / ``stmt`` / ``bank``
    # / ``estmt`` survive the first pass. THEN the DENY list
    # (``filename_is_non_statement``) catches the false-positive case
    # where a filename does match the allow list but ALSO contains a
    # non-statement term (e.g. ``Bank Statement Plus Voided Check
    # Cover.pdf``). The 2026-06-16 recovery pass surfaced enough of
    # these to be worth the per-attachment deny lookup; the deny list
    # is operator-curated and lives in ``close/field_map.py``.
    statement_candidates: list[CloseAttachment] = []
    for att in attachments:
        if not filename_matches_statement_filter(att.name, filename_filters):
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
            continue

        deny_term = filename_is_non_statement(att.name)
        if deny_term is not None:
            summary["skipped"] += 1
            audit.record(
                actor="worker",
                actor_email=actor_email,
                action="close.attachment.skipped_non_statement",
                subject_type="merchant",
                subject_id=merchant.id,
                details={
                    "attachment_id": att.id,
                    "filename": att.name,
                    "matched_deny_term": deny_term,
                    "close_lead_id": close_lead_id,
                },
            )
            continue

        statement_candidates.append(att)

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


_SUBMISSION_REMINDER_STALE_AFTER = timedelta(hours=24)


def run_submission_reminder_pass(
    *,
    audit: AuditLog,
    submissions: FunderNoteSubmissionRepository,
    merchants: MerchantRepository,
    funders: FunderRepository,
    close_client: CloseClient,
    now: datetime | None = None,
) -> dict[str, int]:
    """Post one Close task per stale pending funder-note submission.

    A submission is "stale" when ``status='pending'`` AND ``submitted_at``
    is older than 24h. Dedupe key is a ``close.task.submission_reminder``
    audit row keyed on the submission id — once a reminder fires it
    NEVER fires again for that submission, regardless of how long the
    funder takes to respond. (The operator clears the task in Close; if
    they want a second nudge they re-submit, which creates a new row.)

    Skip conditions:
      * Merchant lookup raises ``MerchantNotFoundError`` (concurrent
        delete) → structured log + skip.
      * Funder lookup raises ``FunderNotFoundError`` (catalog cleanup) →
        structured log + skip.
      * Merchant has no ``close_lead_id`` → silent skip (no Lead to drop
        the task on).
      * Prior ``close.task.submission_reminder`` audit row for this
        submission_id → silent skip (duplicate guard).

    Close API errors on the task POST are audited as
    ``close.task.submission_reminder_failed`` but do NOT raise — one bad
    Lead must not abort the whole cron pass (same posture as the renewal
    reminder).
    """
    now = now or datetime.now(UTC)
    threshold = now - _SUBMISSION_REMINDER_STALE_AFTER
    today = now.date()

    rows = submissions.list_pending_older_than(threshold)
    summary = {
        "considered": len(rows),
        "created": 0,
        "skipped_no_lead": 0,
        "skipped_dup": 0,
        "skipped_missing_merchant": 0,
        "skipped_missing_funder": 0,
        "failed": 0,
    }

    for row in rows:
        try:
            merchant = merchants.get(row.merchant_id)
        except MerchantNotFoundError:
            _log.warning(
                "submission_reminder.skip_missing_merchant submission_id=%s merchant_id=%s",
                row.id,
                row.merchant_id,
            )
            summary["skipped_missing_merchant"] += 1
            continue

        try:
            funder = funders.get(row.funder_id)
        except FunderNotFoundError:
            _log.warning(
                "submission_reminder.skip_missing_funder submission_id=%s funder_id=%s",
                row.id,
                row.funder_id,
            )
            summary["skipped_missing_funder"] += 1
            continue

        if not merchant.close_lead_id:
            summary["skipped_no_lead"] += 1
            continue

        if _has_prior_submission_reminder(audit, row.id):
            summary["skipped_dup"] += 1
            continue

        text = (
            f"Log funder response — {merchant.business_name} submitted to "
            f"{funder.name}. Was it approved, declined, or countered?"
        )
        try:
            response = close_client.create_task(
                lead_id=merchant.close_lead_id,
                text=text,
                due_date=today,
            )
        except CloseError as exc:
            summary["failed"] += 1
            audit.record(
                actor="cron.submission_reminder",
                action="close.task.submission_reminder_failed",
                subject_type="funder_note_submission",
                subject_id=row.id,
                details={
                    "submission_id": str(row.id),
                    "funder_id": str(row.funder_id),
                    "close_lead_id": merchant.close_lead_id,
                    "status_code": exc.status_code,
                    "error": str(exc)[:200],
                },
            )
            continue

        close_task_id = response.get("id") if isinstance(response, dict) else None
        audit.record(
            actor="cron.submission_reminder",
            action="close.task.submission_reminder",
            subject_type="funder_note_submission",
            subject_id=row.id,
            details={
                "submission_id": str(row.id),
                "funder_id": str(row.funder_id),
                "close_lead_id": merchant.close_lead_id,
                "close_task_id": close_task_id,
            },
        )
        summary["created"] += 1

    return summary


def _has_prior_submission_reminder(audit: AuditLog, submission_id: UUID) -> bool:
    """True if any ``close.task.submission_reminder`` audit row already
    exists for ``submission_id`` — used as the duplicate guard.

    The check uses ``subject_type='funder_note_submission'`` +
    ``subject_id=submission_id`` so the lookup is bounded even when an
    operator accumulates thousands of audit rows site-wide.
    """
    rows = audit.list_for_subject(
        subject_type="funder_note_submission",
        subject_id=submission_id,
        action="close.task.submission_reminder",
        limit=10,
    )
    sid = str(submission_id)
    return any((r.get("details") or {}).get("submission_id") == sid for r in rows)


async def run_submission_reminder_cron(ctx: dict[str, Any]) -> dict[str, int]:
    """arq daily cron entrypoint.

    Reads dependencies from the arq context (tests inject in-memory
    fakes) and falls back to the process-wide DI when not present —
    the same pattern ``run_renewal_reminder_cron`` and
    ``run_archive_cron`` use.
    """
    from aegis.api.deps import (
        get_audit,
        get_close_client,
        get_funder_note_submission_repository,
        get_funder_repository,
        get_merchant_repository,
    )

    audit = ctx.get("audit") or get_audit()
    submissions = ctx.get("submissions") or get_funder_note_submission_repository()
    merchants = ctx.get("merchants") or get_merchant_repository()
    funders = ctx.get("funders") or get_funder_repository()
    close_client = ctx.get("close_client") or get_close_client()

    summary = run_submission_reminder_pass(
        audit=audit,
        submissions=submissions,
        merchants=merchants,
        funders=funders,
        close_client=close_client,
    )
    _log.info(
        "submission_reminder.run considered=%s created=%s skipped_no_lead=%s "
        "skipped_dup=%s skipped_missing_merchant=%s skipped_missing_funder=%s failed=%s",
        summary["considered"],
        summary["created"],
        summary["skipped_no_lead"],
        summary["skipped_dup"],
        summary["skipped_missing_merchant"],
        summary["skipped_missing_funder"],
        summary["failed"],
    )
    return summary


class WorkerSettings:
    """arq config. Reads concurrency + timeout from env via Settings."""

    functions = (parse_document, process_funder_reply, process_close_attachments)
    # Crons:
    #   * 02:00 UTC daily — audit retention archiver (master plan §17).
    #   * 09:00 UTC daily — renewal reminder: one Close task per renewing
    #     merchant per calendar month. Operator's daily start; the
    #     task lands in the morning queue.
    #   * 17:00 UTC daily — submission reminder: one Close task per
    #     pending ``funder_note_submission`` that's >24h old, deduped
    #     forever per submission. Late-day fire so the operator gets
    #     the nudge for everything submitted earlier in their day.
    #
    # NOT registered here: the funder folder monitor. The OneDrive
    # guidelines folder lives on the operator's Windows box and isn't
    # mounted on the Hetzner prod box, so running it as a server arq
    # cron would only ever produce graceful-skip audit rows. It now
    # runs as a Windows Task Scheduler job — see
    # ``deploy/windows/INSTALL_FUNDER_MONITOR.md``. The cron entrypoint
    # ``aegis.funders.monitor.run_funder_monitor_cron`` is kept in
    # place so a future Windows-side arq worker could register it
    # without a code change.
    cron_jobs = (
        cron(run_archive_cron, hour=2, minute=0, run_at_startup=False),
        cron(run_renewal_reminder_cron, hour=9, minute=0, run_at_startup=False),
        cron(run_submission_reminder_cron, hour=17, minute=0, run_at_startup=False),
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
    "run_submission_reminder_cron",
    "run_submission_reminder_pass",
]
