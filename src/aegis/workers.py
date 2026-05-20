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
    get_funder_reply_repository,
    get_llm,
    get_repository,
)
from aegis.audit import AuditLog
from aegis.audit_archiver import run_archive_cron
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
# Phase 7 — audit retention archiver cron (mp §17).
# ---------------------------------------------------------------------------
#
# The archiver entrypoint is defined in aegis.audit_archiver. The cron
# registration lives here because arq reads ``cron_jobs`` off WorkerSettings.


def _build_archive_cron() -> Any:
    """Wrap arq.cron(run_archive_cron, hour=3, minute=30).

    Indirect call so the import of arq.cron stays inside this function —
    keeps the module import light when arq is unavailable (tests that
    monkey-patch the redis surface). ``arq.cron`` returns a CronJob
    dataclass; we forward it unchanged.
    """
    from arq import cron as _arq_cron

    from aegis.audit_archiver import run_archive_cron

    return _arq_cron(
        run_archive_cron,
        name="audit_archiver.daily",
        hour=3,
        minute=30,
        unique=True,
        run_at_startup=False,
        timeout=600,
    )


class WorkerSettings:
    """arq config. Reads concurrency + timeout from env via Settings."""

    functions = (parse_document, process_funder_reply)
    # Nightly at 02:00 UTC — low-traffic window. Per master plan §17,
    # the archive cron runs daily and is the only cron registered here.
    cron_jobs = (
        cron(run_archive_cron, hour=2, minute=0, run_at_startup=False),
    )
    on_startup = _on_startup
    on_shutdown = _on_shutdown

    # Daily cron jobs. The audit retention archiver runs at 03:30 UTC —
    # off-peak for both Bedrock + the operator's review window, and
    # well clear of the Supabase daily backup window (mp Phase 11 #4).
    # See aegis.audit_archiver.run_archive_cron for the body.
    cron_jobs = (
        _build_archive_cron(),
    )

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


__all__ = ["WorkerSettings", "parse_document", "process_funder_reply"]
