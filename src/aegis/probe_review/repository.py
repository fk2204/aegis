"""ProbeReviewRepository — Protocol + in-memory + Supabase impls.

Owns the ``probe_review_verdicts`` table (migration 091). Two write
paths and three read paths:

  * ``add_verdict`` — write-or-noop one verdict. Idempotent on the
    schema's ``UNIQUE (document_id, probe_name, operator_email)`` —
    a second click from the same operator returns the existing row
    unchanged rather than raising.
  * ``count_verdicts(probe_name)`` — histogram of verdicts for one
    probe across all operators. Powers the admin "ready to flip"
    banner.
  * ``list_reviewed_document_ids(probe_name, operator_email)`` — the
    set of documents already adjudicated by ONE operator. Used to
    filter the unreviewed-disagreement listing per-operator.
  * ``list_all_verdicts(probe_name)`` — the full corpus for one probe,
    newest first. Used by the admin UI to surface the verdict log.

Write failures raise ``ProbeReviewWriteError`` so the calling route
can refuse to claim a successful persist — mirrors the
``AuditWriteError`` / ``BankLayoutWriteError`` semantics from
CLAUDE.md Auditability.

PII discipline
--------------
The schema carries ``operator_email`` (the CF Access SSO identity).
That email is the same one written to ``audit_log.actor_email`` for
every operator action across AEGIS — a known operator-identity field,
not merchant PII.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import UUID, uuid4

from aegis.db import get_supabase
from aegis.logger import get_logger
from aegis.probe_review.models import (
    SHADOW_FLAG_CODE,
    DisagreementRow,
    ProbeReviewVerdict,
    Verdict,
)

if TYPE_CHECKING:
    from aegis.merchants.repository import MerchantRepository
    from aegis.storage import DocumentRepository, DocumentRow

_log = get_logger(__name__)


class ProbeReviewWriteError(RuntimeError):
    """Raised when a probe_review_verdicts row could not be persisted."""


class ProbeReviewRepository(Protocol):
    """Read + write contract for the probe_review_verdicts table."""

    def add_verdict(
        self,
        *,
        document_id: UUID,
        probe_name: str,
        verdict: Verdict,
        operator_email: str,
    ) -> ProbeReviewVerdict:
        """Insert one verdict; return the persisted (or pre-existing) row.

        Idempotent on the ``UNIQUE (document_id, probe_name,
        operator_email)`` constraint — a duplicate write from the same
        operator returns the existing row instead of raising.
        """

    def count_verdicts(self, probe_name: str) -> dict[str, int]:
        """Return ``{'v2_correct': n, 'v1_correct': m}`` across all operators.

        Both keys are always present; absence in the underlying table
        renders as ``0``. The dict shape is fixed so the admin banner
        can index directly without ``.get(default=0)`` everywhere.
        """

    def list_reviewed_document_ids(self, *, probe_name: str, operator_email: str) -> set[UUID]:
        """Return the document_ids this operator has already verdicted.

        Used by the route handler to filter the unreviewed-disagreement
        listing — an operator who has already adjudicated a document
        does not see it on their next visit.
        """

    def list_all_verdicts(self, probe_name: str, *, limit: int = 500) -> list[ProbeReviewVerdict]:
        """Return all verdicts for one probe, newest first."""


class InMemoryProbeReviewRepository:
    """Dict-backed probe-review store. Tests + in-memory backend."""

    def __init__(self) -> None:
        # Indexed by (document_id, probe_name, operator_email) so the
        # UNIQUE-constraint semantics from the migration map directly to
        # a Python dict lookup. Iteration order matches insertion order
        # so the newest-first sort in ``list_all_verdicts`` is stable.
        self._rows: dict[tuple[UUID, str, str], ProbeReviewVerdict] = {}

    def add_verdict(
        self,
        *,
        document_id: UUID,
        probe_name: str,
        verdict: Verdict,
        operator_email: str,
    ) -> ProbeReviewVerdict:
        key = (document_id, probe_name, operator_email)
        existing = self._rows.get(key)
        if existing is not None:
            return existing
        row = ProbeReviewVerdict(
            id=uuid4(),
            document_id=document_id,
            probe_name=probe_name,
            operator_verdict=verdict,
            operator_email=operator_email,
            created_at=datetime.now(UTC),
        )
        self._rows[key] = row
        return row

    def count_verdicts(self, probe_name: str) -> dict[str, int]:
        counts: dict[str, int] = {"v2_correct": 0, "v1_correct": 0}
        for row in self._rows.values():
            if row.probe_name != probe_name:
                continue
            counts[row.operator_verdict] = counts.get(row.operator_verdict, 0) + 1
        return counts

    def list_reviewed_document_ids(self, *, probe_name: str, operator_email: str) -> set[UUID]:
        return {
            row.document_id
            for row in self._rows.values()
            if row.probe_name == probe_name and row.operator_email == operator_email
        }

    def list_all_verdicts(self, probe_name: str, *, limit: int = 500) -> list[ProbeReviewVerdict]:
        rows = [r for r in self._rows.values() if r.probe_name == probe_name]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[:limit]


class SupabaseProbeReviewRepository:
    """Persistence backed by Postgres ``probe_review_verdicts`` (mig 091)."""

    def add_verdict(
        self,
        *,
        document_id: UUID,
        probe_name: str,
        verdict: Verdict,
        operator_email: str,
    ) -> ProbeReviewVerdict:
        # Idempotency pre-query — the UNIQUE constraint at the schema
        # layer would raise on a duplicate insert. Returning the
        # existing row mirrors the bank_layouts repository's
        # ``upsert_success`` pattern and keeps the route handler from
        # having to catch + decode a Postgres unique-violation error.
        existing = self._find_existing(
            document_id=document_id,
            probe_name=probe_name,
            operator_email=operator_email,
        )
        if existing is not None:
            return existing

        payload: dict[str, Any] = {
            "document_id": str(document_id),
            "probe_name": probe_name,
            "operator_verdict": verdict,
            "operator_email": operator_email,
        }
        try:
            result = get_supabase().table("probe_review_verdicts").insert(payload).execute()
        except Exception as exc:
            _log.error(
                "probe_review.insert_failed probe_name=%s document_id=%s",
                probe_name,
                document_id,
            )
            raise ProbeReviewWriteError(
                f"failed to insert probe_review_verdicts row for {document_id}"
            ) from exc
        rows = cast(list[dict[str, Any]], result.data or [])
        if not rows:
            raise ProbeReviewWriteError("supabase insert returned no row for probe_review_verdicts")
        return _row_to_verdict(rows[0])

    def _find_existing(
        self,
        *,
        document_id: UUID,
        probe_name: str,
        operator_email: str,
    ) -> ProbeReviewVerdict | None:
        result = (
            get_supabase()
            .table("probe_review_verdicts")
            .select("*")
            .eq("document_id", str(document_id))
            .eq("probe_name", probe_name)
            .eq("operator_email", operator_email)
            .limit(1)
            .execute()
        )
        rows = cast(list[dict[str, Any]], result.data or [])
        if not rows:
            return None
        return _row_to_verdict(rows[0])

    def count_verdicts(self, probe_name: str) -> dict[str, int]:
        try:
            result = (
                get_supabase()
                .table("probe_review_verdicts")
                .select("operator_verdict")
                .eq("probe_name", probe_name)
                .limit(10_000)
                .execute()
            )
        except Exception:
            _log.warning("probe_review.count_failed probe_name=%s", probe_name)
            return {"v2_correct": 0, "v1_correct": 0}
        rows = cast(list[dict[str, Any]], result.data or [])
        counts: dict[str, int] = {"v2_correct": 0, "v1_correct": 0}
        for r in rows:
            v = str(r.get("operator_verdict") or "")
            if v in counts:
                counts[v] += 1
        return counts

    def list_reviewed_document_ids(self, *, probe_name: str, operator_email: str) -> set[UUID]:
        try:
            result = (
                get_supabase()
                .table("probe_review_verdicts")
                .select("document_id")
                .eq("probe_name", probe_name)
                .eq("operator_email", operator_email)
                .limit(10_000)
                .execute()
            )
        except Exception:
            _log.warning(
                "probe_review.list_reviewed_failed probe_name=%s",
                probe_name,
            )
            return set()
        out: set[UUID] = set()
        for r in cast(list[dict[str, Any]], result.data or []):
            doc_id_raw = r.get("document_id")
            if doc_id_raw is None:
                continue
            out.add(UUID(str(doc_id_raw)))
        return out

    def list_all_verdicts(self, probe_name: str, *, limit: int = 500) -> list[ProbeReviewVerdict]:
        try:
            result = (
                get_supabase()
                .table("probe_review_verdicts")
                .select("*")
                .eq("probe_name", probe_name)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
        except Exception:
            _log.warning("probe_review.list_all_failed probe_name=%s", probe_name)
            return []
        rows = cast(list[dict[str, Any]], result.data or [])
        return [_row_to_verdict(r) for r in rows]


# ---------------------------------------------------------------------------
# Disagreement collection — composes DocumentRepository + verdict reads
# ---------------------------------------------------------------------------


def _parse_flag_kv_tail(flag: str) -> dict[str, str]:
    """Parse the ``KEY=VAL KEY=VAL ...`` tail of a shadow flag.

    Returns the KV pairs as a plain dict. Tolerates a leading
    ``[SHADOW] code: `` prefix; everything after the first colon is
    treated as the KV body. Missing or malformed tails return ``{}``.
    """
    if ":" not in flag:
        return {}
    body = flag.split(":", 1)[1].strip()
    out: dict[str, str] = {}
    for part in body.split():
        if "=" not in part:
            continue
        key, _sep, val = part.partition("=")
        if key:
            out[key.strip()] = val.strip()
    return out


def _format_decision(route_vision: str | None) -> str:
    """Translate the ``v2_route_vision=True/False`` value to a UI label."""
    if route_vision is None:
        return "unknown"
    lowered = route_vision.strip().lower()
    if lowered in ("true", "1"):
        return "route to vision"
    if lowered in ("false", "0"):
        return "use text layer"
    return route_vision


def collect_unreviewed_disagreements(
    *,
    docs: DocumentRepository,
    merchants: MerchantRepository,
    repo: ProbeReviewRepository,
    probe_name: str,
    operator_email: str,
    scan_limit: int = 500,
) -> list[DisagreementRow]:
    """Walk the most-recent ``scan_limit`` documents, return unreviewed disagreements.

    "Unreviewed" = a document carries the shadow disagreement flag AND
    this operator has not yet recorded a verdict on it. Documents
    verdicted by OTHER operators are still shown — every operator sees
    every disagreement once, so the corpus accumulates independent
    judgments.

    Bank name comes from the ``analyses`` row (where the parser
    records the detected bank) with a fallback to the merchant's
    ``business_name`` and finally ``"(unknown bank)"`` for legacy
    rows where the FK is stale; the operator can still click through
    to the PDF in any of those cases.

    Page count is the int suffix of the ``page_count=<N>`` KV pair on
    the shadow flag — when the parser pipeline is updated to emit
    that key. Today the flag carries ``chars_avg`` and
    ``numeric_lines`` (no page_count); ``page_count=0`` here means
    "not exposed by the shadow flag yet" rather than "zero-page PDF".
    The full KV tail is preserved on ``flag_detail`` for completeness.
    """
    reviewed = repo.list_reviewed_document_ids(probe_name=probe_name, operator_email=operator_email)

    documents = docs.list_documents(limit=scan_limit)
    # Pre-filter to disagreement rows so we don't burn an analyses
    # batch lookup on every doc — only the ones we'll surface.
    disagreement_docs: list[tuple[DocumentRow, str]] = []
    for doc in documents:
        if doc.id in reviewed:
            continue
        flag = _find_disagreement_flag(doc.all_flags)
        if flag is None:
            continue
        disagreement_docs.append((doc, flag))

    if not disagreement_docs:
        return []

    analyses = docs.get_analyses_by_document_ids([doc.id for doc, _ in disagreement_docs])

    out: list[DisagreementRow] = []
    bank_name_cache: dict[UUID, str] = {}
    for doc, disagreement_flag in disagreement_docs:
        kv = _parse_flag_kv_tail(disagreement_flag)
        analysis = analyses.get(doc.id)
        bank_name = (
            analysis.bank_name
            if analysis is not None and analysis.bank_name
            else _resolve_bank_name(
                doc.merchant_id,
                merchants=merchants,
                cache=bank_name_cache,
            )
        )
        out.append(
            DisagreementRow(
                document_id=doc.id,
                bank_name=bank_name,
                page_count=_safe_int(kv.get("page_count")) or 0,
                v1_decision=_format_decision(kv.get("live_route_vision")),
                v2_decision=_format_decision(kv.get("v2_route_vision")),
                original_filename=doc.original_filename,
                parsed_at=doc.parsed_at,
                flag_detail=_strip_prefix(disagreement_flag),
            )
        )
    return out


def _find_disagreement_flag(flags: Iterable[str]) -> str | None:
    """Return the first ``[SHADOW] text_layer_probe_v2_disagrees`` flag.

    A document with multiple shadow flags (other probes) only emits one
    disagreement per probe per parse — the first match is the one the
    operator needs to see.
    """
    needle = f"[SHADOW] {SHADOW_FLAG_CODE}"
    for flag in flags:
        if flag.startswith(needle):
            return flag
    return None


def _strip_prefix(flag: str) -> str:
    """Return the KV tail of a shadow flag, without the leading code+colon."""
    if ":" not in flag:
        return flag
    return flag.split(":", 1)[1].strip()


def _safe_int(value: str | None) -> int | None:
    """Parse an int out of an optional string; return None on any failure."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _resolve_bank_name(
    merchant_id: UUID | None,
    *,
    merchants: MerchantRepository,
    cache: dict[UUID, str],
) -> str:
    """Look up the merchant's bank name for the disagreement listing.

    Orphan merchant FKs (soft-deleted between parse + review) resolve
    to ``"(unknown bank)"`` so the listing survives a stale reference.
    Merchants without a recorded bank also fall back to that label —
    the bank name is the operator's primary at-a-glance cue and an
    empty string would be misleading.
    """
    if merchant_id is None:
        return "(unknown bank)"
    if merchant_id in cache:
        return cache[merchant_id]
    try:
        row = merchants.get(merchant_id)
    except Exception:
        cache[merchant_id] = "(unknown bank)"
        return cache[merchant_id]
    name = getattr(row, "bank_name", None) or getattr(row, "business_name", None)
    label = str(name) if name else "(unknown bank)"
    cache[merchant_id] = label
    return label


# ---------------------------------------------------------------------------
# Row decoder
# ---------------------------------------------------------------------------


def _row_to_verdict(row: dict[str, Any]) -> ProbeReviewVerdict:
    """Decode a Postgres row to a ``ProbeReviewVerdict``.

    ``created_at`` round-trips as a string (Supabase REST default) or
    a ``datetime`` (when the driver decodes it); handle both shapes.
    """
    created_at_raw = row.get("created_at")
    if isinstance(created_at_raw, datetime):
        created_at: datetime = (
            created_at_raw
            if created_at_raw.tzinfo is not None
            else created_at_raw.replace(tzinfo=UTC)
        )
    elif isinstance(created_at_raw, str) and created_at_raw:
        parsed = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
        created_at = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    else:
        created_at = datetime.now(UTC)

    verdict_raw = str(row.get("operator_verdict") or "")
    if verdict_raw not in ("v2_correct", "v1_correct"):
        raise ProbeReviewWriteError(
            f"probe_review_verdicts row has unrecognised verdict: {verdict_raw!r}"
        )

    return ProbeReviewVerdict(
        id=UUID(str(row["id"])),
        document_id=UUID(str(row["document_id"])),
        probe_name=str(row.get("probe_name") or ""),
        operator_verdict=cast(Verdict, verdict_raw),
        operator_email=str(row.get("operator_email") or ""),
        created_at=created_at,
    )


__all__ = [
    "InMemoryProbeReviewRepository",
    "ProbeReviewRepository",
    "ProbeReviewWriteError",
    "SupabaseProbeReviewRepository",
    "collect_unreviewed_disagreements",
]
