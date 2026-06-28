"""Weekly shadow-signal review pass.

A growing family of shadow-mode detectors append `[SHADOW] <code>: <detail>`
entries to ``DocumentRow.all_flags`` during parse:

* ``unreconciled_internal_transfer_v2`` — hidden-account transfers
  (parser/patterns.py)
* ``ai_generated_statement`` — composite forensic detector
  (parser/forensic/ai_statement.py)
* ``bank_statement_tampering_confirmed`` — tampering verdict in shadow mode
  (parser/forensic/tampering.py)
* + any future shadow detector that follows the same emit convention

Shadow-mode discipline (CLAUDE.md "Decision-boundary changes — shadow-first")
ships these as evidence-only; ``parse_status`` and ``fraud_score`` are
unaffected. The operator validates false-positive rate against a corpus of
live shadow audit rows before promoting any detector to live.

This module owns the weekly aggregation pass that surfaces those fires:

1. The cron entrypoint (``aegis.workers.run_shadow_review_cron``) runs
   each Wednesday at 06:00 UTC — distinct from the Monday 06:00 Track A
   sentinel and Monday 07:00 compliance cron so the operator's morning
   queue doesn't get triple-stacked.
2. ``run_shadow_review_pass`` walks every document parsed in the last 7
   days, parses ``all_flags`` for ``[SHADOW] *`` entries, writes one
   ``shadow_signal.weekly_summary`` audit row per (document, flag_code)
   tuple, then writes one ``shadow_signal.weekly_summary_complete``
   summary row carrying per-flag counts and ``source_document_ids`` so
   the dossier drill-down contract holds (CLAUDE.md auditability rule
   "every aggregate metric stores its source transaction IDs").
3. Idempotency: each per-fire audit row is keyed on
   ``(document_id, flag_code, window_start)``. A re-run inside the same
   window skips fires that already have a matching audit row. The
   weekly cadence keeps the dedupe lookup bounded.
4. ``build_shadow_review_attention_section`` powers the Today dashboard
   "Shadow signals this week" card and returns the
   (count, source_document_ids, cards) triple the dashboard router
   expects (mirrors ``build_compliance_attention_section``).

Distinct from ``aegis.merchants.shadow_signals`` (cross-merchant
``merchants_shadow_signals`` table — related-account / duplicate-PDF
signals at merchant scope). This module operates on per-document
``[SHADOW]`` text-flag emissions instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from aegis.audit import AuditLog
from aegis.logger import get_logger
from aegis.merchants.repository import MerchantNotFoundError

if TYPE_CHECKING:
    from aegis.merchants.repository import MerchantRepository
    from aegis.storage import DocumentRepository, DocumentRow

_log = get_logger(__name__)

SHADOW_FLAG_PREFIX: Final[str] = "[SHADOW] "
DEFAULT_WINDOW_DAYS: Final[int] = 7
TODAY_CARD_MAX_ITEMS: Final[int] = 5
# Pull cap when scanning the recent-document window. At ~100 deals/month
# = ~25/week the 500 ceiling has a 20x safety margin; if volume ever
# spikes past 500/week the cron should grow a dedicated ``parsed_since``
# query method on ``DocumentRepository`` (see TODO below).
_DOCUMENT_SCAN_LIMIT: Final[int] = 500

_AUDIT_ACTION_PER_FIRE: Final[str] = "shadow_signal.weekly_summary"
_AUDIT_ACTION_SUMMARY: Final[str] = "shadow_signal.weekly_summary_complete"


@dataclass(frozen=True)
class ShadowFlagFire:
    """One parsed ``[SHADOW] code: detail`` emission tied to a document."""

    flag_code: str
    flag_detail: str
    document_id: UUID
    document_filename: str
    merchant_id: UUID
    merchant_name: str
    parsed_at: datetime


@dataclass(frozen=True)
class ShadowReviewCard:
    """One row on the ``/ui/shadow-review`` listing and Today card.

    ``contributing_codes`` holds every distinct flag code that fired on
    this document inside the window so the operator can see at a glance
    which detectors hit on the same statement.
    """

    document_id: UUID
    document_filename: str
    merchant_id: UUID
    merchant_name: str
    parsed_at: datetime
    contributing_codes: tuple[str, ...]
    href: str
    test_id: str


@dataclass(frozen=True)
class ShadowReviewSummary:
    """End-state of a single ``run_shadow_review_pass`` invocation.

    ``counts_by_code`` and ``source_document_ids_by_code`` travel as a
    pair so the aggregate satisfies the CLAUDE.md "every aggregate
    metric stores its source transaction IDs" rule — drill-down from a
    per-code count back to the contributing documents is preserved.
    """

    window_start: date
    window_end: date
    docs_scanned: int
    fires: tuple[ShadowFlagFire, ...]
    counts_by_code: dict[str, int]
    source_document_ids_by_code: dict[str, list[UUID]]
    audit_rows_written: int
    audit_rows_skipped_dup: int = field(default=0)


def parse_shadow_flag(flag: str) -> tuple[str, str] | None:
    """Extract ``(code, detail)`` from a ``[SHADOW] code: detail`` string.

    Returns ``None`` when the flag does not match the prefix or is empty
    after the prefix. Whitespace around code/detail is stripped. A flag
    with no detail body (``[SHADOW] code:``) returns ``(code, "")``.
    """
    if not flag.startswith(SHADOW_FLAG_PREFIX):
        return None
    body = flag[len(SHADOW_FLAG_PREFIX) :].strip()
    if not body:
        return None
    code, sep, detail = body.partition(":")
    code = code.strip()
    if not code:
        return None
    return code, (detail.strip() if sep else "")


def extract_fires_from_document(
    doc: DocumentRow,
    *,
    merchant_name: str,
    merchant_id: UUID,
) -> list[ShadowFlagFire]:
    """Walk ``doc.all_flags`` for ``[SHADOW]`` entries.

    Multiple entries with the same code on the same document return
    multiple fires (each carries a distinct detail string). The audit
    write path collapses (document, code) into one row via the dedupe
    key, so duplicate-code entries become one audit row but every
    detail variant is preserved in the per-row detail payload.
    """
    if doc.parsed_at is None:
        return []
    out: list[ShadowFlagFire] = []
    for flag in doc.all_flags:
        parsed = parse_shadow_flag(flag)
        if parsed is None:
            continue
        code, detail = parsed
        out.append(
            ShadowFlagFire(
                flag_code=code,
                flag_detail=detail,
                document_id=doc.id,
                document_filename=doc.original_filename or "",
                merchant_id=merchant_id,
                merchant_name=merchant_name,
                parsed_at=doc.parsed_at,
            )
        )
    return out


def collect_shadow_fires(
    *,
    docs: DocumentRepository,
    merchants: MerchantRepository,
    since: datetime,
    limit: int = _DOCUMENT_SCAN_LIMIT,
) -> tuple[list[ShadowFlagFire], int]:
    """Return (fires, docs_scanned).

    Walks the most-recent ``limit`` documents, keeps those with
    ``parsed_at >= since``, dereferences merchant names for the audit /
    UI payloads, and emits one ``ShadowFlagFire`` per ``[SHADOW]``
    entry found.

    Merchant-name dereference is best-effort — a missing merchant
    record (orphan document) renders as ``"(unknown merchant)"`` so the
    summary survives a stale FK while still surfacing the doc to the
    operator.
    """
    documents = docs.list_documents(limit=limit)
    fires: list[ShadowFlagFire] = []
    docs_scanned = 0
    merchant_name_cache: dict[UUID, str] = {}
    for doc in documents:
        if doc.parsed_at is None or doc.parsed_at < since:
            continue
        docs_scanned += 1
        merchant_id = doc.merchant_id
        if merchant_id is None:
            continue
        if merchant_id not in merchant_name_cache:
            try:
                row = merchants.get(merchant_id)
                merchant_name_cache[merchant_id] = (
                    row.business_name if row.business_name else "(unknown merchant)"
                )
            except MerchantNotFoundError:
                # Orphan FK — merchant was soft-deleted between parse and
                # the cron run. Surface the doc anyway with a placeholder
                # name; the operator can still drill into the document.
                merchant_name_cache[merchant_id] = "(unknown merchant)"
        fires.extend(
            extract_fires_from_document(
                doc,
                merchant_name=merchant_name_cache[merchant_id],
                merchant_id=merchant_id,
            )
        )
    return fires, docs_scanned


def _has_prior_per_fire_audit(
    audit: AuditLog,
    *,
    document_id: UUID,
    flag_code: str,
    window_start: date,
) -> bool:
    """Idempotency probe — True if a matching per-fire audit row exists.

    Bounded by ``subject_type='document'`` + ``subject_id=document_id``
    + the per-fire action, so the lookup stays small even on a
    long-lived audit_log. Matches the discipline of
    ``_has_prior_submission_reminder`` in workers.py.
    """
    rows = audit.list_for_subject(
        subject_type="document",
        subject_id=document_id,
        action=_AUDIT_ACTION_PER_FIRE,
        limit=50,
    )
    iso = window_start.isoformat()
    for r in rows:
        details = r.get("details") or {}
        if details.get("flag_code") == flag_code and details.get("window_start") == iso:
            return True
    return False


def run_shadow_review_pass(
    *,
    audit: AuditLog,
    docs: DocumentRepository,
    merchants: MerchantRepository,
    today: date | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> ShadowReviewSummary:
    """Run the weekly review pass; write audit rows; return summary.

    Audit emission:
      * One ``shadow_signal.weekly_summary`` row per (document, flag_code)
        — duplicate codes within a doc collapse to one row whose details
        include both flag_detail strings (joined). Skipped when the
        idempotency probe finds an existing row for the same window.
      * One ``shadow_signal.weekly_summary_complete`` row at the end,
        carrying per-code counts and ``source_document_ids_by_code`` so
        the dossier drill-down contract holds (CLAUDE.md auditability).

    Audit-write failures propagate — per CLAUDE.md, audit writes
    failing must FAIL the operation, never silently log-and-continue.
    """
    today = today or datetime.now(UTC).date()
    window_start = today - timedelta(days=window_days)
    since_dt = datetime.combine(window_start, datetime.min.time(), tzinfo=UTC)

    fires, docs_scanned = collect_shadow_fires(docs=docs, merchants=merchants, since=since_dt)

    # Collapse same-code-on-same-doc into a single per-fire audit row.
    grouped: dict[tuple[UUID, str], list[ShadowFlagFire]] = {}
    for fire in fires:
        grouped.setdefault((fire.document_id, fire.flag_code), []).append(fire)

    audit_rows_written = 0
    audit_rows_skipped_dup = 0
    for (document_id, flag_code), group in grouped.items():
        if _has_prior_per_fire_audit(
            audit,
            document_id=document_id,
            flag_code=flag_code,
            window_start=window_start,
        ):
            audit_rows_skipped_dup += 1
            continue
        primary = group[0]
        details: dict[str, Any] = {
            "flag_code": flag_code,
            "flag_detail": primary.flag_detail,
            "document_filename": primary.document_filename,
            "merchant_name": primary.merchant_name,
            "merchant_id": str(primary.merchant_id),
            "parsed_at": primary.parsed_at.isoformat(),
            "window_start": window_start.isoformat(),
            "window_end": today.isoformat(),
        }
        if len(group) > 1:
            details["additional_details"] = [g.flag_detail for g in group[1:]]
        audit.record(
            actor="cron.shadow_review",
            action=_AUDIT_ACTION_PER_FIRE,
            subject_type="document",
            subject_id=document_id,
            details=details,
        )
        audit_rows_written += 1

    counts_by_code: dict[str, int] = {}
    source_document_ids_by_code: dict[str, list[UUID]] = {}
    for (document_id, flag_code), _group in grouped.items():
        counts_by_code[flag_code] = counts_by_code.get(flag_code, 0) + 1
        source_document_ids_by_code.setdefault(flag_code, []).append(document_id)

    audit.record(
        actor="cron.shadow_review",
        action=_AUDIT_ACTION_SUMMARY,
        subject_type="shadow_review_window",
        subject_id=None,
        details={
            "window_start": window_start.isoformat(),
            "window_end": today.isoformat(),
            "docs_scanned": docs_scanned,
            "docs_with_shadow": len({fire.document_id for fire in fires}),
            "counts_by_code": counts_by_code,
            "source_document_ids_by_code": {
                code: [str(doc_id) for doc_id in ids]
                for code, ids in source_document_ids_by_code.items()
            },
            "audit_rows_written": audit_rows_written,
            "audit_rows_skipped_dup": audit_rows_skipped_dup,
        },
    )

    return ShadowReviewSummary(
        window_start=window_start,
        window_end=today,
        docs_scanned=docs_scanned,
        fires=tuple(fires),
        counts_by_code=counts_by_code,
        source_document_ids_by_code=source_document_ids_by_code,
        audit_rows_written=audit_rows_written,
        audit_rows_skipped_dup=audit_rows_skipped_dup,
    )


# 5-minute Redis cache for the dashboard attention section. Operator
# report 2026-06-28: shadow_review_attention_section was taking 2.3s on
# every dashboard render (walks the trailing 7 days of audit log per
# merchant). The result is stable enough to memoize for 5 minutes; the
# Wed 06:00 UTC ``run_shadow_review_cron`` does the authoritative weekly
# pass via ``run_shadow_review_pass`` which doesn't go through this
# cache.
_SHADOW_CACHE_KEY: Final[str] = "aegis:shadow_review:attention"
_SHADOW_CACHE_TTL_SECONDS: Final[int] = 300


def _get_redis_client() -> Any:  # noqa: ANN401 — redis.Redis comes back loosely typed
    """Best-effort Redis client builder. Returns None on any failure so
    the dashboard render falls through to a cold compute rather than
    failing the page."""
    try:
        import redis

        from aegis.config import get_settings

        return redis.Redis.from_url(get_settings().redis_url, socket_timeout=1.0)
    except Exception:
        return None


def build_shadow_review_attention_section(
    *,
    docs: DocumentRepository,
    merchants: MerchantRepository,
    today: date | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    max_cards: int = TODAY_CARD_MAX_ITEMS,
) -> tuple[int, list[UUID], list[ShadowReviewCard]]:
    """Return (count, source_document_ids, cards) for the Today dashboard.

    Mirrors ``build_compliance_attention_section`` shape so the
    dashboard router treats every attention section identically.

    ``count`` is the number of DISTINCT documents with at least one
    shadow fire in the window (NOT the count of fires). ``source_document_ids``
    is the union of those distinct document ids (satisfies the CLAUDE.md
    aggregate-with-source-ids rule). ``cards`` is the first ``max_cards``
    documents sorted most-recently-parsed first.

    Result is cached in Redis for 5 minutes (see ``_SHADOW_CACHE_KEY``).
    Cache misses fall through to the cold compute and re-populate the
    cache; cache failures (Redis down, network blip) also fall through
    to cold compute so the dashboard never 500s on a transient infra
    blip.
    """
    # Cache key folds in ``today``/``window_days``/``max_cards`` so a
    # caller with non-default args doesn't clobber another caller's
    # result. ``today=None`` (the live default) gets a stable midnight-
    # rounded key so two renders in the same minute share the cache.
    today_key = (today or datetime.now(UTC).date()).isoformat()
    cache_key = f"{_SHADOW_CACHE_KEY}:{today_key}:{window_days}:{max_cards}"

    redis_client = _get_redis_client()
    if redis_client is not None:
        try:
            cached = redis_client.get(cache_key)
            if cached:
                import json

                payload = json.loads(cached)
                cached_cards: list[ShadowReviewCard] = [
                    ShadowReviewCard(
                        document_id=UUID(c["document_id"]),
                        document_filename=c["document_filename"],
                        merchant_id=UUID(c["merchant_id"]),
                        merchant_name=c["merchant_name"],
                        parsed_at=datetime.fromisoformat(c["parsed_at"]),
                        contributing_codes=tuple(c["contributing_codes"]),
                        href=c["href"],
                        test_id=c["test_id"],
                    )
                    for c in payload["cards"]
                ]
                source_ids = [UUID(s) for s in payload["source_document_ids"]]
                return int(payload["count"]), source_ids, cached_cards
        except Exception as exc:
            # Cache read failure — fall through to cold compute. Don't
            # let a transient Redis blip 500 the dashboard.
            _log.warning("shadow_review.cache_read_failed err=%s", exc)

    today = today or datetime.now(UTC).date()
    window_start = today - timedelta(days=window_days)
    since_dt = datetime.combine(window_start, datetime.min.time(), tzinfo=UTC)

    fires, _docs_scanned = collect_shadow_fires(docs=docs, merchants=merchants, since=since_dt)

    # Group fires by document. Preserve a stable per-doc record (first
    # fire's merchant + filename + parsed_at) and the set of distinct codes.
    by_doc: dict[UUID, tuple[ShadowFlagFire, set[str]]] = {}
    for fire in fires:
        existing = by_doc.get(fire.document_id)
        if existing is None:
            by_doc[fire.document_id] = (fire, {fire.flag_code})
        else:
            existing[1].add(fire.flag_code)

    # Sort most-recently-parsed first.
    ordered = sorted(by_doc.values(), key=lambda pair: pair[0].parsed_at, reverse=True)
    cards: list[ShadowReviewCard] = []
    for fire, codes in ordered[:max_cards]:
        cards.append(
            ShadowReviewCard(
                document_id=fire.document_id,
                document_filename=fire.document_filename,
                merchant_id=fire.merchant_id,
                merchant_name=fire.merchant_name,
                parsed_at=fire.parsed_at,
                contributing_codes=tuple(sorted(codes)),
                href="/ui/shadow-review",
                test_id="today-attn-shadow-review",
            )
        )

    source_document_ids = list(by_doc.keys())

    # Best-effort write-through. A cache write failure is non-fatal —
    # the next render takes the same 2.3s cold compute again.
    if redis_client is not None:
        try:
            import json

            payload = {
                "count": len(by_doc),
                "source_document_ids": [str(s) for s in source_document_ids],
                "cards": [
                    {
                        "document_id": str(c.document_id),
                        "document_filename": c.document_filename,
                        "merchant_id": str(c.merchant_id),
                        "merchant_name": c.merchant_name,
                        "parsed_at": c.parsed_at.isoformat(),
                        "contributing_codes": list(c.contributing_codes),
                        "href": c.href,
                        "test_id": c.test_id,
                    }
                    for c in cards
                ],
            }
            redis_client.set(cache_key, json.dumps(payload), ex=_SHADOW_CACHE_TTL_SECONDS)
        except Exception as exc:
            # Non-fatal: next render re-runs the cold compute. Log so
            # operators can spot a sustained cache outage.
            _log.warning("shadow_review.cache_write_failed err=%s", exc)

    return len(by_doc), source_document_ids, cards


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "SHADOW_FLAG_PREFIX",
    "ShadowFlagFire",
    "ShadowReviewCard",
    "ShadowReviewSummary",
    "build_shadow_review_attention_section",
    "collect_shadow_fires",
    "extract_fires_from_document",
    "parse_shadow_flag",
    "run_shadow_review_pass",
]
