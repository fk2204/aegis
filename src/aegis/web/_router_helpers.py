"""Shared module-level helpers for the operator dashboard sub-routers.

Extracted from ``router.py`` during R4.1 so multiple sub-routers can
reference these without re-importing the 5k-line aggregator. Anything
that lives here is consumed by routes in MULTIPLE domain sub-routers
(or by a sub-router AND something still inside router.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException, Request, UploadFile

from aegis.audit import AuditLog
from aegis.compliance.states import StateNotServed, validate_state_served
from aegis.merchants.models import EntityType
from aegis.storage import DocumentRepository

_AGGREGATE_LABELS: dict[str, str] = {
    "true_revenue": "True Revenue",
    "avg_daily_balance": "Average Daily Balance",
    "num_nsf": "NSF Count",
    "days_negative": "Days Negative",
    "mca_daily_total": "MCA Daily Total",
}

# Per-aggregate unit hint shown under the KPI value (e.g. "$" amount,
# "days", "count"). Kept aligned with _AGGREGATE_LABELS — every key
# present in labels must have an entry here so the KPI tile can format.
_AGGREGATE_UNIT_KIND: dict[str, str] = {
    "true_revenue": "money",
    "avg_daily_balance": "money",
    "num_nsf": "count",
    "days_negative": "days",
    "mca_daily_total": "money",
}

_AGGREGATE_SOURCE_FIELDS: dict[str, str] = {
    "true_revenue": "true_revenue_source_ids",
    "avg_daily_balance": "avg_daily_balance_source_ids",
    "num_nsf": "num_nsf_source_ids",
    "days_negative": "days_negative_source_ids",
    "mca_daily_total": "mca_daily_total_source_ids",
}


@dataclass
class _UploadResult:
    """Per-file outcome surfaced to the operator on the upload form."""

    filename: str
    status: str  # "ok" | "duplicate" | "error"
    document_id: str | None
    detail: str  # human-readable summary or error message


async def _persist_uploads(
    *,
    request: Request,
    files: list[UploadFile],
    repository: DocumentRepository,
    audit: AuditLog,
    actor: str,
    actor_email: str | None = None,
    merchant_id: UUID | None,
    per_file_cap: int,
    total_cap: int,
) -> tuple[list[_UploadResult], str | None]:
    """Read N files, persist each via ``persist_pdf_upload``, return per-file
    outcomes plus an optional batch-level error.

    Per-file failures (oversize, non-PDF, dedup-race) become an entry in
    the result list with ``status="error"``; the batch keeps going so a
    bad file doesn't kill 3 good ones. A batch-level error (total cap
    exceeded) short-circuits and returns no results.
    """
    # Lazy import — see aegis.web.router module-top comment for the cycle
    # this avoids. Same rationale: aegis.api.routes.__init__ imports
    # aegis.web.router (so an eager import the other direction makes
    # router.py / sub-routers depend on aegis.api.routes finishing first).
    from aegis.api.routes.upload import (
        _make_request_enqueue,
        persist_pdf_upload,
    )

    bodies: list[tuple[str, bytes]] = []
    running_total = 0
    for f in files:
        body = await f.read(per_file_cap + 1)
        if len(body) > per_file_cap:
            return (
                [],
                f"{f.filename or 'unnamed'} exceeds the per-file cap of {per_file_cap} bytes",
            )
        running_total += len(body)
        if running_total > total_cap:
            return (
                [],
                f"total upload size exceeds the {total_cap}-byte batch cap",
            )
        bodies.append((f.filename or "unnamed.pdf", body))

    results: list[_UploadResult] = []
    for filename, body in bodies:
        try:
            resp = await persist_pdf_upload(
                enqueue_parse=_make_request_enqueue(request),
                body=body,
                original_filename=filename,
                repository=repository,
                audit=audit,
                actor=actor,
                actor_email=actor_email,
                merchant_id=merchant_id,
            )
        except HTTPException as exc:
            results.append(
                _UploadResult(
                    filename=filename,
                    status="error",
                    document_id=None,
                    detail=str(exc.detail),
                )
            )
            continue
        results.append(
            _UploadResult(
                filename=filename,
                status="duplicate" if resp.duplicate_of_existing else "ok",
                document_id=str(resp.document_id),
                detail=(
                    "deduped to existing document"
                    if resp.duplicate_of_existing
                    else f"queued (parse_status={resp.parse_status})"
                ),
            )
        )
    return results, None


def _validate_merchant_state(state: str) -> str | None:
    """Return an error string if the state isn't served, else None."""
    try:
        validate_state_served(state.upper())
    except StateNotServed as exc:
        return str(exc)
    return None


def _entity_type_or_none(value: str) -> EntityType | None:
    """Coerce a form-string to ``EntityType`` or ``None``.

    Strict-cast: anything outside the literal set returns ``None`` so a
    mistyped entity_type doesn't crash the intake flow. Callers pass
    user input directly from the form, where the ``<select>`` constrains
    valid values, but defense-in-depth is cheap here.
    """
    v = value.strip().lower()
    if v in {"llc", "corp", "sole_prop", "partnership", "other"}:
        return cast(EntityType, v)
    return None


_FORM_FIELDS: tuple[str, ...] = (
    "business_name",
    "owner_name",
    "state",
    "dba",
    "industry_naics",
    "credit_score",
    "time_in_business_months",
    "email",
    "phone",
    "entity_type",
    "ein",
    "requested_amount",
    "requested_factor",
    "requested_term_days",
    "broker_source",
    "intake_date",
    "is_renewal",
)


def _form_dict_from_locals(locs: dict[str, Any]) -> dict[str, str]:
    """Lift the named form fields out of a route's local namespace.

    Keeps the form re-render path strict: only the documented field names
    pass through, never auxiliary locals (request, repo, etc.) that would
    leak into the template context.
    """
    return {k: str(locs.get(k, "")) for k in _FORM_FIELDS}


def _decimal_or_none(value: str) -> Decimal | None:
    """Parse a form-string to Decimal; return None for empty/whitespace.

    Lifted to ``_router_helpers`` during R4.1 funders extraction — used by
    both the funders sub-router (criteria amounts) and the still-resident
    merchants routes (funder-response offered amount / factor).
    """
    s = value.strip()
    if not s:
        return None
    try:
        return Decimal(s)
    except Exception as exc:
        raise ValueError(f"invalid decimal: {value!r}") from exc


def _int_or_none(value: str) -> int | None:
    """Parse a form-string to int; return None for empty/whitespace.

    Lifted to ``_router_helpers`` alongside ``_decimal_or_none`` — same
    cross-sub-router consumer set.
    """
    s = value.strip()
    if not s:
        return None
    return int(s)


def _sha256_hex(payload: bytes) -> str:
    """Cheap content-addressable handle for an audit-log attachment row.

    Lifted to ``_router_helpers`` during R4.1 funders extraction — the
    funders re-extract route and the merchants submit-to-funders route
    both stamp SHA-256 hashes into audit details.
    """
    import hashlib

    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "_AGGREGATE_LABELS",
    "_AGGREGATE_SOURCE_FIELDS",
    "_AGGREGATE_UNIT_KIND",
    "_FORM_FIELDS",
    "_UploadResult",
    "_decimal_or_none",
    "_entity_type_or_none",
    "_form_dict_from_locals",
    "_int_or_none",
    "_persist_uploads",
    "_sha256_hex",
    "_validate_merchant_state",
]
