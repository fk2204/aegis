"""Admin sub-router — operator visibility for migrations + audit log (U32).

Routes:
  * ``GET /ui/admin/applied-migrations``   — rows from ``schema_migrations``
  * ``GET /ui/admin/audit-log``            — recent ``audit_log`` rows with
                                             ``?action=<prefix>&days=<N>&limit=<N>``

Read-only operator surfaces. No write paths — re-applying migrations
happens via ``make migrate TARGET=prod`` (the path of record per
``deploy/RUNBOOK.md``); audit_log is append-only by design.

PII posture
-----------
``schema_migrations`` carries filename + sha256 + applied_at + applied_by
— no PII by construction. ``audit_log`` ``details`` JSONB can carry
non-PII fields (deal_id, funder_id, action-specific scalars), but per
CLAUDE.md the logger masker has already run before the row hits the DB.
The template renders only the structural columns (actor, action,
subject_type, subject_id-suffix, created_at) and a compact ``details``
key list — never the raw values — so a transaction description that
leaked through the masker cannot surface on this page either.

schema_migrations access
------------------------
``schema_migrations`` is a public-schema table maintained by
``scripts/apply_migrations.py``. RLS is enabled (migration 030) but the
Supabase service-role key used by ``aegis.db.get_supabase()`` bypasses
RLS — no separate DSN needed for a web read. The reader is a tiny
Protocol + InMemory + Supabase pair so tests can pin a deterministic
row list via ``app.dependency_overrides``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final, Protocol, cast

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from aegis.api.deps import (
    get_audit,
    get_schema_migrations_reader,
)
from aegis.audit import AuditLog
from aegis.web._templates import templates

router = APIRouter()


# --- schema_migrations reader ----------------------------------------------
#
# Pulled from the durable ``schema_migrations`` table that
# ``scripts/apply_migrations.py`` writes after every successful apply.
# Read-only; the operator's mental model is "what's in prod right now,
# and when did it land." Mutations happen via ``make migrate``.

@dataclass(frozen=True)
class SchemaMigrationRow:
    """One ``schema_migrations`` row, structurally typed for the template.

    ``sha256`` is the full 64-char hex; the template truncates to the
    first 12 chars for at-a-glance scanning while keeping the full hash
    in the DOM for copy/paste diffing against the file-system migration.
    """

    filename: str
    sha256: str
    applied_at: datetime | None
    applied_by: str


class SchemaMigrationsReader(Protocol):
    """Read-only interface over ``schema_migrations``.

    Tests inject ``InMemorySchemaMigrationsReader`` with a fixed row
    list. Production wires ``SupabaseSchemaMigrationsReader`` via
    ``aegis.db.get_supabase()`` — service-role key bypasses RLS.
    """

    def list_applied(self) -> list[SchemaMigrationRow]:
        """Return every applied migration, newest first.

        Newest-first because the operator's first question is "did the
        last deploy's migration land?" — top-of-table answers it. The
        full table is fine: at one migration per release we're nowhere
        near a row count that would need pagination.
        """


class InMemorySchemaMigrationsReader:
    """List-backed reader — used by tests and the in-memory backend."""

    def __init__(self, rows: list[SchemaMigrationRow] | None = None) -> None:
        self.rows: list[SchemaMigrationRow] = list(rows or [])

    def list_applied(self) -> list[SchemaMigrationRow]:
        # Newest first — sort defensively so callers that prepend in
        # any order still see a consistent table.
        def _key(r: SchemaMigrationRow) -> datetime:
            return r.applied_at or datetime.min.replace(tzinfo=UTC)

        return sorted(self.rows, key=_key, reverse=True)


class SupabaseSchemaMigrationsReader:
    """Supabase-backed reader. Service-role key bypasses RLS."""

    def list_applied(self) -> list[SchemaMigrationRow]:
        from aegis.db import get_supabase
        from aegis.logger import get_logger

        try:
            result = (
                get_supabase()
                .table("schema_migrations")
                .select("filename,sha256,applied_at,applied_by")
                .order("applied_at", desc=True)
                .limit(500)
                .execute()
            )
        except Exception:
            # Treat outage as empty rather than 500-ing the page.
            # Operator still sees the page chrome + an empty-state hint.
            get_logger(__name__).warning("admin.schema_migrations.fetch_failed")
            return []
        rows = cast(list[dict[str, Any]], result.data or [])
        out: list[SchemaMigrationRow] = []
        for r in rows:
            applied_at = _coerce_datetime(r.get("applied_at"))
            out.append(
                SchemaMigrationRow(
                    filename=str(r.get("filename", "—")),
                    sha256=str(r.get("sha256", "")),
                    applied_at=applied_at,
                    applied_by=str(r.get("applied_by", "—")),
                )
            )
        return out


def _coerce_datetime(value: object) -> datetime | None:
    """Pull a tz-aware ``datetime`` out of a Supabase JSON timestamp."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


# --- audit-log surface ------------------------------------------------------

_AUDIT_LOG_DEFAULT_LIMIT: Final[int] = 100
_AUDIT_LOG_MAX_LIMIT: Final[int] = 500
_AUDIT_LOG_DEFAULT_DAYS: Final[int] = 30
_AUDIT_LOG_MAX_DAYS: Final[int] = 365

# A coarse fetch ceiling for the post-filter pass. The route reads up to
# this many rows via ``AuditLog.list_recent``, then filters by action
# prefix + window before truncating to ``limit``. 2k keeps the fetch
# bounded while leaving plenty of headroom for a narrow ``?action=``
# prefix across a 30-day window at AEGIS's ~100 deals/month cadence.
_AUDIT_LOG_FETCH_CEILING: Final[int] = 2000


@router.get("/admin/applied-migrations", response_class=HTMLResponse)
async def applied_migrations_view(
    request: Request,
    reader: Annotated[
        SchemaMigrationsReader, Depends(get_schema_migrations_reader)
    ],
) -> HTMLResponse:
    """List every row in ``schema_migrations``, newest first.

    Columns: filename, applied_at, applied_by, sha256 (12-char prefix).
    Banner copy explains the operator-only nature + points at the
    re-apply path (``make migrate TARGET=prod``).
    """
    rows = reader.list_applied()
    return cast(
        "HTMLResponse",
        templates.TemplateResponse(
            request,
            "admin_migrations.html.j2",
            {
                "active": "Admin",
                "rows": rows,
                "now": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            },
        ),
    )


@router.get("/admin/audit-log", response_class=HTMLResponse)
async def audit_log_view(
    request: Request,
    audit: Annotated[AuditLog, Depends(get_audit)],
    action: Annotated[
        str | None,
        Query(
            description=(
                "Optional action-prefix filter. Matches rows whose "
                "``action`` starts with the given string. Empty / None → "
                "no filter. Use to drill into one subsystem (e.g. "
                "``close.``, ``deal.``, ``aegis_disclosure_render_event``)."
            ),
        ),
    ] = None,
    days: Annotated[
        int | None,
        Query(
            description=(
                "Window length in days (today minus N). Default 30, "
                "max 365."
            ),
            ge=1,
            le=_AUDIT_LOG_MAX_DAYS,
        ),
    ] = None,
    limit: Annotated[
        int | None,
        Query(
            description=(
                "Maximum rows to render. Default 100, max 500."
            ),
            ge=1,
            le=_AUDIT_LOG_MAX_LIMIT,
        ),
    ] = None,
) -> HTMLResponse:
    """Recent ``audit_log`` rows, newest first.

    Filter posture:

      * ``?action=<prefix>`` narrows to rows whose action starts with
        the given string. Case-sensitive (the audit table is too).
      * ``?days=<N>`` narrows the window to today minus N days.
      * ``?limit=<N>`` caps the rendered row count.

    The route fetches up to ``_AUDIT_LOG_FETCH_CEILING`` rows via
    ``AuditLog.list_recent`` and filters in Python. At AEGIS's
    ~100 deals/month cadence, this is well under the ceiling for any
    realistic window; if the prod table grows past that, this route
    needs a dedicated ranged query (left as a TODO once row volume
    crosses the threshold).
    """
    effective_action = (action or "").strip() or None
    effective_days = days if days is not None else _AUDIT_LOG_DEFAULT_DAYS
    effective_limit = limit if limit is not None else _AUDIT_LOG_DEFAULT_LIMIT

    cutoff = datetime.now(UTC) - timedelta(days=effective_days)
    raw_rows = audit.list_recent(limit=_AUDIT_LOG_FETCH_CEILING)

    filtered: list[dict[str, Any]] = []
    for r in raw_rows:
        action_value = str(r.get("action") or "")
        if effective_action is not None and not action_value.startswith(
            effective_action
        ):
            continue
        created_at = _coerce_datetime(r.get("created_at"))
        if created_at is not None and created_at < cutoff:
            continue
        filtered.append(
            {
                "actor": str(r.get("actor") or "—"),
                "action": action_value or "—",
                "subject_type": r.get("subject_type"),
                "subject_id_suffix": _subject_id_suffix(r.get("subject_id")),
                "subject_id_full": _subject_id_string(r.get("subject_id")),
                "created_at": created_at,
                "details_keys": _details_keys(r.get("details")),
            }
        )
        if len(filtered) >= effective_limit:
            break

    return cast(
        "HTMLResponse",
        templates.TemplateResponse(
            request,
            "admin_audit_log.html.j2",
            {
                "active": "Admin",
                "rows": filtered,
                "filter_action": effective_action or "",
                "filter_days": effective_days,
                "filter_limit": effective_limit,
                "fetch_ceiling": _AUDIT_LOG_FETCH_CEILING,
                "now": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            },
        ),
    )


def _subject_id_suffix(value: object) -> str:
    """Return the last 8 chars of a UUID-ish subject_id, or ``—``.

    The full UUID lives in ``subject_id_full`` for copy/paste; this
    suffix is the at-a-glance disambiguator that fits in a column.
    """
    s = _subject_id_string(value)
    if not s:
        return "—"
    # Strip a trailing brace/quote that snuck through serialization
    # (defensive — supabase-py returns plain strings).
    s = s.strip()
    return s[-8:] if len(s) >= 8 else s


def _subject_id_string(value: object) -> str:
    """Coerce a subject_id field to a plain string. Empty for None."""
    if value is None:
        return ""
    return str(value)


def _details_keys(value: object) -> list[str]:
    """Return the sorted JSONB key list of an audit ``details`` payload.

    Renders as a compact comma-separated list so the operator sees the
    shape of the audit row without us exposing the values (which could
    carry counts, IDs, or other non-PII scalars). When the payload is
    not a dict (legacy rows or unmasked None), returns an empty list.
    """
    if not isinstance(value, dict):
        return []
    return sorted(str(k) for k in value)


__all__ = [
    "InMemorySchemaMigrationsReader",
    "SchemaMigrationRow",
    "SchemaMigrationsReader",
    "SupabaseSchemaMigrationsReader",
    "router",
]
