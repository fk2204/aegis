"""Daily-cap budget guard for automated Bedrock calls.

Wraps a single check — ``check_bedrock_budget(feature)`` — that every
cron / worker-side caller invokes BEFORE doing any Bedrock work. The
check counts ``audit_log`` rows whose ``action='bedrock.usage'`` were
written today (UTC) and refuses to start a new call when the count
hits the configured ceiling.

Operator-triggered Bedrock calls (manual "Generate Deal Summary",
manual statement upload, ``/ui/funders/import``) MUST NOT call this —
the operator is in the loop and their click is the authorization. The
guard exists only to bound autonomous spend: funder syncs, narrator
backfill, background checks, etc.

Cost-tracking schema:
    ``audit_log.action = 'bedrock.usage'`` (one row per Bedrock call,
    written by ``aegis.ops.cost_tracking.CostTrackingBedrockClient``).
    See that module for the wider design — this file is just the
    counter + the budget gate.

The cap is operator-tunable via ``AEGIS_DAILY_BEDROCK_LIMIT`` env var
(default 200). Setting it to 0 disables the guard entirely (returns
True unconditionally); negative values are coerced to 0.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from postgrest.types import CountMethod

from aegis.db import get_supabase
from aegis.logger import get_logger

_log = get_logger(__name__)


_DEFAULT_DAILY_LIMIT = 200


def _daily_limit() -> int:
    """Read the per-day Bedrock-call ceiling from env.

    Done lazily (not as a module-level constant) so tests + the cron
    runtime can flip the env var between calls without re-importing
    the module. Negative / unparseable values fall back to 0
    (effectively disables the guard).
    """
    raw = os.environ.get("AEGIS_DAILY_BEDROCK_LIMIT")
    if raw is None:
        return _DEFAULT_DAILY_LIMIT
    try:
        value = int(raw)
    except ValueError:
        _log.warning("bedrock_budget.bad_limit_env value=%r — falling back to default", raw)
        return _DEFAULT_DAILY_LIMIT
    return max(0, value)


def check_bedrock_budget(feature: str) -> bool:
    """Return True if today's Bedrock call count is under the budget.

    ``feature`` is a short tag identifying the calling subsystem so
    cost can be attributed when over-budget skips are audited
    (``funder_sync``, ``narrator_backfill``, ``background_checks``,
    ``corpus_ingestion`` — keep it short, kebab/snake, ASCII).

    Side effect on over-budget: writes one ``bedrock.budget_exceeded``
    audit row tagged with the feature so the operator can see why a
    given cron ticked over an interval. Logging is the only output —
    the caller is responsible for the actual short-circuit.
    """
    limit = _daily_limit()
    if limit <= 0:
        # Operator disabled the guard explicitly. Allow every call;
        # don't audit (no decision was made).
        return True
    sb = get_supabase()
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        result = (
            sb.table("audit_log")
            .select("id", count=CountMethod.exact)
            .eq("action", "bedrock.usage")
            .gte("created_at", today_start.isoformat())
            .execute()
        )
    except Exception as exc:
        _log.warning(
            "bedrock_budget.count_query_failed feature=%s err=%s",
            feature,
            exc,
        )
        # Failing open is the safer default: a transient Supabase blip
        # must not silently freeze all background work. The downstream
        # call still writes its own bedrock.usage row, so cost stays
        # observable even when the gate misfires.
        return True
    used = result.count or 0
    if used < limit:
        return True
    _log.error(
        "bedrock_budget_exceeded feature=%s used=%d limit=%d",
        feature,
        used,
        limit,
    )
    try:
        sb.table("audit_log").insert(
            {
                "actor": "system:bedrock_budget",
                "action": "bedrock.budget_exceeded",
                "details": {
                    "feature": feature,
                    "used": used,
                    "limit": limit,
                },
            }
        ).execute()
    except Exception as exc:
        _log.warning(
            "bedrock_budget.audit_failed feature=%s err=%s",
            feature,
            exc,
        )
    return False


__all__ = ["check_bedrock_budget"]
