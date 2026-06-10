"""Shared module-level helpers for the operator dashboard sub-routers.

Extracted from ``router.py`` during R4.1 so multiple sub-routers can
reference these without re-importing the 5k-line aggregator. Anything
that lives here is consumed by routes in MULTIPLE domain sub-routers
(or by a sub-router AND something still inside router.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException, Request, UploadFile

from aegis.audit import AuditLog
from aegis.compliance.states import StateNotServed, validate_state_served
from aegis.merchants.models import EntityType, MerchantRow
from aegis.scoring.models import ScoreInput
from aegis.storage import AnalysisRow, DocumentRepository, DocumentRow

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


# ---------------------------------------------------------------------------
# Bundling + score-input helpers (R4.1 finish-part-4 — merchants extraction).
# ---------------------------------------------------------------------------
#
# These were defined inside router.py alongside the merchant routes that
# consume them. Lifted here during the merchants split so:
#   * ``routers/merchants.py`` can consume them without back-importing
#     the aggregator (which would re-create the original cycle).
#   * ``routers/dashboard.py`` can drop its lazy ``from aegis.web.router
#     import _collect_analyzed_for_merchant`` import.
#   * ``aegis.api.routes.findings`` can drop its lazy
#     ``from aegis.web.router import _score_input_from_dashboard`` import.
#   * Tests already importing these from ``aegis.web.router`` keep working
#     via re-exports preserved in router.py.

# Hardcoded window: include the trailing 3 statement months in the
# multi-month score. Funder underwriting industry-norm is "last 3 bank
# statements" — more isn't more informative because business conditions
# change. Configurable later if a specific funder asks for 4 or 6.
_SCORE_WINDOW_MONTHS: int = 3


BundleKey = tuple[str | None, str | None]


# Trailing suffixes that distinguish one printed bank-name variant from
# another (e.g. "Bank of America" vs "Bank of America, N.A.") without
# representing a different institution. Stripped during bundle keying so
# the same account at the same bank groups under one bundle regardless
# of which suffix the statement happened to print this month.
_BANK_NAME_SUFFIXES = (
    ", n. a.",
    ", n.a.",
    " n.a.",
    ", national association",
)


def _normalize_bank_name(name: str | None) -> str | None:
    """Lowercase, strip, drop trailing institution-type suffixes.

    Used only for bundle keying — UI label rendering reads the raw,
    unnormalized ``analysis.bank_name`` so operators still see "Bank of
    America, N.A." in the picker.
    """
    if name is None:
        return None
    s = name.strip().lower()
    if not s:
        return None
    for suffix in _BANK_NAME_SUFFIXES:
        if s.endswith(suffix):
            s = s[: -len(suffix)].rstrip(", ")
            break
    return s.strip() or None


def _bundle_key(analysis: AnalysisRow) -> BundleKey:
    """The (normalized bank_name, account_last4) key used to group statements.

    Verified VU Development 2026-06: three docs printed
    "Bank of America, N.A." and one printed "Bank of America" with the
    same account_last4. Pre-normalization the singleton was dropped from
    the default bundle.
    """
    return (_normalize_bank_name(analysis.bank_name), analysis.account_last4)


def _bundle_keys_for_merchant(
    items: list[tuple[DocumentRow, AnalysisRow]],
) -> list[tuple[BundleKey, int]]:
    """Return distinct bundle keys with their statement counts, most-populated first.

    Ties broken by latest ``statement_period_end`` so the most-recent
    bundle wins when two accounts have equal statement counts.
    """
    counts: dict[BundleKey, int] = {}
    latest_end: dict[BundleKey, date] = {}
    for _, analysis in items:
        key = _bundle_key(analysis)
        counts[key] = counts.get(key, 0) + 1
        if (
            key not in latest_end
            or analysis.statement_period_end > latest_end[key]
        ):
            latest_end[key] = analysis.statement_period_end

    def _sort_key(item: tuple[BundleKey, int]) -> tuple[int, date]:
        key, count = item
        return (count, latest_end[key])

    return sorted(counts.items(), key=_sort_key, reverse=True)


def _select_default_bundle(
    items: list[tuple[DocumentRow, AnalysisRow]],
) -> BundleKey | None:
    """Pick the most-populated bundle, or ``None`` if no items.

    See ``_bundle_keys_for_merchant`` for the tiebreak rule.
    """
    keys = _bundle_keys_for_merchant(items)
    return keys[0][0] if keys else None


def _filter_to_bundle(
    items: list[tuple[DocumentRow, AnalysisRow]],
    bundle: BundleKey,
) -> list[tuple[DocumentRow, AnalysisRow]]:
    """Keep only items whose ``(bank_name, account_last4)`` matches ``bundle``.

    Normalizes the incoming ``bundle`` so callers can pass raw
    ``(bank_name, last4)`` tuples without first running them through
    ``_normalize_bank_name`` themselves.
    """
    bank, last4 = bundle
    normalized = (_normalize_bank_name(bank), last4)
    return [(d, a) for d, a in items if _bundle_key(a) == normalized]


def _bundle_to_query(bundle: BundleKey) -> str:
    """Encode a bundle key as the ``?bundle=`` query value (``bank|last4``)."""
    bank, last4 = bundle
    return f"{bank or ''}|{last4 or ''}"


def _parse_bundle_query(value: str | None) -> BundleKey | None:
    """Parse a ``?bundle=`` query value back into a bundle key.

    Returns ``None`` for missing / blank / malformed inputs (caller then
    picks the default bundle). Empty segments are mapped to ``None`` so
    a pre-migration ``(None, None)`` bundle is addressable as ``|``.
    """
    if not value:
        return None
    parts = value.split("|", 1)
    if len(parts) != 2:
        return None
    bank, last4 = parts
    # Normalize so an old bookmark with "Bank of America, N.A.|7719"
    # resolves to the same bundle as the new "bank of america|7719".
    return (_normalize_bank_name(bank or None), last4 or None)


def _build_bundle_summaries(
    bundle_options: list[tuple[BundleKey, int]],
    active: BundleKey | None,
) -> list[dict[str, Any]]:
    """Template-friendly view of the merchant's bundles.

    Each entry carries the bank/last4 labels, the statement count, the
    URL-safe query value, and whether the bundle is currently active.
    """
    out: list[dict[str, Any]] = []
    for key, count in bundle_options:
        bank, last4 = key
        out.append(
            {
                "bank_name": bank,
                "account_last4": last4,
                "count": count,
                "query": _bundle_to_query(key),
                "is_active": key == active,
            }
        )
    return out


def _collect_analyzed_for_merchant(
    docs: DocumentRepository,
    merchant_id: UUID,
    *,
    window: int = _SCORE_WINDOW_MONTHS,
    bundle: BundleKey | None = None,
) -> list[tuple[DocumentRow, AnalysisRow]]:
    """Return up to ``window`` most-recent analyzed docs for a merchant.

    "Analyzed" means the document has an analysis row — i.e. extraction +
    validation + classification + aggregation all completed. ``manual_review``
    status is OK if the analysis exists (classification-confidence floor
    breaches still produce a usable analysis; the operator's decision is
    informed by including them).

    Returned newest first so the caller can pick the latest doc as the
    "current state" anchor and use the remainder as historical context.

    Bundling
    --------
    A merchant with two bank accounts produces two bundles of statements.
    Scoring across mixed-account statements is wrong: revenue sums across
    accounts double-count cash that just moved between them. The default
    behavior here is therefore "pick the most-populated bundle" — pass
    ``bundle`` explicitly to override (operator switching bundles in the
    UI). Pre-migration analyses without ``bank_name``/``account_last4``
    all share the ``(None, None)`` bundle and behave identically to the
    pre-bundling implementation.
    """
    rows = docs.list_documents(merchant_id=merchant_id, limit=window * 4)
    analyzed: list[tuple[DocumentRow, AnalysisRow]] = []
    for d in rows:
        a = docs.get_analysis(d.id)
        if a is None:
            continue
        analyzed.append((d, a))

    if not analyzed:
        return []

    selected_bundle = bundle if bundle is not None else _select_default_bundle(analyzed)
    if selected_bundle is None:
        return []

    filtered = _filter_to_bundle(analyzed, selected_bundle)
    return filtered[:window]


def _project_monthly(period_revenue: Decimal, statement_days: int) -> Decimal:
    """Annualize a period revenue figure to a per-30-day projection.

    Used by ``_score_input_from_dashboard`` (single-statement panel) to
    fill ``monthly_revenue`` on the ``ScoreInput``. Multi-month scoring
    has its own projection (``aegis.scoring.multi_month._project_monthly``)
    that sums across statements before projecting; both implementations
    use the same Decimal precision contract.
    """
    if statement_days <= 0:
        return Decimal("0.00")
    return (period_revenue / Decimal(statement_days) * Decimal(30)).quantize(
        Decimal("0.01")
    )


def _score_input_from_dashboard(
    merchant: MerchantRow,
    document: DocumentRow,
    analysis: AnalysisRow,
) -> ScoreInput:
    """Build a ScoreInput for the matched-funders dashboard panel.

    The match panel needs the same shape ``score_deal`` consumes, but the
    dashboard's "deal" is a derived view (per audit F1) without the
    operator's requested-amount / requested-factor / requested-term-days
    inputs. Use sane defaults: midpoint of the funder typical ranges, 120-
    day term. Operator overrides via the bearer-token API path before any
    real submission ships.
    """
    monthly = _project_monthly(analysis.true_revenue, analysis.statement_days)
    # Same caller-gated contract as score_input_multi_month — see the
    # note in scoring/multi_month.py. Empty string is the documented
    # fallback for callers that lose track of the finalized check.
    return ScoreInput(
        merchant_id=merchant.id,
        business_name=merchant.business_name,
        owner_name=merchant.owner_name,
        state=(merchant.state or "").upper(),
        industry_naics=merchant.industry_naics,
        industry_risk_tier=merchant.industry_risk_tier,
        time_in_business_months=merchant.time_in_business_months,
        credit_score=merchant.credit_score,
        avg_daily_balance=analysis.avg_daily_balance,
        true_revenue=analysis.true_revenue,
        monthly_revenue=monthly,
        lowest_balance=analysis.lowest_balance,
        num_nsf=analysis.num_nsf,
        days_negative=analysis.days_negative,
        mca_positions=analysis.mca_positions,
        mca_daily_total=analysis.mca_daily_total,
        debt_to_revenue=analysis.debt_to_revenue,
        payroll_detected=analysis.payroll_detected,
        returned_ach_count=analysis.returned_ach_count,
        statement_period_start=analysis.statement_period_start,
        statement_period_end=analysis.statement_period_end,
        statement_days=analysis.statement_days,
        fraud_score=document.fraud_score or 0,
        eof_markers=1,
        validation_passed=document.parse_status != "manual_review",
        extraction_confidence=100,
        # Operator-input fields not stored in analysis yet: use placeholders
        # that the funder match doesn't gate on (50K/1.30/120d). Real
        # submissions go through the API where these come from a form.
        requested_amount=Decimal("50000.00"),
        requested_factor=Decimal("1.30"),
        requested_term_days=120,
    )


__all__ = [
    "_AGGREGATE_LABELS",
    "_AGGREGATE_SOURCE_FIELDS",
    "_AGGREGATE_UNIT_KIND",
    "_BANK_NAME_SUFFIXES",
    "_FORM_FIELDS",
    "_SCORE_WINDOW_MONTHS",
    "BundleKey",
    "_UploadResult",
    "_build_bundle_summaries",
    "_bundle_key",
    "_bundle_keys_for_merchant",
    "_bundle_to_query",
    "_collect_analyzed_for_merchant",
    "_decimal_or_none",
    "_entity_type_or_none",
    "_filter_to_bundle",
    "_form_dict_from_locals",
    "_int_or_none",
    "_normalize_bank_name",
    "_parse_bundle_query",
    "_persist_uploads",
    "_project_monthly",
    "_score_input_from_dashboard",
    "_select_default_bundle",
    "_sha256_hex",
    "_validate_merchant_state",
]
