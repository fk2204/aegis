"""Admin sub-router — operator visibility for migrations + audit log (U32)
and the system-health dashboard (U35).

Routes:
  * ``GET /ui/admin/applied-migrations``   — rows from ``schema_migrations``
  * ``GET /ui/admin/audit-log``            — recent ``audit_log`` rows with
                                             ``?action=<prefix>&days=<N>&limit=<N>``
  * ``GET /ui/admin/health``               — service info + config flags +
                                             repository counts + recent
                                             ``*_failed`` / ``error_*`` audit rows
  * ``GET /ui/admin``                       — 302 → ``/ui/admin/health`` (the
                                              "is everything OK?" landing page)

Read-only operator surfaces. No write paths — re-applying migrations
happens via ``make migrate TARGET=prod`` (the path of record per
``deploy/RUNBOOK.md``); audit_log is append-only by design; the health
page only reads.

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

import platform
import socket
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final, Protocol, cast

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import aegis as _aegis_pkg
from aegis.api.deps import (
    get_audit,
    get_decision_snapshot,
    get_disclosure_render_event_repository,
    get_funder_repository,
    get_merchant_repository,
    get_merchant_shadow_signal_repository,
    get_repository,
    get_schema_migrations_reader,
    get_scoring_disagreement_repository,
    get_submission_repository,
)
from aegis.audit import AuditLog
from aegis.compliance.render_events import DisclosureRenderEventRepository
from aegis.compliance.snapshot import DecisionSnapshot, InMemoryDecisionSnapshot
from aegis.config import Settings, get_settings
from aegis.funders.repository import FunderRepository
from aegis.merchants.repository import MerchantRepository
from aegis.merchants.shadow_signals import MerchantShadowSignalRepository
from aegis.scoring_v2.shadow_disagreements import ScoringDisagreementRepository
from aegis.storage import DocumentRepository
from aegis.submissions import SubmissionRepository
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


# --- health page (U35) ------------------------------------------------------
#
# Operator-facing "is everything OK?" surface. Read-only. Four sections:
#
#   A. Service info        — version, git sha, python, hostname
#   B. Config flag state   — Settings whitelist; secrets NEVER render
#   C. Repository row counts — counts of merchants, funders, documents,
#                              decisions, submissions, scoring disagreements
#                              (open + total), shadow signals, migrations
#                              (disclosure_transmissions degrades to "—" in
#                              memory mode since it isn't a dep-injected repo)
#   D. Recent errors       — last 24h of ``*_failed`` / ``error_*`` audit
#                            rows grouped by action with counts
#
# Why a whitelist for §B (instead of "render every Settings field that
# doesn't end in _key / _token / _secret / _password")? Because new
# Settings fields land regularly; a deny-list would silently leak a
# future secret named without the standard suffix (e.g. a future
# ``aws_signing_material``). A whitelist makes the safe surface
# explicit. The PII canary test enforces no secret VALUE ever renders;
# the whitelist enforces only operator-tunable knobs are surfaced.

_HEALTH_ERROR_WINDOW_HOURS: Final[int] = 24
_HEALTH_ERROR_FETCH_CEILING: Final[int] = 2000

# Settings field names whose VALUE is safe to render on the health page.
# A non-whitelisted field never reaches the template, even if it has no
# ``_key`` / ``_token`` / ``_secret`` / ``_password`` suffix — fail
# closed. See module docstring for the deny-list-versus-whitelist
# rationale.
_HEALTH_SAFE_CONFIG_FIELDS: Final[tuple[str, ...]] = (
    "aegis_data_residency_confirmed",
    "aegis_storage_backend",
    "aegis_scoring_engine",
    "aegis_eof_threshold",
    "aegis_tampering_decline_mode",
    "aegis_parser_page_routing",
    "aegis_document_bucket",
    "aegis_max_upload_bytes",
    "aegis_max_intake_total_bytes",
    "aegis_worker_max_concurrent",
    "aegis_worker_job_timeout",
    "aws_region",
    "bedrock_model_id",
    "app_port",
    "log_level",
    "close_docs_in_pre_uw_status_id",
    "close_attachment_warn_threshold",
    "close_attachment_hard_cap",
    "pdf_encryption_keys_current",
)

# Substring patterns that disqualify a field from the safe-config list.
# Belt + suspenders alongside the whitelist: even if a future
# refactor adds a SecretStr field name to the whitelist by mistake,
# this filter removes it before it reaches the template.
_HEALTH_SECRET_NAME_PATTERNS: Final[tuple[str, ...]] = (
    "_key",
    "_token",
    "_secret",
    "_password",
    "_credential",
    "service_role",
    "api_key",
    "webhook_secret",
)


@dataclass(frozen=True)
class _HealthConfigFlag:
    """One row in §B. ``ok`` is the traffic-light hint:

      * ``True``  → render green (healthy)
      * ``False`` → render red (problem)
      * ``None``  → render neutral (informational only)
    """

    name: str
    value: str
    ok: bool | None
    note: str | None


@dataclass(frozen=True)
class _HealthCountRow:
    """One row in §C. ``last_activity_at`` is None when the section has
    no timestamp available or the table is empty.
    """

    label: str
    count: int | None
    last_activity_at: datetime | None
    note: str | None


@dataclass(frozen=True)
class _HealthErrorGroup:
    """One row in §D. ``count`` is the per-action 24h frequency."""

    action: str
    count: int
    latest_at: datetime | None


def _git_short_sha() -> str | None:
    """Return ``git rev-parse --short HEAD`` or ``None`` on failure.

    Best-effort: missing git, missing .git directory, or a sandbox that
    blocks subprocess all return ``None`` rather than 500-ing the page.
    """
    # ``git`` resolved via PATH because AEGIS runs on the operator's
    # workstation + the Hetzner box; both have ``git`` on PATH by
    # operator standards. The argv is a fixed literal (no shell, no
    # user input), so S603/S607 do not apply.
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607 — fixed argv
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def _safe_config_name(name: str) -> bool:
    """True iff ``name`` is allowed to surface a VALUE on the health page.

    A name passes only if it's in the whitelist AND doesn't contain a
    secret-pattern substring. The substring check is a regression-guard
    against a future whitelist edit accidentally including a secret
    field.
    """
    if name not in _HEALTH_SAFE_CONFIG_FIELDS:
        return False
    lname = name.lower()
    return all(pat not in lname for pat in _HEALTH_SECRET_NAME_PATTERNS)


def _build_config_flags(settings: Settings) -> list[_HealthConfigFlag]:
    """§B — config flag rows with traffic-light hints.

    Each rendered field is checked through ``_safe_config_name`` so a
    secret value can never land in the rendered HTML even if a future
    refactor extends the whitelist by mistake.
    """
    flags: list[_HealthConfigFlag] = []

    for field_name in _HEALTH_SAFE_CONFIG_FIELDS:
        if not _safe_config_name(field_name):
            continue
        raw = getattr(settings, field_name, None)
        if raw is None:
            value_str = "—"
        else:
            value_str = str(raw)

        ok, note = _config_traffic_light(field_name, raw)
        flags.append(
            _HealthConfigFlag(
                name=field_name,
                value=value_str,
                ok=ok,
                note=note,
            )
        )
    return flags


def _config_traffic_light(
    name: str, value: object
) -> tuple[bool | None, str | None]:
    """Map (field_name, value) → (traffic-light ok flag, optional note).

    Rules:
      * residency confirmed False → red (refuses to boot in prod, but
        if the page is reachable with it False something is wrong)
      * storage_backend != "supabase" → red in prod-shaped deploy
        (we tolerate "memory" in tests but mark it informationally)
      * scoring_engine == "track_abc" → yellow (cutover state)
      * tampering_decline_mode == "live" → yellow (live decline path)
      * eof_threshold > 2 → yellow (above documented policy)
      * everything else informational (None)
    """
    if name == "aegis_data_residency_confirmed":
        return (bool(value), None if value else "Boot guard violation")
    if name == "aegis_storage_backend":
        if value == "supabase":
            return (True, None)
        return (None, "in-memory (tests / offline)")
    if name == "aegis_scoring_engine":
        if value == "legacy":
            return (True, None)
        return (None, "track_abc cutover — verify operator authorized")
    if name == "aegis_tampering_decline_mode":
        if value == "shadow":
            return (True, None)
        return (None, "live decline path — verify operator authorized")
    if name == "aegis_eof_threshold":
        if value is None:
            return (None, None)
        try:
            # ``value`` is typed ``object`` here because the caller passes
            # in the raw Settings attribute. Narrow via str(...) so any
            # SupportsInt-ish value parses uniformly.
            n = int(str(value))
        except (TypeError, ValueError):
            return (None, None)
        if n <= 2:
            return (True, None)
        return (False, "above documented policy (>2)")
    return (None, None)


def _coerce_datetime_from_any(value: object) -> datetime | None:
    """Best-effort coercion of mixed timestamp shapes used across the
    different repos (ISO strings, datetime, None).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        return _coerce_datetime(value)
    return None


def _repo_counts(
    *,
    merchants: MerchantRepository,
    funders: FunderRepository,
    documents: DocumentRepository,
    decisions: DecisionSnapshot,
    submissions: SubmissionRepository,
    disagreements: ScoringDisagreementRepository,
    render_events: DisclosureRenderEventRepository,
    shadow_signals: MerchantShadowSignalRepository,
    migrations: SchemaMigrationsReader,
) -> list[_HealthCountRow]:
    """§C — repository row counts + best-effort last-activity timestamps.

    Each section is wrapped in try/except so a single repo failure
    degrades to ``count=None`` rather than 500-ing the whole page. The
    "—" rendering in the template communicates "couldn't read" without
    misleading the operator into thinking the table is empty.
    """
    rows: list[_HealthCountRow] = []

    # merchants — count_total() is on both backends.
    rows.append(_count_merchants(merchants))
    # funders — list_active() drives both the count and (None) last activity.
    rows.append(_count_funders(funders))
    # documents — sum count_by_parse_status histogram.
    rows.append(_count_documents(documents))
    # decisions — InMemoryDecisionSnapshot.rows() in memory, supabase
    # count for prod.
    rows.append(_count_decisions(decisions))
    # submissions — list_in_window over a wide range. Bounded by repo.
    rows.append(_count_submissions(submissions))
    # scoring shadow disagreements — open + total counts in one row.
    rows.append(_count_disagreements(disagreements))
    # disclosure_transmissions — best-effort supabase count; "—" in
    # memory mode (the table isn't a dep-injected repo today).
    rows.append(_count_disclosure_transmissions())
    # merchants_shadow_signals — list_in_window over a wide range.
    rows.append(_count_shadow_signals(shadow_signals))
    # disclosure render events — list_in_window over a wide range.
    rows.append(_count_disclosure_render_events(render_events))
    # schema_migrations — list_applied() drives count + latest applied_at.
    rows.append(_count_migrations(migrations))

    return rows


def _count_merchants(repo: MerchantRepository) -> _HealthCountRow:
    try:
        n = repo.count_total()
    except Exception:
        return _HealthCountRow("merchants", None, None, "read failed")
    return _HealthCountRow("merchants", n, None, None)


def _count_funders(repo: FunderRepository) -> _HealthCountRow:
    try:
        active = repo.list_active()
    except Exception:
        return _HealthCountRow("funders (active)", None, None, "read failed")
    return _HealthCountRow("funders (active)", len(active), None, None)


def _count_documents(repo: DocumentRepository) -> _HealthCountRow:
    try:
        hist = repo.count_by_parse_status()
    except Exception:
        return _HealthCountRow("documents", None, None, "read failed")
    return _HealthCountRow("documents", sum(hist.values()), None, None)


def _count_decisions(repo: DecisionSnapshot) -> _HealthCountRow:
    # The DecisionSnapshot Protocol doesn't expose a read API; reach
    # into the InMemory impl for tests, and fall through to a direct
    # Supabase count for prod.
    if isinstance(repo, InMemoryDecisionSnapshot):
        rows_data = repo.rows()
        latest: datetime | None = None
        for r in rows_data:
            ts = _coerce_datetime_from_any(r.get("decided_at"))
            if ts is not None and (latest is None or ts > latest):
                latest = ts
        return _HealthCountRow("decisions", len(rows_data), latest, None)
    # Supabase path.
    try:
        from aegis.db import get_supabase

        result = (
            get_supabase()
            .table("decisions")
            .select("decided_at")
            .order("decided_at", desc=True)
            .limit(10000)
            .execute()
        )
        rows_data = cast(list[dict[str, Any]], result.data or [])
    except Exception:
        return _HealthCountRow("decisions", None, None, "read failed")
    latest = (
        _coerce_datetime_from_any(rows_data[0].get("decided_at"))
        if rows_data
        else None
    )
    return _HealthCountRow("decisions", len(rows_data), latest, None)


def _count_submissions(repo: SubmissionRepository) -> _HealthCountRow:
    # Use a wide date window so the count covers the full table. AEGIS
    # has ~100 deals/month so the 5000-row cap on the Supabase backend
    # is well above any realistic window.
    from datetime import date as _date

    try:
        rows_data = repo.list_in_window(
            from_date=_date(2000, 1, 1),
            to_date=_date(2100, 1, 1),
        )
    except Exception:
        return _HealthCountRow("submissions", None, None, "read failed")
    latest: datetime | None = None
    for r in rows_data:
        ts = getattr(r, "submitted_at", None)
        if isinstance(ts, datetime) and (latest is None or ts > latest):
            latest = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts
    return _HealthCountRow("submissions", len(rows_data), latest, None)


def _count_disagreements(
    repo: ScoringDisagreementRepository,
) -> _HealthCountRow:
    try:
        open_rows = repo.list_open()
        all_rows = repo.list_all()
    except Exception:
        return _HealthCountRow(
            "scoring_shadow_disagreements", None, None, "read failed"
        )
    n_open = len(open_rows)
    n_total = len(all_rows)
    return _HealthCountRow(
        "scoring_shadow_disagreements",
        n_total,
        None,
        f"{n_open} open of {n_total} total",
    )


def _count_disclosure_transmissions() -> _HealthCountRow:
    """Best-effort Supabase count. The table isn't behind a
    dep-injected repo today, so the memory backend / tests see ``—``;
    prod sees the live count.
    """
    settings = get_settings()
    if settings.aegis_storage_backend == "memory":
        return _HealthCountRow(
            "disclosure_transmissions", None, None, "not tracked in memory mode"
        )
    try:
        from aegis.db import get_supabase

        result = (
            get_supabase()
            .table("disclosure_transmissions")
            .select("sent_at")
            .order("sent_at", desc=True)
            .limit(10000)
            .execute()
        )
        rows_data = cast(list[dict[str, Any]], result.data or [])
    except Exception:
        return _HealthCountRow(
            "disclosure_transmissions", None, None, "read failed"
        )
    latest = (
        _coerce_datetime_from_any(rows_data[0].get("sent_at"))
        if rows_data
        else None
    )
    return _HealthCountRow(
        "disclosure_transmissions", len(rows_data), latest, None
    )


def _count_shadow_signals(
    repo: MerchantShadowSignalRepository,
) -> _HealthCountRow:
    from datetime import date as _date

    try:
        rows_data = repo.list_in_window(
            from_date=_date(2000, 1, 1),
            to_date=_date(2100, 1, 1),
            limit=10000,
        )
    except Exception:
        return _HealthCountRow(
            "merchants_shadow_signals", None, None, "read failed"
        )
    latest: datetime | None = None
    for r in rows_data:
        ts = getattr(r, "detected_at", None)
        if isinstance(ts, datetime) and (latest is None or ts > latest):
            latest = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts
    return _HealthCountRow(
        "merchants_shadow_signals", len(rows_data), latest, None
    )


def _count_disclosure_render_events(
    repo: DisclosureRenderEventRepository,
) -> _HealthCountRow:
    from datetime import date as _date

    try:
        rows_data = repo.list_in_window(
            from_date=_date(2000, 1, 1),
            to_date=_date(2100, 1, 1),
            limit=10000,
        )
    except Exception:
        return _HealthCountRow(
            "disclosure_render_events", None, None, "read failed"
        )
    latest: datetime | None = None
    for r in rows_data:
        ts = getattr(r, "rendered_at", None)
        if isinstance(ts, datetime) and (latest is None or ts > latest):
            latest = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts
    return _HealthCountRow(
        "disclosure_render_events", len(rows_data), latest, None
    )


def _count_migrations(repo: SchemaMigrationsReader) -> _HealthCountRow:
    try:
        rows_data = repo.list_applied()
    except Exception:
        return _HealthCountRow("schema_migrations", None, None, "read failed")
    latest = rows_data[0].applied_at if rows_data else None
    return _HealthCountRow("schema_migrations", len(rows_data), latest, None)


def _is_error_action(action: str) -> bool:
    """Match the operator's "show me anything that smells like a failure"
    intuition: anything ending in ``_failed`` OR starting with
    ``error_`` OR containing ``.fail`` (covers ``audit.write_failed``,
    ``error_quarantine``, and the ``foo.failure`` namespace).
    """
    if not action:
        return False
    lname = action.lower()
    return (
        lname.endswith("_failed")
        or lname.startswith("error_")
        or "_failed" in lname
        or ".fail" in lname
    )


def _build_error_groups(
    audit: AuditLog, *, window_hours: int
) -> list[_HealthErrorGroup]:
    """§D — group recent ``*_failed`` / ``error_*`` rows by action."""
    cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
    try:
        raw_rows = audit.list_recent(limit=_HEALTH_ERROR_FETCH_CEILING)
    except Exception:
        return []

    groups: dict[str, _HealthErrorGroup] = {}
    for r in raw_rows:
        action = str(r.get("action") or "")
        if not _is_error_action(action):
            continue
        ts = _coerce_datetime_from_any(r.get("created_at"))
        if ts is not None and ts < cutoff:
            continue

        existing = groups.get(action)
        if existing is None:
            groups[action] = _HealthErrorGroup(
                action=action, count=1, latest_at=ts
            )
        else:
            latest = existing.latest_at
            if ts is not None and (latest is None or ts > latest):
                latest = ts
            groups[action] = _HealthErrorGroup(
                action=action,
                count=existing.count + 1,
                latest_at=latest,
            )

    return sorted(groups.values(), key=lambda g: g.count, reverse=True)


@router.get("/admin", include_in_schema=False)
async def admin_index_redirect() -> RedirectResponse:
    """``/ui/admin`` → ``/ui/admin/health``. Operator landing page.

    The "is everything OK?" health view is the better default than a
    raw migrations table — migrations + audit log are reachable via
    the health-page header actions.
    """
    return RedirectResponse(url="/ui/admin/health", status_code=302)


@router.get("/admin/health", response_class=HTMLResponse)
async def admin_health_view(
    request: Request,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    funders: Annotated[FunderRepository, Depends(get_funder_repository)],
    documents: Annotated[DocumentRepository, Depends(get_repository)],
    decisions: Annotated[DecisionSnapshot, Depends(get_decision_snapshot)],
    submissions: Annotated[
        SubmissionRepository, Depends(get_submission_repository)
    ],
    disagreements: Annotated[
        ScoringDisagreementRepository,
        Depends(get_scoring_disagreement_repository),
    ],
    render_events: Annotated[
        DisclosureRenderEventRepository,
        Depends(get_disclosure_render_event_repository),
    ],
    shadow_signals: Annotated[
        MerchantShadowSignalRepository,
        Depends(get_merchant_shadow_signal_repository),
    ],
    migrations_reader: Annotated[
        SchemaMigrationsReader, Depends(get_schema_migrations_reader)
    ],
    audit: Annotated[AuditLog, Depends(get_audit)],
) -> HTMLResponse:
    """Operator-facing "is everything running?" status page.

    Read-only. Four sections (service info, config flags, repo counts,
    recent errors). No writes — re-applies happen via ``make migrate``
    and config flips happen via ``/etc/aegis/aegis.env``.
    """
    settings = get_settings()

    service_info = {
        "aegis_version": getattr(_aegis_pkg, "__version__", "unknown"),
        "git_short_sha": _git_short_sha() or "—",
        "python_version": platform.python_version(),
        "hostname": socket.gethostname(),
        "repo_url": "https://github.com/fk2204/aegis",
    }

    config_flags = _build_config_flags(settings)

    repo_counts = _repo_counts(
        merchants=merchants,
        funders=funders,
        documents=documents,
        decisions=decisions,
        submissions=submissions,
        disagreements=disagreements,
        render_events=render_events,
        shadow_signals=shadow_signals,
        migrations=migrations_reader,
    )

    error_groups = _build_error_groups(
        audit, window_hours=_HEALTH_ERROR_WINDOW_HOURS
    )

    return cast(
        "HTMLResponse",
        templates.TemplateResponse(
            request,
            "admin_health.html.j2",
            {
                "active": "Admin",
                "service_info": service_info,
                "config_flags": config_flags,
                "repo_counts": repo_counts,
                "error_groups": error_groups,
                "error_window_hours": _HEALTH_ERROR_WINDOW_HOURS,
                "now": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            },
        ),
    )


__all__ = [
    "InMemorySchemaMigrationsReader",
    "SchemaMigrationRow",
    "SchemaMigrationsReader",
    "SupabaseSchemaMigrationsReader",
    "router",
]
