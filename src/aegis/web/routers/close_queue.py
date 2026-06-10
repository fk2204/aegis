"""Close-queue sub-router — pipeline state for every Close-sourced merchant.

Routes:
  * ``GET /ui/close-queue``  — aggregate pipeline state per Close lead

The classifier (`_classify_close_pipeline_state`) is consumed by tests
in ``tests/test_close_queue.py`` via re-export from
``aegis.web.router`` — keep the symbol importable from there.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from aegis.api.deps import (
    get_audit,
    get_merchant_repository,
    get_repository,
)
from aegis.audit import AuditLog
from aegis.merchants.repository import MerchantRepository
from aegis.storage import DocumentRepository, DocumentRow
from aegis.web._templates import templates

router = APIRouter()


# Close-queue thresholds. A pull enqueued but not completed within this
# many hours is suspect — the worker either crashed silently or the lead
# has a payload Close hasn't published yet; either way it surfaces as
# STUCK so the operator can retry. Parsing-pending threshold is much
# tighter because a Bedrock parse should not take more than a few
# minutes per document.
_CLOSE_QUEUE_STALE_PULL_HOURS: Final[float] = 6.0
_CLOSE_QUEUE_STALE_PARSE_HOURS: Final[float] = 1.0


# Flag-category → human label for the GATED detail line. The classifier
# peeks at all_flags on each manual_review doc and surfaces the unique
# categories so the operator sees WHY at a glance — "editor metadata +
# reconciliation drift" reads as a tampering signal, "OFAC match" as a
# sanctions hit, "OCR concerns" as a missing-data review. Distinct
# semantics demand distinct response — re-pulling won't change
# tampering flags but it might clear an OCR concern.
_CLOSE_QUEUE_FLAG_CATEGORY_LABELS: Final[dict[str, str]] = {
    "META":    "editor metadata",
    "MATH":    "reconciliation drift",
    "PATTERN": "pattern signal",
    "STRUCT":  "PDF structure",
    "OFAC":    "OFAC match",
    "LLM":     "LLM concerns",
    "OCR":     "OCR concerns",
}
_CLOSE_QUEUE_FLAG_CATEGORY_ORDER: Final[tuple[str, ...]] = (
    "OFAC", "META", "MATH", "PATTERN", "STRUCT", "OCR", "LLM",
)


def _gating_reason_labels(docs: list[DocumentRow]) -> list[str]:
    """Extract unique [CATEGORY] flag prefixes across docs, map to
    human labels in stable order. Empty list if no docs carry tagged
    flags — fall back to a generic phrase in the classifier."""
    found: set[str] = set()
    for d in docs:
        for f in (d.all_flags or []):
            if isinstance(f, str) and f.startswith("[") and "]" in f:
                cat = f[1:f.find("]")]
                if cat in _CLOSE_QUEUE_FLAG_CATEGORY_LABELS:
                    found.add(cat)
    return [
        _CLOSE_QUEUE_FLAG_CATEGORY_LABELS[c]
        for c in _CLOSE_QUEUE_FLAG_CATEGORY_ORDER
        if c in found
    ]


def _parse_audit_ts(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _hours_since(ts: datetime | None, now: datetime) -> float | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return max(0.0, (now - ts).total_seconds() / 3600.0)


def _classify_close_pipeline_state(
    *,
    docs: list[DocumentRow],
    audit_rows: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    """Derive the Close-queue state for one merchant.

    Returns a dict with ``state`` (machine token), ``label`` (chip
    text), ``severity`` (chip color: good/warn/bad/info), ``action``
    (``retry`` / ``review`` / ``None``), and ``detail`` (one-line
    human reason).

    States distinguish three operator-relevant categories:

    * **Needs a retry** — ``failed_pull``, ``failed_parse``, ``stuck``.
      The rescan button is the right action.
    * **Needs an underwriter** — ``gated``. The parser ran and flagged
      integrity / reconciliation concerns; the right action is to
      open the dossier, not to retry.
    * **Informational** — ``awaiting_pull``, ``parsing``, ``scored``.
      No action needed.

    At 30 deals/day the distinction matters: "Score unavailable" on
    the dossier is ambiguous (broken pipeline vs. flagged for human
    review vs. still working). The queue makes the reason readable
    at a glance.
    """
    # Defensive: sort by created_at descending so we look at the LATEST
    # orchestration outcome regardless of how the caller ordered the
    # rows. The audit-log API returns newest-first today, but a future
    # bulk-load path could pass them oldest-first and silently invert
    # the verdict (e.g. "enqueued" stays current after "list_failed").
    close_orch_rows = sorted(
        (
            r
            for r in audit_rows
            if str(r.get("action", "")).startswith("close.orchestration.")
        ),
        key=lambda r: _parse_audit_ts(r.get("created_at")) or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    last_orch = close_orch_rows[0] if close_orch_rows else None
    last_action = (last_orch or {}).get("action")
    last_ts = _parse_audit_ts((last_orch or {}).get("created_at"))

    if not docs:
        if last_action == "close.orchestration.list_failed":
            details = (last_orch or {}).get("details") or {}
            err_msg = ""
            if isinstance(details, dict):
                err_msg = str(details.get("message") or details.get("error") or "")
            return {
                "state": "failed_pull",
                "label": "Failed to pull",
                "severity": "bad",
                "action": "retry",
                "detail": (
                    f"Close listing failed: {err_msg[:80]}"
                    if err_msg
                    else "Close listing failed"
                ),
            }
        if last_action in (
            "close.orchestration.enqueued",
            "close.orchestration.manual_rescan",
        ):
            elapsed_h = _hours_since(last_ts, now)
            if elapsed_h is not None and elapsed_h > _CLOSE_QUEUE_STALE_PULL_HOURS:
                return {
                    "state": "stuck",
                    "label": f"Stuck (no pull, {elapsed_h:.0f}h)",
                    "severity": "warn",
                    "action": "retry",
                    "detail": (
                        f"Pull enqueued {elapsed_h:.0f}h ago with no completion"
                    ),
                }
            return {
                "state": "awaiting_pull",
                "label": "Pulling",
                "severity": "info",
                "action": None,
                "detail": "Close attachment pull in flight",
            }
        return {
            "state": "stuck",
            "label": "Stuck (no audit)",
            "severity": "warn",
            "action": "retry",
            "detail": "No Close orchestration audit on file",
        }

    pending = [d for d in docs if d.parse_status == "pending"]
    error = [d for d in docs if d.parse_status == "error"]
    manual_review = [d for d in docs if d.parse_status == "manual_review"]
    clean = [d for d in docs if d.parse_status in ("proceed", "review")]

    if pending:
        oldest_ts = min(
            (d.uploaded_at for d in pending if d.uploaded_at is not None),
            default=None,
        )
        elapsed_h = _hours_since(oldest_ts, now)
        if elapsed_h is not None and elapsed_h > _CLOSE_QUEUE_STALE_PARSE_HOURS:
            return {
                "state": "stuck",
                "label": f"Stuck (parse {elapsed_h:.0f}h)",
                "severity": "warn",
                "action": "retry",
                "detail": (
                    f"{len(pending)} document(s) pending parse for "
                    f"{elapsed_h:.0f}h"
                ),
            }
        return {
            "state": "parsing",
            "label": f"Parsing {len(docs) - len(pending)}/{len(docs)}",
            "severity": "info",
            "action": None,
            "detail": f"{len(pending)} document(s) still parsing",
        }

    if manual_review:
        extras = []
        if error:
            extras.append(f"{len(error)} errored")
        if clean:
            extras.append(f"{len(clean)} clean")
        suffix = (" · " + ", ".join(extras)) if extras else ""
        # Surface the gating reasons (flag categories) so the operator
        # can distinguish tampering (editor metadata + reconciliation
        # drift) from OFAC, from OCR concerns, from PDF structure issues
        # — each implies a different next move.
        reason_labels = _gating_reason_labels(manual_review)
        reason_phrase = (
            " + ".join(reason_labels)
            if reason_labels
            else "integrity / reconciliation concerns"
        )
        return {
            "state": "gated",
            "label": "Needs underwriter",
            "severity": "warn",
            "action": "review",
            "detail": (
                f"{len(manual_review)} statement(s) flagged · "
                f"{reason_phrase}{suffix}"
            ),
        }
    if error and not clean:
        return {
            "state": "failed_parse",
            "label": "Failed to parse",
            "severity": "bad",
            "action": "retry",
            "detail": f"All {len(error)} document(s) errored during parse",
        }
    if clean:
        suffix = f" · {len(error)} errored" if error else ""
        return {
            "state": "scored",
            "label": "Scored",
            "severity": "good",
            "action": None,
            "detail": f"{len(clean)} clean statement(s){suffix}",
        }
    return {
        "state": "stuck",
        "label": "Stuck",
        "severity": "warn",
        "action": "retry",
        "detail": "Unknown document state",
    }


# Sort order: failures first (most urgent), then stuck, then gated
# (operator action needed), then in-flight, then scored. Within a state
# tier, sort alphabetically by business name for predictable scanning.
_CLOSE_QUEUE_STATE_ORDER: Final[dict[str, int]] = {
    "failed_pull":   0,
    "failed_parse":  1,
    "stuck":         2,
    "gated":         3,
    "parsing":       4,
    "awaiting_pull": 5,
    "scored":        6,
}


@router.get("/close-queue", response_class=HTMLResponse)
async def close_queue(
    request: Request,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
) -> HTMLResponse:
    """Pipeline state for every Close-sourced merchant.

    Aggregates merchants where ``close_lead_id IS NOT NULL`` into one
    row each, classified by audit + document state. FAILED rows expose
    the rescan retry button. GATED rows link to the dossier for
    operator review. The point at 30 deals/day is that a silently-stuck
    merchant cannot fall through the cracks — every Close-sourced
    deal surfaces here in a single sortable view with the reason it
    needs (or does not need) attention.
    """
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for m in merchants.list_all():
        if not m.close_lead_id:
            continue
        merchant_docs = docs.list_documents(merchant_id=m.id, limit=50)
        merchant_audit = audit.list_for_subject(
            subject_type="merchant", subject_id=m.id, limit=50
        )
        state = _classify_close_pipeline_state(
            docs=merchant_docs, audit_rows=merchant_audit, now=now
        )
        last_orch = next(
            (
                r
                for r in merchant_audit
                if str(r.get("action", "")).startswith("close.orchestration.")
            ),
            None,
        )
        rows.append(
            {
                "merchant_id": str(m.id),
                "business_name": m.business_name,
                "close_lead_id": m.close_lead_id,
                "state": state,
                "doc_count": len(merchant_docs),
                "last_audit_action": (last_orch or {}).get("action") or "—",
                "last_audit_at": _parse_audit_ts(
                    (last_orch or {}).get("created_at")
                ),
            }
        )
    rows.sort(
        key=lambda r: (
            _CLOSE_QUEUE_STATE_ORDER.get(r["state"]["state"], 99),
            r["business_name"].lower(),
        )
    )
    # State counts for the deck header — "3 failed, 2 needs review, …"
    state_counts: dict[str, int] = {}
    for r in rows:
        state_counts[r["state"]["state"]] = (
            state_counts.get(r["state"]["state"], 0) + 1
        )
    return templates.TemplateResponse(
        request,
        "close_queue.html.j2",
        {
            "rows": rows,
            "state_counts": state_counts,
            "stale_pull_hours": _CLOSE_QUEUE_STALE_PULL_HOURS,
            "stale_parse_hours": _CLOSE_QUEUE_STALE_PARSE_HOURS,
        },
    )
