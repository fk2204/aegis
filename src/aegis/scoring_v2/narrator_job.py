"""arq job — auto-generate narrator summary after parse_status='proceed'.

Two-piece module:

* :func:`enqueue_narrator_summary_from_worker` — fire-and-forget helper
  used inside :func:`aegis.workers.parse_document`. Posts a job onto the
  arq queue (or the in-process ``pending_jobs`` fallback for the test
  harness) and audit-logs the enqueue outcome. Never raises.
* :func:`generate_narrator_summary` — the arq worker function. Builds a
  :class:`NarratorContext` from the just-parsed document, calls
  :func:`aegis.scoring_v2.narrator.narrate_deal`, persists via
  :meth:`DocumentRepository.set_narrator_summary`, and writes one of
  ``narrator.generated`` / ``narrator.skipped_existing`` / ``narrator.failed``
  audit rows.

**Idempotency contract**

The job is keyed on ``document_id``. If ``analysis.narrator_summary`` is
already non-null when the job fires, the job audits a
``narrator.skipped_existing`` row and returns without calling Bedrock.
Re-enqueues (manual reparse, race conditions) MUST NOT re-spend Bedrock
budget on already-narrated docs.

**Non-blocking contract**

The parser's own outcome never depends on the narrator:

* Enqueue failure (Redis blip, arq pool not initialized) audits
  ``narrator.enqueue_failed`` and returns False. The parse return path
  is unchanged.
* Worker-side failure (Bedrock error, context-build error) audits
  ``narrator.failed`` and returns normally. ``parse_status`` is already
  durable from the upstream :func:`persist_parse_result` call; nothing
  the narrator does can mutate it.

This mirrors the posture of :func:`aegis.background_checks.enqueue_background_checks`.

**Duplication note**

The :class:`NarratorContext` build duplicates the logic in
:func:`aegis.web.routers.merchants.merchant_narrator_refresh`. Both call
sites compute the same per-document scoring inputs from scratch because
``NarratorContext`` is intentionally pure-deterministic over loaded
objects (see ``aegis.scoring_v2.narrator.NarratorContext`` docstring) —
neither caller can share state with the other. A future refactor could
extract a shared ``build_narrator_context_for_document`` helper; today
the duplication is bounded to the context-assembly block and is the
cheaper path given the workflow-first principle in
:doc:`CLAUDE.md`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from aegis.logger import get_logger
from aegis.scoring_v2.narrator import (
    NarratorContext,
    NarratorError,
    narrate_deal,
)

if TYPE_CHECKING:
    from aegis.audit import AuditLog

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Enqueue helper — called from parse_document.
# ---------------------------------------------------------------------------


async def enqueue_narrator_summary_from_worker(
    *,
    ctx: dict[str, Any],
    document_id: UUID,
    merchant_id: UUID,
    audit: AuditLog,
) -> bool:
    """Fire-and-forget enqueue from inside an arq worker.

    Unlike :func:`aegis.background_checks.enqueue_background_checks`
    (which is called from FastAPI routes and reads ``request.app.state``),
    this helper is invoked from inside another arq worker and reads the
    Redis pool from ``ctx['redis']`` — the standard arq worker context.

    The in-process fallback writes to ``ctx['pending_narrator_jobs']`` so
    test harnesses that don't wire a real Redis can still assert on the
    enqueue side effect without hitting the wire. Mirrors the pattern in
    :func:`aegis.workers._orchestrator_enqueue`.

    Returns True on enqueue success, False on enqueue failure. Never raises.
    """
    try:
        redis = ctx.get("redis")
        if redis is not None:
            await redis.enqueue_job(
                "generate_narrator_summary",
                str(document_id),
                str(merchant_id),
            )
        else:
            pending = cast("list[dict[str, str]]", ctx.setdefault("pending_narrator_jobs", []))
            pending.append(
                {
                    "document_id": str(document_id),
                    "merchant_id": str(merchant_id),
                }
            )
    except Exception as exc:
        audit.record(
            actor="system",
            action="narrator.enqueue_failed",
            subject_type="document",
            subject_id=document_id,
            details={
                "merchant_id": str(merchant_id),
                "error": type(exc).__name__,
                "message": str(exc)[:500],
            },
        )
        _log.warning(
            "narrator.enqueue_failed document_id=%s merchant_id=%s error=%s",
            document_id,
            merchant_id,
            exc,
            exc_info=True,
        )
        return False

    audit.record(
        actor="system",
        action="narrator.enqueued",
        subject_type="document",
        subject_id=document_id,
        details={"merchant_id": str(merchant_id)},
    )
    return True


# ---------------------------------------------------------------------------
# Worker function — registered in WorkerSettings.functions.
# ---------------------------------------------------------------------------


async def generate_narrator_summary(
    ctx: dict[str, Any],
    document_id_str: str,
    merchant_id_str: str,
) -> dict[str, Any]:
    """arq job — generate the Bedrock narrator summary for ``document_id``.

    Idempotent. If ``analyses.narrator_summary`` is already non-null,
    audits ``narrator.skipped_existing`` and returns without calling
    Bedrock. Otherwise builds a :class:`NarratorContext` from the loaded
    document + analysis + scoring inputs, calls :func:`narrate_deal`,
    persists the result, and audits ``narrator.generated``.

    On any exception, audits ``narrator.failed`` (with the masked error
    string) and returns normally — the calling parse worker's
    ``parse_status`` is already durable and must not be retroactively
    affected by narrator trouble.

    Repository / audit / merchant / OFAC / LLM clients are read from
    ``ctx`` when provided (the test harness path) and fall back to the
    cached process-wide singletons (``aegis.api.deps``).
    """
    # Lazy imports: keep this module importable without FastAPI in the path.
    from aegis.api.deps import (
        get_audit,
        get_merchant_repository,
        get_ofac_client,
        get_repository,
    )
    from aegis.merchants.repository import MerchantNotFoundError
    from aegis.ops.cost_tracking import build_cost_tracking_client
    from aegis.scoring.multi_month import score_input_multi_month
    from aegis.scoring.score import score_deal
    from aegis.scoring_v2.balance_health import compute_balance_health
    from aegis.scoring_v2.industry import industry_risk_tier
    from aegis.scoring_v2.mca_stack import aggregate_mca_stack
    from aegis.scoring_v2.score_deal_inputs import compute_score_deal_track_inputs
    from aegis.storage import DocumentNotFoundError
    from aegis.web._router_helpers import _collect_analyzed_for_merchant
    from aegis.web.routers.merchants import _dossier_pattern_analysis

    audit = cast("AuditLog", ctx.get("audit") or get_audit())
    docs = ctx.get("docs") or get_repository()
    merchants = ctx.get("merchants") or get_merchant_repository()

    try:
        document_id = UUID(document_id_str)
        merchant_id = UUID(merchant_id_str)
    except ValueError as exc:
        _log.warning(
            "narrator.invalid_uuid document_id=%s merchant_id=%s error=%s",
            document_id_str,
            merchant_id_str,
            exc,
        )
        return {
            "document_id": document_id_str,
            "merchant_id": merchant_id_str,
            "skipped": True,
            "reason": "invalid_uuid",
        }

    try:
        analysis = docs.get_analysis(document_id)
        if analysis is None:
            # Race: parse_status flipped to proceed but the analyses row
            # write hasn't landed yet (or was rolled back). Skip — the
            # next parse will re-enqueue.
            audit.record(
                actor="system",
                action="narrator.skipped_no_analysis",
                subject_type="document",
                subject_id=document_id,
                details={"merchant_id": str(merchant_id)},
            )
            return {
                "document_id": document_id_str,
                "merchant_id": merchant_id_str,
                "skipped": True,
                "reason": "no_analysis",
            }

        # Idempotency short-circuit. The "Refresh narrator" button
        # overwrites by definition; the auto-trigger does not.
        if analysis.narrator_summary is not None:
            audit.record(
                actor="system",
                action="narrator.skipped_existing",
                subject_type="document",
                subject_id=document_id,
                details={"merchant_id": str(merchant_id)},
            )
            return {
                "document_id": document_id_str,
                "merchant_id": merchant_id_str,
                "skipped": True,
                "reason": "narrator_already_set",
            }

        document = docs.get_document(document_id)
        merchant = merchants.get(merchant_id)

        items = _collect_analyzed_for_merchant(docs, merchant_id, bundle=None)
        if not items:
            audit.record(
                actor="system",
                action="narrator.skipped_no_items",
                subject_type="document",
                subject_id=document_id,
                details={"merchant_id": str(merchant_id)},
            )
            return {
                "document_id": document_id_str,
                "merchant_id": merchant_id_str,
                "skipped": True,
                "reason": "no_analyzed_items",
            }

        latest_transactions = docs.list_transactions(document_id)
        pattern_analysis = _dossier_pattern_analysis(analysis, latest_transactions)
        score_input = score_input_multi_month(merchant, items, pattern_analysis=pattern_analysis)
        track_a_verdict, track_b_band = compute_score_deal_track_inputs(
            documents=[d for d, _ in items],
            list_transactions=docs.list_transactions,
            analyses_by_doc={d.id: a for d, a in items},
            merchant_id=merchant_id,
            industry_tier=industry_risk_tier(merchant.industry_choice),
        )
        score_result = score_deal(
            score_input,
            ofac=get_ofac_client(),
            track_a_verdict=track_a_verdict,
            track_b_band=track_b_band,
        )

        mca_stack = aggregate_mca_stack(
            transactions=latest_transactions,
            monthly_revenue=analysis.monthly_revenue,
            period_days=analysis.statement_days,
        )
        balance_health = compute_balance_health(
            transactions=latest_transactions,
            period_days=analysis.statement_days,
        )

        narrator_ctx = NarratorContext(
            merchant=merchant,
            document_id=document_id,
            analysis=analysis,
            score_result=score_result,
            track_a_verdict=track_a_verdict,
            track_b_band=track_b_band,
            mca_stack=mca_stack,
            balance_health=balance_health,
            all_flags=tuple(document.all_flags or ()),
            top_funder_name=None,
            top_funder_factor=None,
            top_funder_advance=None,
            top_funder_term_days=None,
            voided_check_on_file=merchant.voided_check_on_file,
            drivers_license_on_file=merchant.drivers_license_on_file,
            bank_statements_months=merchant.bank_statements_months,
        )

        # Wrap the bedrock call with the cost-tracking client so the
        # auto-narrator's spend lands on the same llm_costs ledger the
        # operator-triggered refresh does. ``call_type="narrator"`` lines
        # up with the audit category in :mod:`aegis.ops.cost_tracking`.
        bedrock = ctx.get("bedrock") or build_cost_tracking_client(
            call_type="narrator",
            document_id=document_id,
            merchant_id=merchant_id,
        )
        summary = narrate_deal(narrator_ctx, bedrock=cast("Any", bedrock))

        payload = summary.model_dump(mode="json")
        docs.set_narrator_summary(document_id, payload)

        audit.record(
            actor="system",
            action="narrator.generated",
            subject_type="document",
            subject_id=document_id,
            details={
                "merchant_id": str(merchant_id),
                "model_id": summary.model_id,
                "recommended_action": summary.recommended_action.action,
                "flag_count": len(summary.flag_explanations),
                "trigger": "auto_after_parse",
            },
        )
        return {
            "document_id": document_id_str,
            "merchant_id": merchant_id_str,
            "generated": True,
            "model_id": summary.model_id,
        }

    except (DocumentNotFoundError, MerchantNotFoundError) as exc:
        # Cross-table race: parse_status proceed-and-merchant resolved
        # but the merchant or document row was soft-deleted between
        # enqueue and dequeue. Audit and return without raising.
        audit.record(
            actor="system",
            action="narrator.failed",
            subject_type="document",
            subject_id=document_id,
            details={
                "merchant_id": str(merchant_id),
                "error": type(exc).__name__,
                "message": str(exc)[:200],
            },
        )
        return {
            "document_id": document_id_str,
            "merchant_id": merchant_id_str,
            "skipped": True,
            "reason": "row_missing",
        }
    except NarratorError as exc:
        # Bedrock returned non-conforming output. Audit + swallow.
        audit.record(
            actor="system",
            action="narrator.failed",
            subject_type="document",
            subject_id=document_id,
            details={
                "merchant_id": str(merchant_id),
                "error": "NarratorError",
                "message": str(exc)[:200],
            },
        )
        _log.warning(
            "narrator.failed document_id=%s merchant_id=%s error=%s",
            document_id,
            merchant_id,
            exc,
        )
        return {
            "document_id": document_id_str,
            "merchant_id": merchant_id_str,
            "generated": False,
            "reason": "narrator_error",
        }
    except Exception as exc:
        # Anything else (context-build failure, scoring failure, DB error
        # during the persist step) is captured here so the worker never
        # cascades the parse's outcome.
        audit.record(
            actor="system",
            action="narrator.failed",
            subject_type="document",
            subject_id=document_id,
            details={
                "merchant_id": str(merchant_id),
                "error": type(exc).__name__,
                "message": str(exc)[:200],
            },
        )
        _log.warning(
            "narrator.failed_unexpected document_id=%s merchant_id=%s error=%s",
            document_id,
            merchant_id,
            exc,
            exc_info=True,
        )
        return {
            "document_id": document_id_str,
            "merchant_id": merchant_id_str,
            "generated": False,
            "reason": "unexpected_error",
        }


__all__ = [
    "enqueue_narrator_summary_from_worker",
    "generate_narrator_summary",
]
