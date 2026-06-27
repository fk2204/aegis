"""Weight-drift calibration page + review-button sink.

Surface for the outcome feedback loop (migration 074). Reads
``deal_outcomes`` + ``decisions.score_factors`` via
``aegis.scoring.weight_calibration.compute_weight_drift`` and renders one
row per ``FRAUD_WEIGHTS`` key with the current weight, fired / not-fired
charge-off rates, and a suggested weight derived from the empirical
ratio.

Routes:

* ``GET /ui/calibration`` — render the table.
* ``POST /ui/calibration/{flag_code}/review`` — record the operator's
  accepted / rejected / deferred decision into
  ``weight_calibration_log`` (migration 074). Does NOT mutate
  ``FRAUD_WEIGHTS`` — the operator edits that constant in code after
  reviewing the full report, same shadow-first discipline that ships
  every other scoring change.

Identity comes from the existing CF Access SSO email header via
``resolve_operator_email``; the fallback ``"dashboard"`` matches the
funder-replies route's behaviour for local-dev / test-client paths.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi import status as http_status
from fastapi.responses import HTMLResponse

from aegis.audit import AuditLog, AuditWriteError
from aegis.db import get_supabase
from aegis.logger import get_logger
from aegis.ops.operators import resolve_operator_email
from aegis.parser.pipeline import FRAUD_WEIGHTS
from aegis.scoring.weight_calibration import (
    WeightDriftEntry,
    WeightDriftReport,
    compute_weight_drift,
)
from aegis.web._role_gate import admin_only
from aegis.web._templates import templates

try:
    # ``get_audit`` is the canonical dep across the other sub-routers.
    # We import lazily-typed to avoid a circular import at package init.
    from aegis.api.deps import get_audit
except ImportError:  # pragma: no cover — defensive
    get_audit = None  # type: ignore[assignment]

_log = get_logger(__name__)

router = APIRouter()


_VALID_OPERATOR_DECISIONS: frozenset[str] = frozenset({"accepted", "rejected", "deferred"})
_DEFAULT_LOOKBACK_DAYS: int = 180


@router.get(
    "/calibration",
    response_class=HTMLResponse,
    dependencies=[Depends(admin_only)],
)
async def calibration_page(
    request: Request,
) -> HTMLResponse:
    """Render the weight-drift calibration table.

    Calls ``compute_weight_drift(lookback_days=180)``; if the underlying
    storage isn't configured (local dev without Supabase) we surface an
    empty report rather than a 500 — the operator still gets the page
    skeleton and can see the empty state.
    """
    report = await _safe_compute_report(lookback_days=_DEFAULT_LOOKBACK_DAYS)
    return templates.TemplateResponse(
        request,
        "calibration.html.j2",
        {
            "report": report,
            "fraud_weights": FRAUD_WEIGHTS,
        },
    )


@router.post(
    "/calibration/{flag_code}/review",
    response_class=HTMLResponse,
    dependencies=[Depends(admin_only)],
)
async def record_review(
    request: Request,
    flag_code: str,
    decision: Annotated[str, Form()],
    suggested_weight: Annotated[str, Form()],
    sample_size: Annotated[int, Form()],
    confidence: Annotated[str, Form()],
    notes: Annotated[str, Form()] = "",
    audit: Annotated[AuditLog | None, Depends(get_audit)] = None,
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
) -> HTMLResponse:
    """Persist one ``weight_calibration_log`` row and return a
    confirmation fragment.

    The endpoint NEVER mutates ``FRAUD_WEIGHTS`` — that constant is
    edited by hand after the operator reviews the full report. The
    response message reminds the operator of this contract.
    """
    decision_normalized = decision.strip().lower()
    if decision_normalized not in _VALID_OPERATOR_DECISIONS:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=(
                f"decision must be one of {sorted(_VALID_OPERATOR_DECISIONS)}; got {decision!r}"
            ),
        )
    if flag_code not in FRAUD_WEIGHTS:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"unknown flag_code: {flag_code!r}",
        )
    confidence_normalized = confidence.strip().lower()
    if confidence_normalized not in {"low", "medium", "high"}:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"confidence must be low/medium/high; got {confidence!r}",
        )

    try:
        suggested = Decimal(suggested_weight)
    except (ValueError, ArithmeticError) as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"invalid suggested_weight: {suggested_weight!r}",
        ) from exc

    current_weight = Decimal(str(FRAUD_WEIGHTS[flag_code])).quantize(Decimal("0.01"))
    reviewed_by = (actor_email or "dashboard").strip() or "dashboard"
    notes_normalized = notes.strip() or None

    row: dict[str, Any] = {
        "id": str(uuid4()),
        "flag_code": flag_code,
        "suggested_weight": str(suggested.quantize(Decimal("0.01"))),
        "current_weight": str(current_weight),
        "operator_decision": decision_normalized,
        "operator_notes": notes_normalized,
        "sample_size": sample_size,
        "confidence": confidence_normalized,
        "reviewed_at": datetime.now(UTC).isoformat(),
        "reviewed_by": reviewed_by,
    }

    try:
        get_supabase().table("weight_calibration_log").insert(row).execute()
    except Exception as exc:
        _log.error(
            "calibration.review_persist_failed flag_code=%s error_type=%s",
            flag_code,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"calibration_persist_unavailable: {type(exc).__name__}",
        ) from exc

    if audit is not None:
        try:
            audit.record(
                actor=reviewed_by,
                action="weight_calibration.reviewed",
                subject_type="fraud_weights",
                subject_id=None,
                details={
                    "flag_code": flag_code,
                    "operator_decision": decision_normalized,
                    "suggested_weight": str(suggested.quantize(Decimal("0.01"))),
                    "current_weight": str(current_weight),
                    "sample_size": sample_size,
                    "confidence": confidence_normalized,
                },
                actor_email=actor_email,
            )
        except AuditWriteError as exc:
            # Audit-write failure must fail the operation (CLAUDE.md
            # "Audit-write failures FAIL the operation, never silently
            # log-and-continue").
            raise HTTPException(
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"calibration_audit_unavailable: {exc}",
            ) from exc

    return HTMLResponse(
        content=(
            '<div class="calibration-recorded" data-test-id="calibration-recorded">'
            "Recorded. Update the FRAUD_WEIGHTS code manually after reviewing"
            " the full report."
            "</div>"
        ),
    )


async def _safe_compute_report(*, lookback_days: int) -> WeightDriftReport:
    """Wrap ``compute_weight_drift`` with a graceful empty-report fall
    back so the page renders even when the DB layer is not configured
    (local dev / first-deploy)."""
    try:
        return await compute_weight_drift(lookback_days=lookback_days)
    except Exception as exc:
        _log.warning(
            "calibration.compute_failed lookback_days=%d error_type=%s",
            lookback_days,
            type(exc).__name__,
        )
        return WeightDriftReport(
            generated_at=datetime.now(UTC),
            lookback_days=lookback_days,
            total_outcomes=0,
            entries=[],
        )


__all__ = ["WeightDriftEntry", "router"]
