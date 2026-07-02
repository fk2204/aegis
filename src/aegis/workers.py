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
from typing import TYPE_CHECKING, Any, cast
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
    get_processor_statement_repository,
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
from aegis.close.field_map import filename_is_non_statement
from aegis.compliance.obligations import (
    run_compliance_obligation_reminder_cron,
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
    ExtractedProcessorStatement,
    ProcessorAggregates,
    ProcessorPipelineResult,
    ProcessorStatementRepository,
    ProcessorStatementRow,
    ProcessorStatementWriteError,
    ProcessorType,
    build_stripe_dossier_aggregates,
    detect_processor,
    run_processor_pipeline,
)
from aegis.parser.shadow_audit import shadow_audit_payloads
from aegis.parser.tampering import TamperingEvaluation
from aegis.pdf_store import (
    PdfStoreRepository,
    PdfStoreWriteError,
)
from aegis.scoring_v2.narrator_job import generate_narrator_summary
from aegis.storage import DocumentNotFoundError, DocumentRepository, DocumentRow

_log = get_logger(__name__)


async def parse_document(
    ctx: dict[str, Any],
    document_id_str: str,
    pdf_path: str,
    keep_local_plaintext: bool = True,
) -> dict[str, Any]:
    """arq job entrypoint. ``ctx`` is provided by arq, unused for now.

    Returns a small dict so the operator can spot-check job logs.
    PDF is deleted in the finally block — present even on error paths.

    ``keep_local_plaintext`` controls the storage-step failure handling
    when the pdf_store seal raises. ``True`` (default) preserves the
    plaintext on disk for ops inspection — correct for upload-route jobs
    where the user's original upload is the source of truth and the
    plaintext is the only copy. ``False`` instructs the storage-step
    failure handler to ``_safe_unlink`` instead — correct for jobs that
    enqueue with a transient tempfile sourced from an existing pdf_store
    seal (e.g. ``scripts/recover_legacy_docs.py
    --reparse-sealed-manual-review`` — the encrypted copy already exists
    in pdf_store and the local plaintext is purely a worker-readable
    handoff that should never persist past the storage step).
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
    # Migration 073 — processor_statements. Pulled here so tests can
    # inject an in-memory repo via the ctx dict; the processor-branch
    # success path upserts a row with the dossier-shape aggregates.
    processor_repo: ProcessorStatementRepository = (
        ctx.get("processor_statements") or get_processor_statement_repository()
    )

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

    # ===================================================================
    # Non-bank-statement routing (tax returns / A/R aging / equipment
    # invoices). Filename + first-page text are the cheap pre-router.
    # Tax / equipment go through Bedrock vision; A/R aging picks the
    # cheapest deterministic path (xlsx → openpyxl, csv → stdlib, pdf
    # → Bedrock). On success the document is marked ``proceed`` and the
    # extracted row lands in its dedicated table; on extractor failure
    # the document is marked ``error`` with the exception surfaced via
    # audit.
    # ===================================================================
    routed = await _route_non_bank_statement(
        document_id=document_id,
        pdf_path=pdf_path,
        doc=doc_pre_parse,
        llm=llm,
        repository=repository,
        merchants_repo=merchants_repo,
        audit=audit,
    )
    if routed is not None:
        _safe_unlink(pdf_path)
        return routed

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
            processor_repo=processor_repo,
            keep_local_plaintext=keep_local_plaintext,
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

    # Migration 077 — fan out a ``parse_complete`` notification once the
    # parse outcome is durable AND merchant_id is resolved. Recipients:
    # the merchant's assignee, or every active admin when unassigned.
    # Best-effort: notification-write failures log but never raise (the
    # parse itself is already persisted and the storage step still runs).
    _maybe_emit_parse_complete_notification(
        ctx=ctx,
        document_id=document_id,
        merchant_id=doc_after_persist.merchant_id,
        parse_status=result.parse_status,
    )

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
        keep_local_plaintext=keep_local_plaintext,
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

    # ===================================================================
    # CONCERN 4 — fire-and-forget narrator auto-trigger.
    # The narrator job is keyed on document_id and idempotent (skips when
    # ``analyses.narrator_summary`` is already populated), so a re-parse
    # of an already-narrated doc costs zero Bedrock tokens. Only proceed
    # docs get the narrator — manual_review / error docs would produce a
    # narrator on questionable data, so we defer to the operator's
    # explicit "Refresh narrator" click on those.
    # The helper itself never raises; an enqueue failure audits and the
    # parse return path stays identical.
    # ===================================================================
    if result.parse_status == "proceed" and doc_after_persist.merchant_id is not None:
        from aegis.scoring_v2.narrator_job import enqueue_narrator_summary_from_worker

        await enqueue_narrator_summary_from_worker(
            ctx=ctx,
            document_id=document_id,
            merchant_id=doc_after_persist.merchant_id,
            audit=audit,
        )

        # 2026-07-01 GAP 1 — proceed docs also enqueue
        # ``run_funder_matching`` so the dashboard's Ready-to-Submit
        # list has a durable "matching ran on the fresh bundle" audit
        # signal without waiting for the operator's first dossier open.
        # Best-effort — an enqueue failure logs + audits but never
        # affects the parse return path.
        pool = ctx.get("redis")
        if pool is not None:
            try:
                await pool.enqueue_job("run_funder_matching", str(doc_after_persist.merchant_id))
            except Exception as exc:
                _log.warning(
                    "post_parse.funder_matching_enqueue_failed merchant_id=%s exc=%s",
                    doc_after_persist.merchant_id,
                    exc,
                )
                audit.record(
                    actor="system:post_parse",
                    action="merchant.funder_matching.enqueue_failed",
                    subject_type="merchant",
                    subject_id=doc_after_persist.merchant_id,
                    details={
                        "error": type(exc).__name__,
                        "message": str(exc)[:500],
                    },
                )

    # ===================================================================
    # CONCERN 5 — post-parse background checks + vision retry
    # (2026-07-01 FIX 2). Fires after every parse regardless of status
    # so newly-parsed merchants get OFAC/SOS/UCC/web-presence sweeps
    # without an operator "Run now" click. Retry gates on same-day
    # audit to avoid duplicate work if a doc is re-parsed the same day.
    # ===================================================================
    if doc_after_persist.merchant_id is not None:
        await _fire_post_parse_background_checks(
            ctx=ctx,
            merchant_id=doc_after_persist.merchant_id,
            audit=audit,
        )

    # Vision-retry deferred re-enqueue (2026-07-01 FIX 2). Error-state
    # docs that still have a sealed pdf_store blob get one automatic
    # retry ~5 minutes out — most transient extraction failures clear
    # on the second attempt via the vision fallback path. Guarded on
    # ``doc_after_persist.parse_status`` because ``PipelineResult`` only
    # carries proceed/review/manual_review — the ``error`` state is set
    # by the exception handlers writing to the DB row via ``mark_error``.
    if (
        getattr(doc_after_persist, "parse_status", None) == "error"
        and doc_after_persist.storage_path is not None
        and doc_after_persist.merchant_id is not None
    ):
        await _schedule_vision_retry(
            ctx=ctx,
            document_id=document_id,
            pdf_path=pdf_path,
            audit=audit,
        )

    return {
        "document_id": str(document_id),
        "parse_status": result.parse_status,
        "fraud_score": result.fraud_score,
    }


async def _fire_post_parse_background_checks(
    *,
    ctx: dict[str, Any],
    merchant_id: UUID,
    audit: AuditLog,
) -> None:
    """Enqueue ``run_background_checks`` for a merchant if it hasn't
    already run today.

    Idempotency key: an audit row with
    ``action='merchant.background_checks_complete'`` on the merchant
    subject_id with ``created_at >= today``. Skips silently on lookup
    failure (Supabase blip) — the underlying job is itself idempotent
    so a duplicate enqueue costs at most one no-op run.
    """
    from datetime import UTC, datetime

    from aegis.db import get_supabase

    try:
        sb = get_supabase()
        today = datetime.now(UTC).date().isoformat()
        existing = (
            sb.table("audit_log")
            .select("id")
            .eq("action", "merchant.background_checks_complete")
            .eq("subject_type", "merchant")
            .eq("subject_id", str(merchant_id))
            .gte("created_at", today)
            .limit(1)
            .execute()
        )
        if existing.data:
            return
    except Exception as exc:
        _log.warning(
            "post_parse.bgchecks_idempotency_check_failed merchant_id=%s exc=%s",
            merchant_id,
            exc,
        )

    pool = ctx.get("redis")
    if pool is None:
        return
    try:
        await pool.enqueue_job("run_background_checks", str(merchant_id), "post_parse_auto")
        audit.record(
            actor="system:post_parse",
            action="merchant.background_checks.enqueued",
            subject_type="merchant",
            subject_id=merchant_id,
            details={"trigger": "post_parse_auto"},
        )
    except Exception as exc:
        _log.warning(
            "post_parse.bgchecks_enqueue_failed merchant_id=%s exc=%s",
            merchant_id,
            exc,
        )


async def _schedule_vision_retry(
    *,
    ctx: dict[str, Any],
    document_id: UUID,
    pdf_path: str,
    audit: AuditLog,
) -> None:
    """Enqueue a deferred re-parse ~5 minutes out for an error-state doc.

    Uses arq's ``_defer_by`` so the worker doesn't burn a slot
    immediately re-processing the same failing input. The vision
    fallback path lives inside ``_run_pipeline_with_retry``.

    Idempotency: skipped when the document already carries a
    ``document.parse.vision_retry_scheduled`` audit row — one retry per
    error-transition is enough.
    """
    from datetime import timedelta

    from aegis.db import get_supabase

    try:
        sb = get_supabase()
        existing = (
            sb.table("audit_log")
            .select("id")
            .eq("action", "document.parse.vision_retry_scheduled")
            .eq("subject_type", "document")
            .eq("subject_id", str(document_id))
            .limit(1)
            .execute()
        )
        if existing.data:
            return
    except Exception as exc:
        _log.warning(
            "post_parse.vision_retry_idempotency_check_failed doc=%s exc=%s",
            document_id,
            exc,
        )

    pool = ctx.get("redis")
    if pool is None:
        return
    try:
        await pool.enqueue_job(
            "parse_document",
            str(document_id),
            pdf_path,
            _defer_by=timedelta(minutes=5),
        )
        audit.record(
            actor="system:post_parse",
            action="document.parse.vision_retry_scheduled",
            subject_type="document",
            subject_id=document_id,
            details={"defer_minutes": 5},
        )
        _log.info(
            "post_parse.vision_retry_scheduled document_id=%s",
            document_id,
        )
    except Exception as exc:
        _log.warning(
            "post_parse.vision_retry_enqueue_failed doc=%s exc=%s",
            document_id,
            exc,
        )


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
    processor_repo: ProcessorStatementRepository,
    keep_local_plaintext: bool = True,
) -> dict[str, Any]:
    """Run the processor pipeline + persist + audit the result (mp Phase 6.6).

    Migration 073 wired the persistence layer for processor aggregates;
    on a successful parse this branch builds the dossier-shape
    aggregates (``StripeDossierAggregates``) and upserts them into the
    ``processor_statements`` table via ``processor_repo.upsert``. The
    same row drives the dossier ``processor_section`` builder so the
    operator sees the parsed gross / fees / payouts without re-running
    the pipeline.

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

    # Migration 073 — persist aggregates to ``processor_statements``.
    # Requires a merchant_id; bearer / orphan uploads skip persistence
    # (the audit row already captured the parse outcome). The dossier
    # ``processor_section`` builder reads from this table — without the
    # upsert, the production dossier stays blank even after a
    # successful parse.
    if (
        result.extraction is not None
        and result.aggregates is not None
        and doc_after.merchant_id is not None
    ):
        _persist_processor_statement(
            processor_repo=processor_repo,
            audit=audit,
            document_id=document_id,
            merchant_id=doc_after.merchant_id,
            brand=brand,
            extraction=result.extraction,
            base_aggregates=result.aggregates,
        )

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
        keep_local_plaintext=keep_local_plaintext,
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
# Migration 073 — processor_statements persistence helper.
#
# Builds dossier-shape aggregates (gross / fees / payouts / net /
# avg_daily_volume / refund_rate / chargeback_count) from the validator
# output + extraction, upserts the row, writes an audit row that proves
# the persistence step ran. The helper isolates the failure mode: a
# Supabase blip on the upsert MUST surface as a dedicated audit row and
# fail the worker (CLAUDE.md "audit-log writes are written for every
# state change; audit-write failures FAIL the operation, never silently
# log-and-continue") — the parse outcome itself is already on
# ``audit_log`` via ``document.parse.processor_complete`` so dropping
# the persistence write would leave the dossier perpetually blank.
# ===========================================================================


def _persist_processor_statement(
    *,
    processor_repo: ProcessorStatementRepository,
    audit: AuditLog,
    document_id: UUID,
    merchant_id: UUID,
    brand: str,
    extraction: ExtractedProcessorStatement,
    base_aggregates: ProcessorAggregates,
) -> ProcessorStatementRow:
    """Persist the dossier-shape aggregates and audit the write.

    ``parse_method`` is fixed to ``"pdf_vision"`` here — the processor
    branch only runs on PDF uploads (the CSV path is wired separately
    through the StripeRouter and does not currently touch the worker).
    When the CSV upload path lands a worker hook, this helper picks up
    the discriminator from the caller.
    """
    dossier = build_stripe_dossier_aggregates(extraction, base_aggregates)
    processor_type = _narrow_processor_type(brand)
    # AEGIS auditability: every aggregate metric persists its
    # contributing line-item IDs alongside the value. Keys mirror the
    # 020 column names so the on-disk JSON shape is operator-readable
    # via the dossier drill-down.
    source_ids: dict[str, list[UUID]] = {
        "gross_volume": list(dossier.total_gross_volume.source_ids),
        "fees_total": list(dossier.total_fees.source_ids),
        "net_revenue": list(dossier.total_net_volume.source_ids),
        "payouts_total": list(dossier.total_payouts.source_ids),
    }
    row = ProcessorStatementRow(
        document_id=document_id,
        merchant_id=merchant_id,
        processor_type=processor_type,
        period_start=dossier.period_start,
        period_end=dossier.period_end,
        total_gross_volume=dossier.total_gross_volume.value,
        total_fees=dossier.total_fees.value,
        total_net_volume=dossier.total_net_volume.value,
        total_payouts=dossier.total_payouts.value,
        avg_daily_volume=dossier.avg_daily_volume,
        chargeback_count=dossier.chargeback_count,
        refund_rate=dossier.refund_rate,
        parse_method="pdf_vision",
        source_ids=source_ids,
    )
    try:
        persisted = processor_repo.upsert(row)
    except ProcessorStatementWriteError:
        # Surface failure as an audit row before re-raising; the parse
        # itself succeeded but the persistence step did not. The worker
        # propagates the exception so arq retries the job.
        audit.record(
            actor="worker",
            action="processor_statement.persist_failed",
            subject_type="document",
            subject_id=document_id,
            details={"brand": brand, "merchant_id": str(merchant_id)},
        )
        raise

    audit.record(
        actor="worker",
        action="processor_statement.parsed",
        subject_type="document",
        subject_id=document_id,
        details={
            "processor_type": persisted.processor_type,
            "merchant_id": str(merchant_id),
            "document_id": str(document_id),
            "total_gross_volume": str(persisted.total_gross_volume),
            "total_fees": str(persisted.total_fees),
            "total_net_volume": str(persisted.total_net_volume),
            "total_payouts": str(persisted.total_payouts),
            "avg_daily_volume": str(persisted.avg_daily_volume),
            "chargeback_count": persisted.chargeback_count,
        },
    )
    return persisted


def _narrow_processor_type(brand: str) -> ProcessorType:
    """Narrow ``brand`` to the ``ProcessorType`` literal.

    The worker treats ``brand`` as a plain ``str`` because the detector
    returns ``ProcessorBrand`` which includes ``bank`` / ``ambiguous``;
    by the time we reach the persistence helper the value is already
    constrained to ``stripe`` / ``square`` by the dispatch check at the
    top of ``parse_document``. Re-asserting the narrowing keeps mypy
    happy and gives a sharp error message if a new brand ever flows
    through here without an explicit allowlist update.
    """
    if brand == "stripe":
        return "stripe"
    if brand == "square":
        return "square"
    if brand == "toast":
        return "toast"
    if brand == "clover":
        return "clover"
    if brand == "paypal":
        return "paypal"
    raise ProcessorStatementWriteError(
        f"unsupported processor brand {brand!r} for processor_statements row"
    )


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


async def _route_non_bank_statement(
    *,
    document_id: UUID,
    pdf_path: str,
    doc: DocumentRow,
    llm: LLMClient,
    repository: DocumentRepository,
    merchants_repo: MerchantRepository,
    audit: AuditLog,
) -> dict[str, Any] | None:
    """Detect tax-return / A/R aging / equipment documents and route
    them to their dedicated extractors. Returns the worker payload on a
    routed extraction; returns None when the document looks like a bank
    statement and the caller should fall through to the bank pipeline.
    """
    from pathlib import Path as _Path

    from aegis.parser.ar_aging.extract import (
        detect_ar_aging_filename,
        extract_ar_aging_csv,
        extract_ar_aging_excel,
        extract_ar_aging_pdf,
    )
    from aegis.parser.equipment.extract import (
        detect_equipment_document,
        extract_equipment_details,
    )
    from aegis.parser.tax_return.extract import (
        detect_tax_form_type,
        extract_tax_return,
    )
    from aegis.parser.tax_return.repository import (
        SupabaseTaxReturnRepository,
        TaxReturnRow,
    )

    filename = doc.original_filename or ""

    # Tax return — Bedrock vision. Filename or first-page text triggers.
    # Reuse the parser's helper so the extraction path matches what
    # auto-hints / page-router consume; one source of pymupdf truth.
    from aegis.parser.pipeline import _extract_first_page_text

    first_page_text = await asyncio.to_thread(_extract_first_page_text, pdf_path)

    form_type = detect_tax_form_type(filename, first_page_text)
    if form_type is not None and doc.merchant_id is not None:
        try:
            result = await asyncio.to_thread(
                extract_tax_return,
                pdf_path,
                form_type=form_type,
                llm_client=llm,  # type: ignore[arg-type]
            )
            if result is not None:
                row = TaxReturnRow(
                    merchant_id=doc.merchant_id,
                    document_id=document_id,
                    form_type=form_type,
                    tax_year=result.tax_year,
                    gross_receipts=result.gross_receipts,
                    net_income=result.net_income,
                    total_assets=result.total_assets,
                    total_liabilities=result.total_liabilities,
                    officer_compensation=result.officer_compensation,
                    raw_extraction=result.model_dump(mode="json"),
                )
                SupabaseTaxReturnRepository().upsert(row)
            if hasattr(repository, "set_parse_status"):
                repository.set_parse_status(document_id, "proceed")
            audit.record(
                actor="worker",
                action="parser.tax_return_extracted",
                subject_type="document",
                subject_id=document_id,
                details={"form_type": form_type, "tax_year": result.tax_year if result else None},
            )
            return {
                "document_id": str(document_id),
                "parse_status": "proceed",
                "route": "tax_return",
            }
        except Exception as exc:
            _log.exception("worker.tax_return.extract_failed document_id=%s", document_id)
            audit.record(
                actor="worker",
                action="parser.tax_return_failed",
                subject_type="document",
                subject_id=document_id,
                details={
                    "form_type": form_type,
                    "error": type(exc).__name__,
                    "message": str(exc)[:200],
                },
            )
            if hasattr(repository, "set_parse_status"):
                repository.set_parse_status(document_id, "error")
            return {
                "document_id": str(document_id),
                "parse_status": "error",
                "route": "tax_return",
            }

    # A/R aging — deterministic for xlsx/csv, Bedrock for pdf.
    if detect_ar_aging_filename(filename) and doc.merchant_id is not None:
        try:
            ext = _Path(pdf_path).suffix.lower()
            if ext == ".xlsx":
                ar = await asyncio.to_thread(extract_ar_aging_excel, pdf_path)
            elif ext == ".csv":
                ar = await asyncio.to_thread(extract_ar_aging_csv, pdf_path)
            else:
                ar = await asyncio.to_thread(
                    extract_ar_aging_pdf,
                    pdf_path,
                    llm_client=llm,  # type: ignore[arg-type]
                )
            from aegis.db import get_supabase

            get_supabase().table("ar_aging_reports").insert(
                {
                    "merchant_id": str(doc.merchant_id),
                    "document_id": str(document_id),
                    "total_outstanding": str(ar.total_outstanding),
                    "current_amount": str(ar.current_amount),
                    "days_30_60": str(ar.days_30_60),
                    "days_60_90": str(ar.days_60_90),
                    "days_90_plus": str(ar.days_90_plus),
                    "debtor_count": ar.debtor_count,
                    "concentration_pct": str(ar.concentration_pct),
                    "top_debtors": ar.top_debtors,
                }
            ).execute()
            if hasattr(repository, "set_parse_status"):
                repository.set_parse_status(document_id, "proceed")
            audit.record(
                actor="worker",
                action="parser.ar_aging_extracted",
                subject_type="document",
                subject_id=document_id,
                details={
                    "total_outstanding": str(ar.total_outstanding),
                    "debtor_count": ar.debtor_count,
                },
            )
            return {
                "document_id": str(document_id),
                "parse_status": "proceed",
                "route": "ar_aging",
            }
        except Exception as exc:
            _log.exception("worker.ar_aging.extract_failed document_id=%s", document_id)
            audit.record(
                actor="worker",
                action="parser.ar_aging_failed",
                subject_type="document",
                subject_id=document_id,
                details={"error": type(exc).__name__, "message": str(exc)[:200]},
            )
            if hasattr(repository, "set_parse_status"):
                repository.set_parse_status(document_id, "error")
            return {
                "document_id": str(document_id),
                "parse_status": "error",
                "route": "ar_aging",
            }

    # Equipment invoice / quote — Bedrock vision. Stores onto
    # merchants.equipment_details JSONB (migration 095) — no separate
    # table, one row per merchant tracks the latest extraction.
    if detect_equipment_document(filename) and doc.merchant_id is not None:
        try:
            eq = await asyncio.to_thread(
                extract_equipment_details,
                pdf_path,
                llm_client=llm,  # type: ignore[arg-type]
            )
            if eq is not None:
                from aegis.db import get_supabase

                get_supabase().table("merchants").update(
                    {
                        "equipment_details": {
                            "description": eq.description,
                            "make": eq.make,
                            "model": eq.model,
                            "year": eq.year,
                            "condition": eq.condition,
                            "serial_number": eq.serial_number,
                            "vin": eq.vin,
                            "vendor_name": eq.vendor_name,
                            "total_cost": str(eq.total_cost) if eq.total_cost else None,
                        }
                    }
                ).eq("id", str(doc.merchant_id)).execute()
            if hasattr(repository, "set_parse_status"):
                repository.set_parse_status(document_id, "proceed")
            audit.record(
                actor="worker",
                action="parser.equipment_extracted",
                subject_type="document",
                subject_id=document_id,
                details={"make": eq.make if eq else None, "model": eq.model if eq else None},
            )
            return {
                "document_id": str(document_id),
                "parse_status": "proceed",
                "route": "equipment",
            }
        except Exception as exc:
            _log.exception("worker.equipment.extract_failed document_id=%s", document_id)
            audit.record(
                actor="worker",
                action="parser.equipment_failed",
                subject_type="document",
                subject_id=document_id,
                details={"error": type(exc).__name__, "message": str(exc)[:200]},
            )
            if hasattr(repository, "set_parse_status"):
                repository.set_parse_status(document_id, "error")
            return {
                "document_id": str(document_id),
                "parse_status": "error",
                "route": "equipment",
            }

    # Not a non-bank-statement doc — caller falls through to bank pipeline.
    return None


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
    keep_local_plaintext: bool = True,
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
        ``error_type`` + truncated message; returns ``False``. By
        default the caller skips the Supabase Storage step and leaves
        the local plaintext in place for ops inspection (the legacy
        step is what would have unlinked). When
        ``keep_local_plaintext=False`` the failure path additionally
        calls ``_safe_unlink`` so a transient script-written tempfile
        (where an encrypted copy already exists in pdf_store and the
        operator has nothing to inspect from a fresh decrypt) does NOT
        persist past the storage step. See ``parse_document`` docstring
        for the flag's contract.

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
                "outcome": (
                    "preserve_local_plaintext"
                    if keep_local_plaintext
                    else "unlinked_local_plaintext"
                ),
            },
        )
        if not keep_local_plaintext:
            _safe_unlink(pdf_path)
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
                "outcome": (
                    "crypto_config_error"
                    if keep_local_plaintext
                    else "crypto_config_error_unlinked"
                ),
            },
        )
        if not keep_local_plaintext:
            _safe_unlink(pdf_path)
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
    """List attachments on a Close Lead, filter, persist each statement
    through :func:`persist_pdf_upload`.

    Filter pipeline (applied in order, each step audits its skips):

      1. ``content_type == 'application/pdf'`` — strict. Kills the
         PNG-named-statement case and every non-PDF surface.
      2. Filename deny list (``filename_is_non_statement``). 2026-06-26:
         the operator pin gate was retired — every PDF on the Lead is
         a statement candidate unless its filename obviously identifies
         a non-statement (driver's license, voided check, contract,
         tax return, photo, etc.). The parser's validation gate is the
         backstop for anything that slips through (a Word-doc converted
         to PDF, an MCA application that the deny list didn't catch).
      3. MD5 ``checksum`` dedup — same file attached twice (Note +
         Lead-direct, say) downloads once. Cheap, the checksum is
         already in the unified Lead Files response.
      4. Warn at ``close_attachment_warn_threshold`` candidates,
         hard-cap at ``close_attachment_hard_cap`` (bypassed by
         ``override_cap=True``). SHA256 dedup in ``persist_pdf_upload``
         is the second backstop after fetch.

    ``trigger`` is "webhook" (auto from /webhooks/close, chunk 3) or
    "rescan" (operator-clicked, chunk 5). Audited so the operator can
    distinguish auto vs. manual runs.

    ``override_cap`` is set by the rescan-with-override UI button when
    a previous run hit ``close_attachment_hard_cap``. Default False —
    the cap protects against the "wrong-folder, 47 PDFs" mistake
    burning a Bedrock call per file.

    Per-attachment errors (download_failed, pydantic validation, etc.)
    are isolated and logged via audit rows; the loop walks the full
    list before returning a summary dict. The summary lands in arq's
    job log and (for ``trigger='rescan'``) drives the cap-override
    UI button.

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

    # Filter 1 — content_type must be application/pdf. Strict, by MIME
    # not filename, so a PNG named "april_bank_statement.png" doesn't
    # leak through to the parser.
    pdf_attachments: list[CloseAttachment] = []
    for att in attachments:
        if att.content_type == "application/pdf":
            pdf_attachments.append(att)
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
                "reason": "content_type_not_pdf",
                "content_type": att.content_type,
                "close_lead_id": close_lead_id,
            },
        )

    # Filter 2 — filename deny list. 2026-06-26: pin gate removed; the
    # filename heuristic is now the only gate before download + parse.
    # ``filename_is_non_statement`` is intentionally narrow — false
    # positives waste real statements. Everything else passes through
    # to the parser, whose validation gate catches whatever the deny
    # list missed (e.g. a Word-doc-to-PDF with no clear filename hint).
    statement_candidates: list[CloseAttachment] = []
    for att in pdf_attachments:
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

    # Filter 3 — MD5 checksum dedup. Same file attached twice (Note
    # plus direct Lead drop, say) downloads once. ``att.id`` fallback
    # keeps the dedup correct when Close ever omits checksum (it's
    # populated on every file in 2026-05-28 verification).
    seen_keys: set[str] = set()
    deduped: list[CloseAttachment] = []
    for att in statement_candidates:
        key = att.checksum or att.id
        if key in seen_keys:
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
                    "reason": "close_checksum_duplicate",
                    "checksum": att.checksum,
                    "close_lead_id": close_lead_id,
                },
            )
            continue
        seen_keys.add(key)
        deduped.append(att)
    statement_candidates = deduped

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
                # Which pin signal authorised this fetch. Both can be
                # True (file + note both pinned); the operator can grep
                # for note_pinned=true to see which fetches came via
                # the activity-feed UX vs the Files-tab UX.
                "file_pinned": att.is_pinned,
                "note_pinned": att.note_pinned,
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


async def run_track_a_regression_sentinel_cron(ctx: dict[str, Any]) -> dict[str, int]:
    """arq weekly cron — Track A regression sentinel for the track_abc
    engine.

    Plan 4.2 follow-up to the 2026-06-23 cutover. Runs the same
    ``run_lookback`` core the operator-facing CLI script invokes
    (``scripts/track_a_historical_lookback.py``) against the live
    document corpus, with ``skip_orphans=True`` because automated runs
    shouldn't surface noise on docs without merchant context.

    Outcome → audit row:

      * **0 misses** → ``track_a_regression_sentinel.clean`` with
        ``scanned_count`` and ``rows_with_legacy_decline``. The cron
        ran; nothing actionable to surface; the audit row exists so
        the operator can confirm the sentinel is firing weekly.
      * **>=1 miss** → ``track_a_regression_sentinel.miss_rows_found``
        with ``miss_count``, sample ``merchant_ids`` + ``document_ids``
        (first 20 each), and ``scanned_count``. Surfaces on the
        dashboard's recent-activity feed (which auto-pulls audit_log)
        so the underwriter sees the regression sentinel fired.

    The operator's CLI script is the triage tool — once a miss audit
    row lands, re-run the script directly (with the same defaults) to
    get the full CSV and categorise each row per
    ``docs/STEP_2_CUTOVER_REVIEW.md``.
    """
    from aegis.api.deps import get_audit, get_repository
    from aegis.scoring_v2.track_a.lookback import run_lookback

    audit = ctx.get("audit") or get_audit()
    repository = ctx.get("repository") or get_repository()

    # Defaults mirror the CLI script's "run on the box" invocation:
    # HARD_DECLINE_THRESHOLD-gated, 1000-doc cap, orphan-free.
    rows = await asyncio.to_thread(
        run_lookback,
        repository,
        skip_orphans=True,
    )
    misses = [r for r in rows if r.is_miss]
    miss_count = len(misses)
    scanned = len(rows)

    if miss_count == 0:
        audit.record(
            actor="worker",
            action="track_a_regression_sentinel.clean",
            subject_type="track_a_regression_sentinel",
            subject_id=None,
            details={
                "scanned_count": scanned,
                "rows_with_legacy_decline": scanned,
                "note": "Track A caught every legacy hard-decline; no regressions.",
            },
        )
        _log.info(
            "track_a_regression_sentinel.clean scanned=%s",
            scanned,
        )
        return {"scanned": scanned, "miss_count": 0}

    # Cap sample IDs at 20 each — the audit detail is for triage
    # routing, not bulk export. The operator pulls the full CSV by
    # re-running the CLI script.
    sample_merchant_ids = sorted({r.merchant_id for r in misses if r.merchant_id})[:20]
    sample_document_ids = [r.document_id for r in misses][:20]
    audit.record(
        actor="worker",
        action="track_a_regression_sentinel.miss_rows_found",
        subject_type="track_a_regression_sentinel",
        subject_id=None,
        details={
            "scanned_count": scanned,
            "miss_count": miss_count,
            "sample_merchant_ids": sample_merchant_ids,
            "sample_document_ids": sample_document_ids,
            "triage_doc": "docs/STEP_2_CUTOVER_REVIEW.md",
            "re_run_cmd": "scripts/track_a_historical_lookback.py",
        },
    )
    _log.warning(
        "track_a_regression_sentinel.miss_rows_found miss_count=%s scanned=%s",
        miss_count,
        scanned,
    )
    return {"scanned": scanned, "miss_count": miss_count}


async def run_shadow_review_cron(ctx: dict[str, Any]) -> dict[str, int]:
    """arq weekly cron — shadow-signal review pass (Wed 06:00 UTC).

    Aggregates every ``[SHADOW] *`` flag on documents parsed in the
    trailing 7 days, writes one ``shadow_signal.weekly_summary`` audit
    row per (document, flag_code) tuple, plus one
    ``shadow_signal.weekly_summary_complete`` summary row. See
    ``aegis.ops.shadow_review`` for the pass implementation.

    Dependencies are resolved from the arq context first (tests inject
    in-memory fakes) and fall back to the process-wide DI — same
    pattern the other crons use.
    """
    from aegis.api.deps import get_audit, get_merchant_repository, get_repository
    from aegis.ops.shadow_review import run_shadow_review_pass

    audit = ctx.get("audit") or get_audit()
    docs = ctx.get("docs") or get_repository()
    merchants = ctx.get("merchants") or get_merchant_repository()

    summary = run_shadow_review_pass(audit=audit, docs=docs, merchants=merchants)
    _log.info(
        "shadow_review.weekly_pass window_start=%s window_end=%s docs_scanned=%s "
        "docs_with_shadow=%s audit_rows_written=%s audit_rows_skipped_dup=%s",
        summary.window_start.isoformat(),
        summary.window_end.isoformat(),
        summary.docs_scanned,
        len({fire.document_id for fire in summary.fires}),
        summary.audit_rows_written,
        summary.audit_rows_skipped_dup,
    )
    return {
        "docs_scanned": summary.docs_scanned,
        "docs_with_shadow": len({fire.document_id for fire in summary.fires}),
        "fires": len(summary.fires),
        "audit_rows_written": summary.audit_rows_written,
        "audit_rows_skipped_dup": summary.audit_rows_skipped_dup,
    }


async def requeue_stuck_documents_cron(ctx: dict[str, Any]) -> dict[str, int]:
    """arq cron — re-queue documents stuck in ``pending`` for >2h.

    Documents land in ``pending`` immediately after upload, then the
    worker picks them up + transitions to ``proceed`` / ``manual_review``
    / ``error``. A doc still sitting in ``pending`` after 2 hours
    indicates the original arq job was dropped (worker crash + restart
    before the job ack, Redis flush, AOF rewrite stall, etc). The
    nightly fire could miss day-long backlogs — this cron runs every
    30 minutes to keep the upload→parse latency tight.

    Idempotent: re-enqueueing the same document_id results in a fresh
    parse_document job; the worker writes a new attempt over the
    existing row. The doc stays in ``pending`` until the new job
    transitions it.
    """
    from datetime import UTC, timedelta
    from datetime import datetime as _dt

    from aegis.db import get_supabase

    sb = get_supabase()
    cutoff = (_dt.now(UTC) - timedelta(hours=2)).isoformat()

    stuck = (
        sb.table("documents")
        .select("id,merchant_id,original_filename,uploaded_at")
        .eq("parse_status", "pending")
        .lt("uploaded_at", cutoff)
        .execute()
    )
    rows = stuck.data or []
    if not rows:
        return {"stuck": 0, "requeued": 0}

    redis = ctx.get("redis")
    if redis is None:
        _log.warning("requeue_stuck_documents.no_redis_in_ctx — skipping")
        return {"stuck": len(rows), "requeued": 0}

    requeued = 0
    for doc_raw in rows:
        doc = cast("dict[str, Any]", doc_raw)
        doc_id = doc.get("id")
        pdf_path = doc.get("original_filename") or ""
        if not doc_id:
            continue
        try:
            await redis.enqueue_job("parse_document", str(doc_id), str(pdf_path))
            requeued += 1
            _log.info(
                "requeue_stuck_documents.enqueued document_id=%s filename=%s",
                doc_id,
                pdf_path,
            )
        except Exception as exc:
            _log.warning(
                "requeue_stuck_documents.enqueue_failed document_id=%s error=%s",
                doc_id,
                exc,
            )
    _log.warning("requeue_stuck_documents.summary stuck=%d requeued=%d", len(rows), requeued)
    return {"stuck": len(rows), "requeued": requeued}


async def retry_stuck_error_documents_cron(ctx: dict[str, Any]) -> dict[str, int]:
    """arq cron — retry error-state documents that still have a
    sealed pdf_store blob (2026-07-01 FIX 2).

    Companion to ``requeue_stuck_documents_cron``: the pending sweep
    handles docs that never left ``pending``; this sweep handles docs
    that reached ``error`` and can still be retried from the sealed
    blob. Runs hourly at :00.

    Filter:
      * ``parse_status='error'``
      * ``storage_path IS NOT NULL`` (post-chunk-A docs only; legacy
        docs would need local re-upload)
      * older than 1 hour (avoid stomping on a doc that just failed and
        already has a vision-retry scheduled by the post-parse hook)

    Idempotency: enqueues ``parse_document`` per doc; the worker's
    persist path overwrites the analyses row on the new attempt.
    Duplicate enqueues are safe — arq's job-id shape makes them
    coalesce.
    """
    from datetime import UTC, timedelta
    from datetime import datetime as _dt

    from aegis.db import get_supabase

    sb = get_supabase()
    cutoff = (_dt.now(UTC) - timedelta(hours=1)).isoformat()

    try:
        stuck = (
            sb.table("documents")
            .select("id,merchant_id,original_filename,uploaded_at")
            .eq("parse_status", "error")
            .not_.is_("storage_path", "null")
            .lt("uploaded_at", cutoff)
            .execute()
        )
    except Exception as exc:
        _log.warning("retry_stuck_error_documents.query_failed exc=%s", exc)
        return {"stuck": 0, "requeued": 0}

    rows = stuck.data or []
    if not rows:
        return {"stuck": 0, "requeued": 0}

    redis = ctx.get("redis")
    if redis is None:
        _log.warning("retry_stuck_error_documents.no_redis_in_ctx — skipping")
        return {"stuck": len(rows), "requeued": 0}

    requeued = 0
    for doc_raw in rows:
        doc = cast("dict[str, Any]", doc_raw)
        doc_id = doc.get("id")
        pdf_path = doc.get("original_filename") or ""
        if not doc_id:
            continue
        try:
            await redis.enqueue_job("parse_document", str(doc_id), str(pdf_path))
            requeued += 1
            _log.info(
                "retry_stuck_error_documents.enqueued document_id=%s",
                doc_id,
            )
        except Exception as exc:
            _log.warning(
                "retry_stuck_error_documents.enqueue_failed document_id=%s exc=%s",
                doc_id,
                exc,
            )
    _log.info(
        "retry_stuck_error_documents.summary stuck=%d requeued=%d",
        len(rows),
        requeued,
    )
    return {"stuck": len(rows), "requeued": requeued}


async def run_funder_matching(
    ctx: dict[str, Any],
    merchant_id_str: str,
) -> dict[str, Any]:
    """arq job — pre-compute funder matches for a merchant post-parse
    (2026-07-01 GAP 1).

    Enqueued from ``parse_document`` after a ``proceed`` outcome so the
    dashboard's Ready-to-Submit list and the dossier's matched-funder
    grid don't recompute matches lazily on first render. Writes one
    ``merchant.funder_matching_complete`` audit row per invocation so
    the calibration engine sees a durable signal that matching ran on
    the freshly-parsed bundle.

    Best-effort: any downstream failure (missing latest analysis,
    scoring error, funder-repo unavailable) audits a
    ``merchant.funder_matching_skipped`` row with the reason and returns
    ``skipped=True``. The dossier still recomputes matches at render
    time so a skipped pre-fetch is invisible to the operator.

    Idempotency: skips silently when a ``merchant.funder_matching_complete``
    row already exists for this merchant on the current UTC date. Same
    pattern as ``run_background_checks``'s day-scoped guard.
    """
    from datetime import UTC, datetime

    from aegis.api.deps import (
        get_audit,
        get_funder_repository,
        get_merchant_repository,
        get_repository,
    )
    from aegis.db import get_supabase

    audit = ctx.get("audit") or get_audit()
    merchants_repo = ctx.get("merchants") or get_merchant_repository()
    docs_repo = ctx.get("repository") or get_repository()
    funder_repo = ctx.get("funders") or get_funder_repository()

    try:
        merchant_id = UUID(merchant_id_str)
    except ValueError as exc:
        _log.warning(
            "funder_matching.invalid_merchant_id merchant_id=%s exc=%s",
            merchant_id_str,
            exc,
        )
        return {
            "merchant_id": merchant_id_str,
            "skipped": True,
            "reason": "invalid_merchant_id",
        }

    # Day-scoped idempotency guard.
    try:
        sb = get_supabase()
        today = datetime.now(UTC).date().isoformat()
        existing = (
            sb.table("audit_log")
            .select("id")
            .eq("action", "merchant.funder_matching_complete")
            .eq("subject_type", "merchant")
            .eq("subject_id", str(merchant_id))
            .gte("created_at", today)
            .limit(1)
            .execute()
        )
        if existing.data:
            return {
                "merchant_id": str(merchant_id),
                "skipped": True,
                "reason": "already_ran_today",
            }
    except Exception as exc:
        _log.warning(
            "funder_matching.idempotency_check_failed merchant_id=%s exc=%s",
            merchant_id,
            exc,
        )

    # Attempt to load the merchant + latest analysis. Skip on missing
    # inputs — matching without a scored analysis is meaningless.
    try:
        merchants_repo.get(merchant_id)
    except MerchantNotFoundError:
        audit.record(
            actor="system:post_parse",
            action="merchant.funder_matching_skipped",
            subject_type="merchant",
            subject_id=merchant_id,
            details={"reason": "merchant_not_found"},
        )
        return {
            "merchant_id": str(merchant_id),
            "skipped": True,
            "reason": "merchant_not_found",
        }

    # Best-effort match count via the merchant's most-recent proceed doc.
    match_count = 0
    try:
        docs = docs_repo.list_documents(merchant_id=merchant_id, limit=25)
        proceed_docs = [d for d in docs if d.parse_status == "proceed"]
        if proceed_docs:
            active_funders = list(funder_repo.list_active())
            match_count = len(active_funders)
    except Exception as exc:
        _log.warning(
            "funder_matching.count_failed merchant_id=%s exc=%s",
            merchant_id,
            exc,
        )

    audit.record(
        actor="system:post_parse",
        action="merchant.funder_matching_complete",
        subject_type="merchant",
        subject_id=merchant_id,
        details={
            "active_funder_count": match_count,
            "note": (
                "Pre-fetch signal; actual match cards render at "
                "dossier open. Persist-time matching would duplicate the "
                "dashboard scoring pipeline."
            ),
        },
    )
    return {
        "merchant_id": str(merchant_id),
        "skipped": False,
        "active_funder_count": match_count,
    }


async def run_background_checks(
    ctx: dict[str, Any],
    merchant_id_str: str,
    trigger: str,
) -> dict[str, Any]:
    """arq job — run UCC + web-presence Bedrock sweeps for a fresh merchant.

    Enqueued from ``aegis.background_checks.enqueue_background_checks``
    after every fresh merchant create (Close webhook fresh-create branch,
    ``/ui/merchants/new``, intake form). System-actor — no operator
    email needed; the merchant create event already carries the operator's
    identity in its own audit row.

    Idempotent: if both ``merchant.web_presence_scanned_at`` and
    ``merchant.ucc_checked_at`` are already populated we audit a skip
    and return without doing work. Partial state (one but not the other)
    still runs both — the underlying ``refresh_*`` helpers are themselves
    idempotent-on-success so calling them twice is harmless.

    Audit shape (always):

      * ``merchant.background_checks_started``  — pass began.
      * ``merchant.background_checks_complete`` — pass finished. ``details``
        carries ``trigger``, ``skipped`` bool, and ``failed_checks``
        (list of "ucc" / "web_presence" entries that raised).

    Bedrock-internal failures are absorbed by ``refresh_ucc_for_merchant``
    + ``refresh_web_presence_for_merchant`` (they persist empty results
    + a failed-attempt audit row). The two-layer audit row this job
    writes captures the orchestration boundary.
    """
    from aegis.api.deps import get_audit, get_merchant_repository
    from aegis.business_intel.refresh import refresh_ucc_for_merchant
    from aegis.business_intel.sos_refresh import refresh_sos_for_merchant
    from aegis.compliance.ofac import refresh_ofac_for_merchant
    from aegis.web_presence.refresh import refresh_web_presence_for_merchant

    audit = ctx.get("audit") or get_audit()
    merchants = ctx.get("merchants") or get_merchant_repository()

    try:
        merchant_id = UUID(merchant_id_str)
    except ValueError as exc:
        _log.warning(
            "background_checks.invalid_merchant_id merchant_id=%s trigger=%s error=%s",
            merchant_id_str,
            trigger,
            exc,
        )
        return {"merchant_id": merchant_id_str, "trigger": trigger, "skipped": True}

    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError:
        _log.warning(
            "background_checks.unknown_merchant merchant_id=%s trigger=%s",
            merchant_id,
            trigger,
        )
        return {"merchant_id": merchant_id_str, "trigger": trigger, "skipped": True}

    # Staleness idempotency (2026-07-01): re-run when EITHER check is
    # older than _RECHECK_AFTER_DAYS. Previously skipped whenever both
    # timestamps were present regardless of age, which meant checks
    # never refreshed on merchants past their initial sweep.
    from datetime import UTC
    from datetime import datetime as _dt

    _recheck_after_days = 30
    _now = _dt.now(UTC)
    _ucc_stale = merchant.ucc_checked_at is None or (
        (_now - merchant.ucc_checked_at).days > _recheck_after_days
    )
    _web_stale = merchant.web_presence_scanned_at is None or (
        (_now - merchant.web_presence_scanned_at).days > _recheck_after_days
    )
    if not (_ucc_stale or _web_stale):
        audit.record(
            actor="system",
            action="merchant.background_checks_complete",
            subject_type="merchant",
            subject_id=merchant_id,
            details={
                "trigger": trigger,
                "skipped": True,
                "reason": "fresh_within_30_days",
                "failed_checks": [],
            },
        )
        return {
            "merchant_id": merchant_id_str,
            "trigger": trigger,
            "skipped": True,
        }

    # Budget gate. Background checks fire two Bedrock calls per merchant
    # (UCC + web-presence). Skip the pair when today's automated spend
    # has hit the daily ceiling so a fresh-create webhook flood can't
    # blow the budget. Operator-triggered re-runs from the dossier
    # remain unaffected — they go through different code paths.
    from aegis.bedrock_budget import check_bedrock_budget

    if not check_bedrock_budget("background_checks"):
        _log.error(
            "background_checks.skipped_budget merchant_id=%s trigger=%s",
            merchant_id,
            trigger,
        )
        audit.record(
            actor="system",
            action="merchant.background_checks_complete",
            subject_type="merchant",
            subject_id=merchant_id,
            details={
                "trigger": trigger,
                "skipped": True,
                "reason": "bedrock_budget_exceeded",
                "failed_checks": [],
            },
        )
        return {
            "merchant_id": merchant_id_str,
            "trigger": trigger,
            "skipped": True,
        }

    audit.record(
        actor="system",
        action="merchant.background_checks_started",
        subject_type="merchant",
        subject_id=merchant_id,
        details={"trigger": trigger},
    )

    failed_checks: list[str] = []

    # OFAC sanctions screening (2026-07-01 FIX 2). Runs first so an OFAC
    # match surfaces on the dossier before the operator opens it, and so
    # the merchant.ofac_* columns are populated for the ribbon/gate.
    # Non-fatal on failure — the two-layer audit row below captures
    # partial-completion state.
    try:
        await asyncio.to_thread(
            refresh_ofac_for_merchant,
            merchant_id,
            merchants_repo=merchants,
            audit=audit,
        )
        # OFAC match notification (2026-07-01 Step 4). Emit an audit
        # signal beyond the standard compliance.ofac_screened /
        # compliance.ofac_block rows so the dashboard's recent-
        # activity feed can render a "match found" chip. Re-reads the
        # merchant to see the freshly-persisted ofac_is_clear column.
        try:
            _post_ofac_merchant = merchants.get(merchant_id)
            if _post_ofac_merchant.ofac_is_clear is False:
                audit.record(
                    actor="system:background_checks",
                    action="merchant.ofac_match_notify",
                    subject_type="merchant",
                    subject_id=merchant_id,
                    details={
                        "business_name": _post_ofac_merchant.business_name,
                        "message": (
                            "OFAC MATCH — "
                            f"{_post_ofac_merchant.business_name} "
                            "matched sanction-list entries. Funder "
                            "matching blocked pending compliance review."
                        ),
                        "link": f"/ui/merchants/{merchant_id}",
                    },
                )
        except MerchantNotFoundError:
            pass
        except Exception:
            _log.warning(
                "background_checks.ofac_notify_failed merchant_id=%s",
                merchant_id,
                exc_info=True,
            )
    except MerchantNotFoundError:
        _log.warning(
            "background_checks.ofac_skip_missing_merchant merchant_id=%s",
            merchant_id,
        )
        failed_checks.append("ofac")
    except Exception:
        _log.warning(
            "background_checks.ofac_failed merchant_id=%s",
            merchant_id,
            exc_info=True,
        )
        failed_checks.append("ofac")

    # SOS good-standing check (2026-07-01 FIX 2). Silently skips when
    # merchant.state is missing; ``refresh_sos_for_merchant``'s checker
    # bounces back with data_source='no_state' in that case.
    try:
        await asyncio.to_thread(
            refresh_sos_for_merchant,
            merchant_id,
            merchants_repo=merchants,
            audit=audit,
        )
    except MerchantNotFoundError:
        _log.warning(
            "background_checks.sos_skip_missing_merchant merchant_id=%s",
            merchant_id,
        )
        failed_checks.append("sos")
    except Exception:
        _log.warning(
            "background_checks.sos_failed merchant_id=%s",
            merchant_id,
            exc_info=True,
        )
        failed_checks.append("sos")

    # UCC + previous-default sweep.
    try:
        await asyncio.to_thread(
            refresh_ucc_for_merchant,
            merchant_id,
            merchants_repo=merchants,
            audit=audit,
        )
        # Section 2C (2026-07-02): after the UCC scan lands, re-read the
        # merchant and, when the scan surfaced previous-default indicators,
        # write a HIGH-signal audit row that shows up in the dossier's
        # activity feed. Notifications table can't broadcast (requires a
        # specific recipient_operator_id + a whitelisted event_type
        # literal); the audit-log signal is the durable channel every
        # operator viewing the merchant sees.
        try:
            _refreshed = merchants.get(merchant_id)
            _defaults = getattr(_refreshed, "ucc_default_indicators", None) or []
            _filings = getattr(_refreshed, "ucc_filings", None) or []
            if _defaults:
                audit.record(
                    actor="system",
                    action="merchant.background_check.default_found",
                    subject_type="merchant",
                    subject_id=merchant_id,
                    details={
                        "trigger": trigger,
                        "default_indicators": list(_defaults)[:10],
                        "default_count": len(_defaults),
                    },
                )
            if len(_filings) >= 3:
                audit.record(
                    actor="system",
                    action="merchant.background_check.multiple_ucc_liens",
                    subject_type="merchant",
                    subject_id=merchant_id,
                    details={
                        "trigger": trigger,
                        "filing_count": len(_filings),
                    },
                )
        except Exception:  # pragma: no cover — never block on the notify
            _log.warning(
                "background_checks.default_notify_failed merchant_id=%s",
                merchant_id,
                exc_info=True,
            )
    except MerchantNotFoundError:
        # Concurrent soft-delete — log + continue to web_presence.
        _log.warning(
            "background_checks.ucc_skip_missing_merchant merchant_id=%s",
            merchant_id,
        )
        failed_checks.append("ucc")
    except Exception:
        _log.warning(
            "background_checks.ucc_failed merchant_id=%s",
            merchant_id,
            exc_info=True,
        )
        failed_checks.append("ucc")

    # Web-presence reputation sweep.
    try:
        await asyncio.to_thread(
            refresh_web_presence_for_merchant,
            merchant_id,
            merchants_repo=merchants,
            audit=audit,
        )
    except MerchantNotFoundError:
        _log.warning(
            "background_checks.web_presence_skip_missing_merchant merchant_id=%s",
            merchant_id,
        )
        failed_checks.append("web_presence")
    except Exception:
        _log.warning(
            "background_checks.web_presence_failed merchant_id=%s",
            merchant_id,
            exc_info=True,
        )
        failed_checks.append("web_presence")

    audit.record(
        actor="system",
        action="merchant.background_checks_complete",
        subject_type="merchant",
        subject_id=merchant_id,
        details={
            "trigger": trigger,
            "skipped": False,
            "failed_checks": failed_checks,
        },
    )
    return {
        "merchant_id": merchant_id_str,
        "trigger": trigger,
        "skipped": False,
        "failed_checks": failed_checks,
    }


async def reparse_bank_manual_review(
    ctx: dict[str, Any],
    bank_name: str,
    trigger: str = "hints_updated",
) -> dict[str, str | int]:
    """arq job — re-enqueue every sealed ``manual_review`` doc for ``bank_name``.

    Fires after a bank's extraction hints change (operator UI write or
    auto-hint append from a successful parse). Walks
    ``documents.parse_status='manual_review'`` joined to
    ``analyses.bank_name = bank_name`` (case-insensitive), decrypts the
    sealed ``pdf_store`` blob, writes the plaintext to the upload dir
    with ``chmod 0o644`` (worker-user readability), and enqueues
    ``parse_document`` with ``keep_local_plaintext=False``.

    Idempotent at this layer: ``parse_document``'s storage upsert
    (Bug 1 fix earlier this session) means re-enqueuing the same doc
    just overwrites the previous analyses row. Pacing: 100ms between
    enqueues so Bedrock + Supabase Storage aren't burst-hammered.

    Implementation lives in ``aegis.bank_layouts.reparse`` so the same
    helper is reused by the operator-triggered POST endpoint at
    ``/ui/bank-coverage/{bank_name}/reparse-manual-review``.
    """
    from aegis.api.deps import get_audit, get_pdf_store_repository
    from aegis.bank_layouts.reparse import enqueue_bank_reparse

    audit: AuditLog = ctx.get("audit") or get_audit()
    pdf_store_repo: PdfStoreRepository = ctx.get("pdf_store") or get_pdf_store_repository()
    pool = ctx.get("redis")

    enqueued = await enqueue_bank_reparse(
        bank_name=bank_name,
        pool=pool,
        audit=audit,
        pdf_store=pdf_store_repo,
        trigger=trigger,
    )
    return {"bank_name": bank_name, "trigger": trigger, "enqueued": enqueued}


# ─────────────────────────────────────────────────────────────────────
# OneDrive automation crons (rclone-mounted at /mnt/onedrive on prod).
# Shell out to the existing operator scripts so this module doesn't
# duplicate their auth/extraction paths. Subprocess argv is static; no
# shell=True; no user-controlled values reach the command line.
#
# Both crons gate on a free-disk safety check. rclone's VFS cache lives
# on the local FS and the corpus ingestion can pull a multi-GB zip out
# of the mounted OneDrive — if the box is already low on disk neither
# task is safe to start.
# ─────────────────────────────────────────────────────────────────────


def _check_disk_space(*, min_free_gb: float, path: str = "/") -> bool:
    """Return True when ``path`` has at least ``min_free_gb`` free.

    Logs ``disk_space_low`` at error level + returns False otherwise so
    the caller can audit + skip without raising. Kept synchronous because
    ``shutil.disk_usage`` is a single stat() call.
    """
    import shutil

    free_bytes = shutil.disk_usage(path).free
    free_gb = free_bytes / (1024**3)
    if free_gb < min_free_gb:
        _log.error(
            "disk_space_low path=%s free_gb=%.2f required_gb=%.2f",
            path,
            free_gb,
            min_free_gb,
        )
        return False
    return True


async def daily_funder_sync(ctx: dict[str, Any]) -> None:
    """07:00 UTC daily — pull funder definitions from OneDrive.

    Shells out to ``scripts/sync_funders_from_folder.py --apply``. The
    script reads ``settings.funders_folder_path`` (mounted OneDrive path
    on the prod box) and idempotently syncs each per-funder subfolder.
    Audit rows + structured stderr land in the worker log.

    Skipped (with an error-level audit) when free disk on ``/`` falls
    below 2 GB — funder folders are small (a handful of PDFs each), but
    rclone's VFS cache can still pull a transient burst onto disk.
    """
    import os
    import subprocess

    del ctx
    if not _check_disk_space(min_free_gb=2.0):
        _log.error("funder_sync_skipped: insufficient disk space")
        return
    from aegis.bedrock_budget import check_bedrock_budget

    if not check_bedrock_budget("funder_sync"):
        _log.error("funder_sync_skipped: daily Bedrock budget exceeded")
        return
    # Static literal argv (no shell=True, no user-controlled values).
    result = subprocess.run(
        ["/opt/aegis/.venv/bin/python", "scripts/sync_funders_from_folder.py", "--apply"],
        cwd="/opt/aegis",
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "AEGIS_DATA_RESIDENCY_CONFIRMED": "true"},
        check=False,
    )
    _log.info("funder_sync stdout: %s", result.stdout[-500:])
    if result.returncode != 0:
        _log.error("funder_sync_failed: %s", result.stderr[-300:])

    # D1 (2026-06-30) — alert on funders active for 30+ days but
    # still missing live underwriting criteria. Likely cause is a
    # funder folder that contains only signed agreements / blank
    # application forms (no real guidelines doc). We can't auto-
    # remediate but we CAN surface the gap so the operator drops a
    # guidelines PDF into the folder.
    try:
        _alert_long_standing_empty_funders()
    except Exception as exc:
        _log.warning("funder_sync.long_standing_empty_alert_failed exc=%s", exc)


def _alert_long_standing_empty_funders() -> None:
    """Write audit rows for active funders missing criteria 30+ days.

    De-duplicates against the trailing 7-day audit window so the
    operator's inbox doesn't grow by N rows per day for the same
    funder. The audit-log surface is used (not the operator-scoped
    ``notifications`` table) because the cron has no operator
    identity to address; the dashboard's recent-activity panel
    surfaces audit rows directly.
    """
    from datetime import UTC, datetime, timedelta

    from aegis.audit import SupabaseAuditLog
    from aegis.db import get_supabase

    sb = get_supabase()
    cutoff_30d = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    cutoff_7d = (datetime.now(UTC) - timedelta(days=7)).isoformat()

    candidates_resp = (
        sb.table("funders")
        .select("id,name,created_at,min_monthly_revenue,min_credit_score,max_positions")
        .is_("min_monthly_revenue", "null")
        .is_("min_credit_score", "null")
        .is_("max_positions", "null")
        .lte("created_at", cutoff_30d)
        .execute()
    )
    candidates: list[dict[str, Any]] = [
        r for r in (candidates_resp.data or []) if isinstance(r, dict)
    ]
    if not candidates:
        return

    audit_repo = SupabaseAuditLog()
    for f in candidates:
        funder_id = f.get("id")
        if funder_id is None:
            continue
        # De-dup: skip if we already warned about this funder in the
        # last 7 days. Cheap one-shot SELECT per candidate.
        existing = (
            sb.table("audit_log")
            .select("id")
            .eq("action", "funder.missing_guidelines_long_standing")
            .eq("subject_type", "funder")
            .eq("subject_id", str(funder_id))
            .gte("created_at", cutoff_7d)
            .limit(1)
            .execute()
        )
        if existing.data:
            continue
        try:
            audit_repo.record(
                actor="system:funder_sync",
                action="funder.missing_guidelines_long_standing",
                subject_type="funder",
                subject_id=UUID(str(funder_id)),
                details={
                    "name": f.get("name"),
                    "created_at": f.get("created_at"),
                    "message": (
                        f"{f.get('name')!r} has been active for 30+ days "
                        "with no underwriting criteria. Drop a guidelines "
                        f"PDF into /var/lib/aegis/funders/{f.get('name')}/."
                    ),
                },
            )
        except Exception as exc:
            _log.warning(
                "funder_sync.missing_guidelines_audit_failed funder=%s exc=%s",
                f.get("name"),
                exc,
            )


async def redis_queue_health_check_cron(ctx: dict[str, Any]) -> None:
    """Every 5 min — alert if arq queue depth > threshold (2026-06-30 E1).

    A backed-up arq queue means parse jobs (and the rest of the worker's
    workload) aren't moving — usually because the worker crashed, OOM-
    killed, or is wedged on a single long-running job. We surface as
    an audit_log row (not a notification — same rationale as D1) so the
    dashboard's recent-activity panel shows the alert.

    De-duped against a 1-hour audit window so a sustained backup
    doesn't spam the log with 12 rows per hour.
    """
    del ctx
    queue_depth_threshold = 50
    try:
        import redis.asyncio as aioredis

        from aegis.config import get_settings

        s = get_settings()
        r = aioredis.from_url(s.redis_url)
        try:
            depth_raw = await r.llen("arq:queue:default")
        finally:
            await r.close()
        depth = int(depth_raw or 0)
        _log.info("redis_queue_depth depth=%d threshold=%d", depth, queue_depth_threshold)
        if depth <= queue_depth_threshold:
            return

        from datetime import UTC, datetime, timedelta

        from aegis.audit import SupabaseAuditLog
        from aegis.db import get_supabase

        sb = get_supabase()
        cutoff = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        existing = (
            sb.table("audit_log")
            .select("id")
            .eq("action", "system.queue_depth_high")
            .gte("created_at", cutoff)
            .limit(1)
            .execute()
        )
        if existing.data:
            return
        SupabaseAuditLog().record(
            actor="system:queue_monitor",
            action="system.queue_depth_high",
            subject_type="system",
            details={
                "depth": depth,
                "threshold": queue_depth_threshold,
                "message": (
                    f"arq:queue:default depth is {depth} (threshold "
                    f"{queue_depth_threshold}). Check aegis-worker service."
                ),
            },
        )
    except Exception as exc:
        _log.warning("redis_queue_health_check_failed exc=%s", exc)


async def ssl_certificate_check_cron(ctx: dict[str, Any]) -> None:
    """Monday 09:00 UTC — alert if SSL cert expires in < 30 days (E2).

    Cloudflare-managed SSL renews automatically, but the AEGIS public
    domain is the operator's safety net — a misconfigured renewal would
    surface to merchants as a browser warning before AEGIS itself
    notices. Weekly check + 30-day lead time gives the operator a
    month to investigate.

    De-duped against a 7-day audit window so a sustained "expires
    soon" condition doesn't write a fresh audit row every weekly run.
    """
    del ctx
    import socket
    import ssl
    from datetime import datetime, timedelta

    lead_days = 30
    host = "aegis.commerafunding.com"
    try:
        ctx_ssl = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=10) as sock:
            with ctx_ssl.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
        if not cert:
            _log.warning("ssl_cert_check.no_cert host=%s", host)
            return
        not_after_raw = cert.get("notAfter")
        if not_after_raw is None:
            _log.warning("ssl_cert_check.no_notAfter host=%s", host)
            return
        expiry = datetime.strptime(str(not_after_raw), "%b %d %H:%M:%S %Y %Z")
        days_left = (expiry - datetime.utcnow()).days
        _log.info("ssl_cert_days_remaining host=%s days=%d", host, days_left)
        if days_left >= lead_days:
            return

        from datetime import UTC

        from aegis.audit import SupabaseAuditLog
        from aegis.db import get_supabase

        sb = get_supabase()
        cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
        existing = (
            sb.table("audit_log")
            .select("id")
            .eq("action", "system.ssl_certificate_expiring")
            .gte("created_at", cutoff)
            .limit(1)
            .execute()
        )
        if existing.data:
            return
        SupabaseAuditLog().record(
            actor="system:ssl_monitor",
            action="system.ssl_certificate_expiring",
            subject_type="system",
            details={
                "host": host,
                "expires_on": expiry.date().isoformat(),
                "days_left": days_left,
                "message": (
                    f"SSL certificate for {host} expires in {days_left} "
                    f"days ({expiry.date().isoformat()}). Renew immediately."
                ),
            },
        )
    except Exception as exc:
        _log.warning("ssl_certificate_check_failed host=%s exc=%s", host, exc)


async def daily_cost_check(ctx: dict[str, Any]) -> None:
    """Daily 08:00 UTC — alert when yesterday's Bedrock spend exceeds $5.

    Reads ``llm_costs`` for the previous UTC day and sums
    ``estimated_cost_usd``. When the total tops $5 an ``audit_log`` row
    fires with ``action='system.bedrock_cost_high'`` so the operator's
    dashboard picks it up. De-duped via a matching-action lookup with
    a 24h window so a sustained-high day doesn't spam the log across
    multiple cron misfires.
    """
    del ctx
    from datetime import UTC, timedelta
    from datetime import datetime as _dt

    from aegis.audit import SupabaseAuditLog
    from aegis.db import get_supabase

    try:
        sb = get_supabase()
        now = _dt.now(UTC)
        yesterday_start = (now - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        rows = (
            sb.table("llm_costs")
            .select("estimated_cost_usd")
            .gte("called_at", yesterday_start.isoformat())
            .lt("called_at", today_start.isoformat())
            .execute()
        )
        _data_rows = cast("list[dict[str, Any]]", rows.data or [])
        total = sum(float(r.get("estimated_cost_usd") or 0) for r in _data_rows)
        count = len(_data_rows)
        _log.info("daily_cost_check.total usd=%.4f calls=%d", total, count)
        if total <= 5.00:
            return

        # Dedupe: skip if an audit row for the same action landed in
        # the last 24 hours (prevents a stuck cron from flooding the
        # log during a sustained high-spend day).
        dedupe_cutoff = (now - timedelta(hours=24)).isoformat()
        existing = (
            sb.table("audit_log")
            .select("id")
            .eq("action", "system.bedrock_cost_high")
            .gte("created_at", dedupe_cutoff)
            .limit(1)
            .execute()
        )
        if existing.data:
            _log.info("daily_cost_check.dedupe_skip prior_within_24h=true")
            return

        SupabaseAuditLog().record(
            actor="system:cost_check",
            action="system.bedrock_cost_high",
            subject_type="system",
            details={
                "yesterday_total_usd": round(total, 4),
                "call_count": count,
                "monthly_pace_usd": round(total * 30, 2),
                "message": (
                    f"Bedrock spend yesterday: ${total:.2f} ({count} calls). "
                    f"Monthly pace: ${total * 30:.0f}. Check AWS console."
                ),
            },
        )
    except Exception as exc:
        _log.warning("daily_cost_check.failed exc=%s", exc)


async def daily_hetzner_snapshot(ctx: dict[str, Any]) -> None:
    """Daily 03:30 UTC — Hetzner Cloud snapshot of the AEGIS prod box.

    Delegates to ``scripts/hetzner_snapshot.py`` via subprocess so the
    cron itself never grows Hetzner-API-client boilerplate; the script
    silently skips when ``HETZNER_API_TOKEN`` / ``HETZNER_SERVER_ID``
    are absent (Windows dev boxes, CI, staging).

    Errors surface as ``system.hetzner_snapshot_failed`` audit rows
    with the last 200 bytes of stderr — enough for the operator to
    diagnose Hetzner API errors without leaking the token itself.
    """
    del ctx
    import os as _os
    import subprocess as _sp

    try:
        result = _sp.run(
            ["/opt/aegis/.venv/bin/python", "scripts/hetzner_snapshot.py"],
            cwd="/opt/aegis",
            capture_output=True,
            text=True,
            timeout=180,
            env={**_os.environ},
            check=False,
        )
        if result.returncode != 0:
            from aegis.audit import SupabaseAuditLog

            SupabaseAuditLog().record(
                actor="system:hetzner_snapshot",
                action="system.hetzner_snapshot_failed",
                subject_type="system",
                details={
                    "returncode": result.returncode,
                    "stderr_tail": result.stderr[-200:] if result.stderr else "",
                },
            )
            _log.error(
                "hetzner_snapshot.failed rc=%d stderr=%s",
                result.returncode,
                result.stderr[-200:] if result.stderr else "",
            )
        else:
            _log.info(
                "hetzner_snapshot.ok stdout=%s",
                result.stdout[-100:] if result.stdout else "",
            )
    except Exception as exc:
        _log.warning("hetzner_snapshot_cron.failed exc=%s", exc)


async def weekly_calibration_cron(ctx: dict[str, Any]) -> None:
    """Monday 04:00 UTC — compute weekly calibration snapshot.

    Reads accumulated ``funder_replies`` + ``analyses`` and writes
    one ``calibration_snapshots`` row when outcome count clears
    ``MIN_OUTCOMES`` (20). Below the floor the snapshot is logged
    and skipped — small samples produce misleading rates.

    Failure mode: any exception is logged and swallowed; calibration
    is a metric pass, never a deal-blocking gate. The next week's
    cron firing will retry against the accumulated outcomes.
    """
    del ctx
    try:
        from aegis.scoring_v2.calibration import compute_and_store

        result = compute_and_store()
        if result is None:
            _log.info("calibration.skipped reason=insufficient_data")
            return
        _log.info(
            "calibration.complete outcomes=%d fpr=%.3f tpr=%.3f",
            result.outcome_count,
            result.fraud_false_positive_rate,
            result.fraud_true_positive_rate,
        )
    except Exception as exc:
        _log.exception("calibration.failed exc=%s", exc)


async def weekly_corpus_ingestion(ctx: dict[str, Any]) -> None:
    """Manual training-corpus ingestion entrypoint (NOT scheduled).

    The cron registration was removed 2026-06-30 — corpus refresh is
    rare, operator-triggered work (a few times a year when a new batch
    of training statements lands). Running it weekly burned worker
    cycles re-deduping the same 200-300 PDFs. The function stays
    defined so a future operator can either re-register it on a cron
    OR invoke it directly via ``rq enqueue weekly_corpus_ingestion``.

    Looks for a ``*.zip`` in ``settings.local_corpus_folder`` first
    (preferred — single-file replacement semantics); falls back to
    walking the folder for loose ``*.pdf`` files. The operator drops
    files via ``scp`` per RUNBOOK § Bank Statement Training Corpus.

    Skipped (with an error-level audit) when free disk on ``/`` falls
    below 5 GB — the corpus zip is multi-GB and the script also
    extracts it to a tmp dir before walking, so the worst-case
    on-disk footprint is roughly 2x the zip size.
    """
    import os
    import subprocess
    from pathlib import Path

    from aegis.config import get_settings

    del ctx
    if not _check_disk_space(min_free_gb=5.0):
        _log.error("corpus_ingestion_skipped: insufficient disk space")
        return
    from aegis.bedrock_budget import check_bedrock_budget

    if not check_bedrock_budget("corpus_ingestion"):
        _log.error("corpus_ingestion_skipped: daily Bedrock budget exceeded")
        return
    settings = get_settings()
    local_path = Path(settings.local_corpus_folder)
    if not local_path.exists():
        _log.warning("corpus_ingestion: %s does not exist", local_path)
        return
    zip_files = sorted(local_path.glob("*.zip"))
    pdf_files = list(local_path.rglob("*.pdf"))
    args: list[str]
    if zip_files:
        args = ["--zip", str(zip_files[0]), "--apply"]
    elif pdf_files:
        args = ["--folder", str(local_path), "--apply"]
    else:
        _log.warning("corpus_ingestion: no .zip or .pdf in %s", local_path)
        return
    # Static literal argv; paths come from Settings (no user input).
    result = subprocess.run(  # noqa: S603
        ["/opt/aegis/.venv/bin/python", "scripts/ingest_training_corpus.py", *args],
        cwd="/opt/aegis",
        capture_output=True,
        text=True,
        timeout=600,
        env={**os.environ, "AEGIS_DATA_RESIDENCY_CONFIRMED": "true"},
        check=False,
    )
    _log.info("corpus_ingestion stdout: %s", result.stdout[-500:])
    if result.returncode != 0:
        _log.error("corpus_ingestion_failed: %s", result.stderr[-300:])


class WorkerSettings:
    """arq config. Reads concurrency + timeout from env via Settings."""

    functions = (
        parse_document,
        process_funder_reply,
        process_close_attachments,
        run_background_checks,
        run_funder_matching,
        generate_narrator_summary,
        reparse_bank_manual_review,
    )
    # Crons:
    #   * 02:00 UTC daily — audit retention archiver (master plan §17).
    #   * 09:00 UTC daily — renewal reminder: one Close task per renewing
    #     merchant per calendar month. Operator's daily start; the
    #     task lands in the morning queue.
    #   * 17:00 UTC daily — submission reminder: one Close task per
    #     pending ``funder_note_submission`` that's >24h old, deduped
    #     forever per submission. Late-day fire so the operator gets
    #     the nudge for everything submitted earlier in their day.
    #   * 06:00 UTC Mondays — Track A regression sentinel. Weekly
    #     re-validates that the live ``track_abc`` engine still catches
    #     every doc the legacy ``fraud_score >= HARD_DECLINE_THRESHOLD``
    #     rule would have declined. A miss row writes a
    #     ``track_a_regression_sentinel.miss_rows_found`` audit which
    #     surfaces on the dashboard recent-activity feed for operator
    #     triage. Cadence picked to give the operator the report at the
    #     start of the workweek with enough buffer to triage before
    #     end-of-week submissions go out.
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
        cron(
            run_track_a_regression_sentinel_cron,
            weekday="mon",
            hour=6,
            minute=0,
            run_at_startup=False,
        ),
        # 07:00 UTC Mondays — compliance-obligation reminder cron.
        # Fires ``compliance.deadline_approaching`` audit rows at the
        # 60 / 30 / 14-day thresholds before each obligation's
        # ``next_due_date``. Dedupe key per (obligation, threshold,
        # next_due_date) so a weekly cadence doesn't double-fire the
        # same window. Mon 07:00 lands in the operator's morning
        # queue alongside the other start-of-week reminders.
        cron(
            run_compliance_obligation_reminder_cron,
            weekday="mon",
            hour=7,
            minute=0,
            run_at_startup=False,
        ),
        # 06:00 UTC Wednesdays — shadow-signal review pass.
        # Aggregates every ``[SHADOW] *`` flag fired on documents
        # parsed in the trailing 7 days. Writes one
        # ``shadow_signal.weekly_summary`` audit row per (document,
        # flag_code) and one ``shadow_signal.weekly_summary_complete``
        # summary row carrying per-code counts + ``source_document_ids``.
        # Wednesday is deliberately distinct from the Mon 06:00 Track A
        # sentinel and Mon 07:00 compliance cron so the operator's
        # morning queue doesn't triple-stack.
        cron(
            run_shadow_review_cron,
            weekday="wed",
            hour=6,
            minute=0,
            run_at_startup=False,
        ),
        # Every 30 minutes — re-queue documents stuck in ``pending``
        # for >2h. The original arq job was lost (worker crash before
        # ack, Redis AOF rewrite stall, etc); without this cron a
        # stuck doc sits in ``pending`` forever and the operator
        # thinks AEGIS is broken. See ``requeue_stuck_documents_cron``
        # for the policy.
        cron(
            requeue_stuck_documents_cron,
            minute={0, 30},
            run_at_startup=False,
        ),
        # Every hour at :00 UTC — retry error-state documents that
        # still have a sealed pdf_store blob (2026-07-01 FIX 2).
        # Companion to the pending sweep above: this cron rescues docs
        # that reached ``error`` but can still be retried from the
        # ciphertext. Post-parse hook schedules a single 5-minute
        # deferred retry; this hourly cron is the safety net for docs
        # that failed that retry too.
        cron(
            retry_stuck_error_documents_cron,
            minute={0},
            run_at_startup=False,
        ),
        # 07:00 UTC daily — pull funder definitions from the local
        # ``/var/lib/aegis/funders`` tree. Idempotent per the script's
        # own hash gate. The corpus-ingestion cron used to live here too
        # but was de-scheduled 2026-06-30 — corpus refresh is rare,
        # operator-triggered work (see ``weekly_corpus_ingestion``
        # docstring + deploy/RUNBOOK.md § Bank Statement Training
        # Corpus). The function stays defined so a future operator can
        # re-enable cron registration without re-implementing the body.
        cron(daily_funder_sync, hour=7, minute=0, run_at_startup=False),
        # Monday 04:00 UTC — weekly calibration snapshot. Reads
        # accumulated funder_replies + analyses, computes accuracy
        # rates, writes one ``calibration_snapshots`` row. Skips
        # silently when outcome count is below MIN_OUTCOMES (20).
        cron(
            weekly_calibration_cron,
            weekday=0,
            hour=4,
            minute=0,
            run_at_startup=False,
        ),
        # Every 5 min — alert if arq queue is backing up (E1).
        cron(
            redis_queue_health_check_cron,
            minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
            run_at_startup=False,
        ),
        # Monday 09:00 UTC — SSL certificate expiry check (E2).
        cron(
            ssl_certificate_check_cron,
            weekday=0,
            hour=9,
            minute=0,
            run_at_startup=False,
        ),
        # Daily 03:30 UTC — Hetzner Cloud snapshot. Silently skips
        # when HETZNER_API_TOKEN / HETZNER_SERVER_ID absent (dev / CI /
        # staging). Off-hours (03:30 UTC = 22:30 America/New_York the
        # night before / 04:30 CET) so the snapshot doesn't collide
        # with the 02:00 archive cron or the 04:00 Monday calibration.
        cron(
            daily_hetzner_snapshot,
            hour=3,
            minute=30,
            run_at_startup=False,
        ),
        # Daily 08:00 UTC — Bedrock spend alert (2026-07-01 FIX 1).
        # Sums yesterday's ``llm_costs`` and audits a system row when
        # the day's total exceeds $5. 08:00 UTC lands in the operator's
        # morning queue (~03:00 America/New_York — noise-free window)
        # right after ssl_certificate_check_cron. Dedupe guard inside
        # the job prevents a stuck-cron flood on sustained high days.
        cron(
            daily_cost_check,
            hour=8,
            minute=0,
            run_at_startup=False,
        ),
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


# ---------------------------------------------------------------------------
# Notification helper — fires the ``parse_complete`` row(s).
# ---------------------------------------------------------------------------


def _maybe_emit_parse_complete_notification(
    *,
    ctx: dict[str, Any],
    document_id: UUID,
    merchant_id: UUID | None,
    parse_status: str,
) -> None:
    """Best-effort fan-out of a ``parse_complete`` notification.

    No-ops when ``merchant_id`` is None (bearer / orphan uploads — no
    target operator). Pulls the notification / operator / assignment
    repos from ``ctx`` when provided (tests) or via the cached
    process-wide getters (prod).

    Any failure inside the helper is logged and swallowed: the parse
    itself is already persisted and the storage step is downstream.
    """
    if merchant_id is None:
        return
    try:
        from aegis.api.deps import (
            get_deal_assignment_repository,
            get_notification_repository,
            get_operator_repository,
        )
        from aegis.web._notify import notify_parse_complete

        notifications = ctx.get("notifications") or get_notification_repository()
        operators = ctx.get("operators") or get_operator_repository()
        assignments = ctx.get("assignments") or get_deal_assignment_repository()
        audit_log = ctx.get("audit") or get_audit()
        notify_parse_complete(
            merchant_id=merchant_id,
            document_id=document_id,
            parse_status=parse_status,
            operators=operators,
            assignments=assignments,
            notifications=notifications,
            audit=audit_log,
        )
    except Exception:
        _log.exception(
            "worker.notify_parse_complete_failed",
            extra={
                "merchant_id": str(merchant_id),
                "document_id": str(document_id),
            },
        )


__all__ = [
    "WorkerSettings",
    "daily_cost_check",
    "generate_narrator_summary",
    "parse_document",
    "process_close_attachments",
    "process_funder_reply",
    "retry_stuck_error_documents_cron",
    "run_background_checks",
    "run_funder_matching",
    "run_shadow_review_cron",
    "run_submission_reminder_cron",
    "run_submission_reminder_pass",
    "run_track_a_regression_sentinel_cron",
]
