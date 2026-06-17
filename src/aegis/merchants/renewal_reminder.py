"""Renewal reminder cron — once-per-month Close task per renewing merchant.

Runs daily at 09:00 UTC via arq. Walks the renewal pipeline (merchants
whose ``maturity_date`` is within the next 14 days OR overdue) and posts
one "Renewal approaching" Close task per merchant. Dedupe key is a
``close.task.renewal_reminder`` audit row stamped with the calendar
month — the cron runs daily but only the first run of each month
actually posts a task, so the operator's Close queue stays clean.

Skipped silently when a merchant has no ``close_lead_id`` (renewal
pipeline pre-dates the Close handoff).

Mirrors the ``aegis.audit_archiver.run_archive_cron`` entrypoint shape
so workers.py can register it via the existing ``cron(...)`` helper.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from aegis.audit import AuditLog
from aegis.close.client import CloseClient, CloseError
from aegis.logger import get_logger
from aegis.merchants.repository import (
    MerchantRepository,
    list_renewal_pipeline,
)

_log = get_logger(__name__)


def _current_calendar_month(today: date) -> str:
    """Return ``YYYY-MM`` — the dedup key for "already prompted this month"."""
    return today.isoformat()[:7]


def _has_reminder_this_month(
    audit: AuditLog,
    merchant_id: Any,  # noqa: ANN401 — UUID, but stays Any for in-memory + Supabase parity
    month_key: str,
) -> bool:
    rows = audit.list_for_subject(
        subject_type="merchant",
        subject_id=merchant_id,
        action="close.task.renewal_reminder",
        limit=200,
    )
    return any((r.get("details") or {}).get("month") == month_key for r in rows)


def run_renewal_reminder_pass(
    *,
    audit: AuditLog,
    merchants: MerchantRepository,
    close_client: CloseClient,
    today: date | None = None,
) -> dict[str, int]:
    """Post one renewal-reminder Close task per merchant due in the next
    14 days, deduped per calendar month.

    Returns a small summary dict so the cron wrapper can log spot-check
    counts. The authoritative trail is in audit_log.
    """
    today = today or datetime.now(UTC).date()
    month_key = _current_calendar_month(today)

    rows = list_renewal_pipeline(merchants, today=today)
    created = 0
    skipped_no_lead = 0
    skipped_dup = 0
    failed = 0

    for row in rows:
        try:
            merchant = merchants.get(row.merchant_id)
        except KeyError:
            # Concurrent delete — treat as skipped, don't crash the cron.
            continue
        if not merchant.close_lead_id:
            skipped_no_lead += 1
            continue
        if _has_reminder_this_month(audit, merchant.id, month_key):
            skipped_dup += 1
            continue

        text = (
            f"Renewal approaching — {merchant.business_name} matures "
            f"{row.maturity_date.isoformat()}"
        )
        try:
            close_client.create_task(
                lead_id=merchant.close_lead_id,
                text=text,
                due_date=today,
            )
        except CloseError as exc:
            failed += 1
            audit.record(
                actor="cron.renewal_reminder",
                action="close.task.renewal_reminder_failed",
                subject_type="merchant",
                subject_id=merchant.id,
                details={
                    "close_lead_id": merchant.close_lead_id,
                    "month": month_key,
                    "status_code": exc.status_code,
                    "error": str(exc)[:200],
                },
            )
            continue

        audit.record(
            actor="cron.renewal_reminder",
            action="close.task.renewal_reminder",
            subject_type="merchant",
            subject_id=merchant.id,
            details={
                "close_lead_id": merchant.close_lead_id,
                "month": month_key,
                "maturity_date": row.maturity_date.isoformat(),
                "task_text": text,
            },
        )
        created += 1

    return {
        "considered": len(rows),
        "created": created,
        "skipped_no_lead": skipped_no_lead,
        "skipped_dup": skipped_dup,
        "failed": failed,
    }


async def run_renewal_reminder_cron(ctx: dict[str, Any]) -> dict[str, int]:
    """arq daily cron entrypoint.

    Reads dependencies from the arq context (tests inject in-memory
    fakes) and falls back to the process-wide DI when not present, the
    same pattern ``run_archive_cron`` uses.
    """
    from aegis.api.deps import get_audit, get_close_client, get_merchant_repository

    audit = ctx.get("audit") or get_audit()
    merchants = ctx.get("merchants") or get_merchant_repository()
    close_client = ctx.get("close_client") or get_close_client()

    summary = run_renewal_reminder_pass(
        audit=audit,
        merchants=merchants,
        close_client=close_client,
    )
    _log.info(
        "renewal_reminder.run considered=%s created=%s skipped_no_lead=%s skipped_dup=%s failed=%s",
        summary["considered"],
        summary["created"],
        summary["skipped_no_lead"],
        summary["skipped_dup"],
        summary["failed"],
    )
    return summary


__all__ = [
    "run_renewal_reminder_cron",
    "run_renewal_reminder_pass",
]
