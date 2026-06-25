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
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from arq import cron
from redis.exceptions import RedisError

if TYPE_CHECKING:
    from arq.connections import RedisSettings

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
        raise
    finally:
        _safe_unlink(pdf_path)

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
    finally:
        _safe_unlink(pdf_path)

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

    return {
        "document_id": str(document_id),
        "parse_status": result.parse_status,
        "fraud_score": 0,  # processor pipeline doesn't compute a fraud_score
    }


def _safe_unlink(pdf_path: str) -> None:
    """Delete the temp PDF; log (but don't raise) on filesystem failure.

    The PDF is the secret here — leaving it behind is the bigger risk
    than failing a single job because of a stale handle.
    """
    p = Path(pdf_path)
    if not p.exists():
        return
    try:
        p.unlink()
    except OSError:
        _log.warning("worker.cleanup_failed path=%s", pdf_path)


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
    ignore_pin: bool = False,
) -> dict[str, Any]:
    """List attachments on a Close Lead, filter, persist each statement
    through :func:`persist_pdf_upload`.

    Filter pipeline (applied in order, each step audits its skips):

      1. ``content_type == 'application/pdf'`` — strict. Kills the
         PNG-named-statement case and every non-PDF surface.
      2. Pin gate (default ``ignore_pin=False``):

           * Pin-only path: keep PDFs with ``is_pinned=True``; skipped
             unpinned PDFs audit ``reason='not_pinned'``. The
             operator's pin IS the "this is a statement" confirmation
             — filename filter is bypassed so an operator pinning
             ``april_KYC.pdf`` (an oddly-named statement) processes it.
           * ``ignore_pin=True`` path: skip the pin check; apply the
             filename-substring filter as the remaining signal. The
             rescan-with-``ignore_pin`` UI button (chunk 5) sets this
             for Leads where the operator knows everything is a
             statement (e.g. fresh inbound web-form leads).

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

    ``ignore_pin`` is set by the rescan-with-``ignore_pin`` UI button.
    Audits ``close.orchestration.pin_ignored`` once when set.

    Empty-state signal: if the pin-only path finds ≥1 PDF but none
    pinned, write ``close.orchestration.no_pinned_files`` so the
    merchant detail UI (chunk 5) can surface "Pin the bank-statement
    files in Close, then click Rescan." Better than a silent no-op.

    Per-attachment errors (download_failed, pydantic validation, etc.)
    are isolated and logged via audit rows; the loop walks the full
    list before returning a summary dict. The summary lands in arq's
    job log and (for ``trigger='rescan'``) drives the cap-override +
    pin-override UI buttons.

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
    summary["ignore_pin"] = ignore_pin

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

    # Filter 2 — pin gate (default) OR filename filter (ignore_pin path).
    # Pin and filename are mutually exclusive signals: when the operator
    # has pinned, their intent overrides the filename heuristic;
    # when they've explicitly bypassed pin, the filename is the only
    # remaining signal.
    statement_candidates: list[CloseAttachment] = []
    if ignore_pin:
        if pdf_attachments:
            audit.record(
                actor="worker",
                actor_email=actor_email,
                action="close.orchestration.pin_ignored",
                subject_type="merchant",
                subject_id=merchant.id,
                details={
                    "close_lead_id": close_lead_id,
                    "trigger": trigger,
                    "candidate_count": len(pdf_attachments),
                },
            )
        for att in pdf_attachments:
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
    else:
        for att in pdf_attachments:
            # Operator confirmed via EITHER signal — file pin (Files tab)
            # OR note pin (activity-feed pin on the wrapping Note). The
            # natural Close UX is note-feed pinning; file pin remains
            # supported for files attached without a note wrapper.
            if att.is_pinned or att.note_pinned:
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
                    "reason": "not_pinned",
                    "close_lead_id": close_lead_id,
                    "file_pinned": att.is_pinned,
                    "note_pinned": att.note_pinned,
                },
            )
        # Empty-state signal: ≥1 PDF on the Lead but none pinned. The
        # merchant detail UI (chunk 5) reads this audit row to show
        # "Pin the bank-statement files in Close, then click Rescan."
        if not statement_candidates and pdf_attachments:
            audit.record(
                actor="worker",
                actor_email=actor_email,
                action="close.orchestration.no_pinned_files",
                subject_type="merchant",
                subject_id=merchant.id,
                details={
                    "close_lead_id": close_lead_id,
                    "trigger": trigger,
                    "total_pdfs_seen": len(pdf_attachments),
                    "unpinned_pdfs": [
                        {"id": a.id, "name": a.name} for a in pdf_attachments
                    ],
                },
            )

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
            file_bytes, filename = close_client.download_attachment(att)
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
