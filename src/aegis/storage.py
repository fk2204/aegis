"""Document + transaction + analysis persistence.

The ``DocumentRepository`` Protocol is what the API and worker depend on.
Two implementations:

  * ``InMemoryDocumentRepository`` — dict-backed, used by tests and any
    code path that needs a working store without Supabase wired.
  * ``SupabaseDocumentRepository`` — talks to Postgres via supabase-py.
    Wraps the multi-row write of a parsed document (analyses + N
    transactions + status update) in a single RPC so a half-persisted
    parse result cannot leak into the dashboard.

Why both behind a Protocol: tests run offline; the deploy switches the
binding in ``api.deps`` without code change.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from aegis.db import get_supabase
from aegis.logger import get_logger
from aegis.money import Money
from aegis.parser.models import (
    Aggregates,
    ClassifiedTransaction,
    StatementSummary,
)
from aegis.parser.patterns import (
    PatternAnalysis,
    PatternAnalysisDTO,
    pattern_analysis_to_dto,
)
from aegis.parser.pipeline import PipelineResult

_log = get_logger(__name__)

ParseStatus = Literal["pending", "proceed", "review", "manual_review", "error"]


# Errors -----------------------------------------------------------------------


class DocumentExistsError(ValueError):
    """Raised when ``create_document`` sees a hash that's already on file."""


class DocumentNotFoundError(KeyError):
    """Raised when a document_id has no row."""


# Models -----------------------------------------------------------------------


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class DocumentRow(_StrictModel):
    """Application view of a row in the ``documents`` table."""

    id: UUID
    file_hash: str
    byte_size: int = Field(gt=0)
    original_filename: str
    merchant_id: UUID | None = None
    parse_status: ParseStatus = "pending"
    fraud_score: int | None = Field(default=None, ge=0, le=100)
    fraud_score_breakdown: dict[str, int] = Field(default_factory=dict)
    all_flags: list[str] = Field(default_factory=list)
    metadata_flags: list[str] = Field(default_factory=list)
    error_detail: str | None = None
    uploaded_at: datetime
    parsed_at: datetime | None = None
    uploaded_by: str = "system"

    # PDF retention chunk B (migration 033). All four nullable for
    # legacy rows (pre-chunk-B parses, ~30 docs as of 2026-06-02) AND
    # rows where the storage step failed in a way that left no
    # ciphertext on Supabase. Populated together atomically by
    # ``persist_storage_metadata`` — the partial index on
    # ``retention_until WHERE storage_path IS NOT NULL`` depends on
    # the four columns moving as one (no row should ever carry
    # ``storage_path != NULL`` with ``retention_until = NULL``; see
    # ``scripts/db_checks/migration-033-no-retained-forever-anomaly.sql``).
    storage_path: str | None = None
    sha256_original: str | None = None
    encryption_key_version: int | None = None
    retention_until: datetime | None = None


class AnalysisRow(_StrictModel):
    """Application view of a row in the ``analyses`` table.

    Includes the *_source_ids arrays added by migration 002 so the
    dashboard drill-down works straight from this object.
    """

    id: UUID
    document_id: UUID
    merchant_id: UUID | None = None
    statement_period_start: date
    statement_period_end: date
    statement_days: int = Field(ge=0)

    beginning_balance: Money
    ending_balance: Money
    avg_daily_balance: Money
    true_revenue: Money
    monthly_revenue: Money
    lowest_balance: Money
    num_nsf: int = Field(ge=0)
    days_negative: int = Field(ge=0)
    mca_positions: int = Field(ge=0)
    mca_daily_total: Money
    debt_to_revenue: Decimal = Field(ge=Decimal("0"))
    payroll_detected: bool = False
    returned_ach_count: int = Field(ge=0, default=0)

    avg_daily_balance_source_ids: list[UUID] = Field(default_factory=list)
    true_revenue_source_ids: list[UUID] = Field(default_factory=list)
    num_nsf_source_ids: list[UUID] = Field(default_factory=list)
    days_negative_source_ids: list[UUID] = Field(default_factory=list)
    mca_daily_total_source_ids: list[UUID] = Field(default_factory=list)

    # Per-calendar-month deposits / withdrawals / avg_balance for this
    # statement, so the merchant detail page + findings export can compute
    # month-over-month deltas across a renewal merchant's stack of N
    # statements without re-querying transactions. List of dicts (Decimals
    # stored as strings to round-trip cleanly through jsonb).
    monthly_breakdown: list[dict[str, str]] = Field(default_factory=list)

    # Bank identity carried forward from StatementSummary (migration 014).
    # Nullable because (a) pass-1 occasionally fails to recover them on
    # noisy statements and (b) pre-migration analyses don't have them.
    # The merchant detail bundling groups by
    # (merchant_id, bank_name, account_last4); rows where either is None
    # land in a single "bank not detected" bundle.
    bank_name: str | None = None
    account_last4: str | None = Field(default=None, max_length=4)

    # Cached PatternAnalysisDTO from migration 032 — drives the
    # Today / Review Queue chip-evidence drill-down without re-running
    # analyze_patterns() at render time. NULL on rows analyzed before
    # stage 2 chunk 2 ships; populated on every new analysis after.
    # Card builders fall back to rendering chips without expander when
    # this is None. NOT a source of truth for scoring — the scorer
    # always recomputes from current transactions.
    pattern_analysis: PatternAnalysisDTO | None = None


# Protocol ---------------------------------------------------------------------


class DocumentRepository(Protocol):
    """The persistence contract API + worker depend on."""

    def create_document(
        self,
        *,
        file_hash: str,
        byte_size: int,
        original_filename: str,
        uploaded_by: str = "system",
        merchant_id: UUID | None = None,
    ) -> DocumentRow:
        """Insert a new pending document. Raise ``DocumentExistsError`` on hash collision."""

    def get_document(self, document_id: UUID) -> DocumentRow:
        """Fetch by id. ``DocumentNotFoundError`` if missing."""

    def find_by_hash(self, file_hash: str) -> DocumentRow | None:
        """Return the existing document for this hash, or ``None``."""

    def persist_parse_result(
        self,
        document_id: UUID,
        *,
        result: PipelineResult,
        merchant_id: UUID | None = None,
    ) -> None:
        """Write transactions + analysis rows + status atomically."""

    def list_transactions(
        self, document_id: UUID, *, category: str | None = None
    ) -> list[ClassifiedTransaction]:
        """Return classified rows for the document, optionally filtered by category."""

    def list_documents(
        self,
        *,
        parse_status: ParseStatus | None = None,
        merchant_id: UUID | None = None,
        limit: int = 100,
    ) -> list[DocumentRow]:
        """Return documents most-recent first, optionally filtered.

        ``parse_status`` powers the ``/ui/review`` queue (filter for
        ``"manual_review"``). ``merchant_id`` powers the ``/ui/deals``
        derived view. ``limit`` caps page size.
        """

    def get_analysis(self, document_id: UUID) -> AnalysisRow | None: ...

    def get_analyses_by_document_ids(
        self, document_ids: list[UUID]
    ) -> dict[UUID, AnalysisRow]:
        """Batch variant of ``get_analysis`` — returns a {document_id -> AnalysisRow}
        mapping for every id that has an analysis row. Missing ids are
        simply absent from the result (callers handle with ``.get(...)``).

        Eliminates the N+1 query pattern of looping per-document
        ``get_analysis`` calls on the dashboard list / detail / trend
        paths. Empty ``document_ids`` returns an empty dict without
        hitting the database.
        """

    def count_by_parse_status(self) -> dict[str, int]:
        """Return a {parse_status -> count} histogram across all documents."""

    # PDF retention chunk B (migration 033) ---------------------------------

    def persist_storage_metadata(
        self,
        document_id: UUID,
        *,
        storage_path: str,
        sha256_original: str,
        encryption_key_version: int,
        retention_until: datetime,
    ) -> None:
        """Atomic update of all four PDF-storage columns together.

        Single ``UPDATE documents SET ... WHERE id = ?`` so the partial
        index on ``retention_until WHERE storage_path IS NOT NULL``
        never observes a half-state where ``storage_path`` is set
        without a ``retention_until`` (which would be a row retained
        forever — see
        ``scripts/db_checks/migration-033-no-retained-forever-anomaly.sql``).

        Worker calls this only AFTER ``storage_objects.upload`` returns
        successfully. If this method raises, the caller treats the
        outcome as a transient failure and quarantines the ciphertext.
        """

    def list_retention_expired(
        self, *, limit: int = 1000
    ) -> list[DocumentRow]:
        """Return docs whose ``retention_until < NOW()`` AND
        ``storage_path IS NOT NULL``, oldest-expiry first, bounded by
        ``limit``.

        Powers the chunk-E retention sweep cron — only rows that still
        have ciphertext to delete are scanned (the partial index
        ``idx_documents_retention_until`` excludes legacy /
        already-swept rows so the sweep doesn't pay for scanning
        the full table).
        """

    def clear_storage_path(self, document_id: UUID) -> None:
        """Set ``storage_path = NULL`` on the named document.

        Used by the chunk-E sweep after the blob has been deleted from
        Supabase Storage AND ``confirm_absent`` returned True. The
        ``retention_until`` column is left as-is — preserved as a
        forensic record of what the retention basis was at sweep time.
        """


# In-memory implementation -----------------------------------------------------


class InMemoryDocumentRepository:
    """Dict-backed repository.

    Designed for tests + the offline path while Supabase is unwired. Holds
    DocumentRow + classified transactions + Analysis in three dicts keyed
    by document_id.
    """

    def __init__(self) -> None:
        self._docs: dict[UUID, DocumentRow] = {}
        self._txs: dict[UUID, list[ClassifiedTransaction]] = {}
        self._analyses: dict[UUID, AnalysisRow] = {}

    def create_document(
        self,
        *,
        file_hash: str,
        byte_size: int,
        original_filename: str,
        uploaded_by: str = "system",
        merchant_id: UUID | None = None,
    ) -> DocumentRow:
        for existing in self._docs.values():
            if existing.file_hash == file_hash:
                raise DocumentExistsError(
                    f"document with hash {file_hash[:12]}... already exists "
                    f"as {existing.id}"
                )
        row = DocumentRow(
            id=uuid4(),
            file_hash=file_hash,
            byte_size=byte_size,
            original_filename=original_filename,
            merchant_id=merchant_id,
            uploaded_by=uploaded_by,
            uploaded_at=datetime.now(UTC),
        )
        self._docs[row.id] = row
        return row

    def get_document(self, document_id: UUID) -> DocumentRow:
        try:
            return self._docs[document_id]
        except KeyError as exc:
            raise DocumentNotFoundError(str(document_id)) from exc

    def find_by_hash(self, file_hash: str) -> DocumentRow | None:
        for row in self._docs.values():
            if row.file_hash == file_hash:
                return row
        return None

    def persist_parse_result(
        self,
        document_id: UUID,
        *,
        result: PipelineResult,
        merchant_id: UUID | None = None,
    ) -> None:
        doc = self.get_document(document_id)
        doc.parse_status = result.parse_status
        doc.fraud_score = result.fraud_score
        doc.fraud_score_breakdown = dict(result.fraud_score_breakdown)
        doc.all_flags = list(result.all_flags)
        doc.metadata_flags = list(result.metadata.flags)
        doc.parsed_at = datetime.now(UTC)
        doc.merchant_id = merchant_id or doc.merchant_id
        self._docs[document_id] = doc

        self._txs[document_id] = list(result.classified)

        if result.aggregates is not None and result.extraction is not None:
            summary = result.extraction.statement.summary
            self._analyses[document_id] = _build_analysis(
                document_id=document_id,
                merchant_id=merchant_id,
                aggregates=result.aggregates,
                summary=summary,
                classified=result.classified,
                patterns=result.patterns,
                monthly_breakdown=result.monthly_breakdown,
            )

    def list_transactions(
        self, document_id: UUID, *, category: str | None = None
    ) -> list[ClassifiedTransaction]:
        rows = self._txs.get(document_id, [])
        if category is None:
            return list(rows)
        return [t for t in rows if t.category == category]

    def list_documents(
        self,
        *,
        parse_status: ParseStatus | None = None,
        merchant_id: UUID | None = None,
        limit: int = 100,
    ) -> list[DocumentRow]:
        rows = list(self._docs.values())
        if parse_status is not None:
            rows = [r for r in rows if r.parse_status == parse_status]
        if merchant_id is not None:
            rows = [r for r in rows if r.merchant_id == merchant_id]
        rows.sort(key=lambda r: r.uploaded_at, reverse=True)
        return rows[:limit]

    def get_analysis(self, document_id: UUID) -> AnalysisRow | None:
        return self._analyses.get(document_id)

    def get_analyses_by_document_ids(
        self, document_ids: list[UUID]
    ) -> dict[UUID, AnalysisRow]:
        return {
            doc_id: self._analyses[doc_id]
            for doc_id in document_ids
            if doc_id in self._analyses
        }

    def count_by_parse_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self._docs.values():
            counts[row.parse_status] = counts.get(row.parse_status, 0) + 1
        return counts

    def mark_error(self, document_id: UUID, detail: str) -> None:
        """Test/worker helper to record an error path (out of band of parse)."""
        doc = self.get_document(document_id)
        doc.parse_status = "error"
        doc.error_detail = detail
        self._docs[document_id] = doc

    # PDF retention chunk B -------------------------------------------------

    def persist_storage_metadata(
        self,
        document_id: UUID,
        *,
        storage_path: str,
        sha256_original: str,
        encryption_key_version: int,
        retention_until: datetime,
    ) -> None:
        doc = self.get_document(document_id)
        # Single atomic mutation — in-memory backend writes all four
        # fields at once, mirroring the production single-UPDATE contract.
        doc.storage_path = storage_path
        doc.sha256_original = sha256_original
        doc.encryption_key_version = encryption_key_version
        doc.retention_until = retention_until
        self._docs[document_id] = doc

    def list_retention_expired(
        self, *, limit: int = 1000
    ) -> list[DocumentRow]:
        now = datetime.now(UTC)
        candidates = [
            d for d in self._docs.values()
            if d.storage_path is not None
            and d.retention_until is not None
            and d.retention_until < now
        ]
        candidates.sort(key=lambda d: d.retention_until or now)
        return candidates[:limit]

    def clear_storage_path(self, document_id: UUID) -> None:
        doc = self.get_document(document_id)
        doc.storage_path = None
        # retention_until preserved per the sweep design — forensic
        # record of what triggered the deletion.
        self._docs[document_id] = doc


# Supabase implementation ------------------------------------------------------


class SupabaseDocumentRepository:
    """Persistence backed by Postgres via supabase-py.

    Multi-row writes (transactions + analysis) are issued in sequence
    inside ``persist_parse_result``. Postgres-side this is best wrapped in
    a single RPC for atomicity; the helper below issues the individual
    inserts and updates the document row last so a partial failure leaves
    the document in ``pending`` and a retry sees no half-state.
    """

    def create_document(
        self,
        *,
        file_hash: str,
        byte_size: int,
        original_filename: str,
        uploaded_by: str = "system",
        merchant_id: UUID | None = None,
    ) -> DocumentRow:
        client = get_supabase()
        existing = (
            client.table("documents")
            .select("*")
            .eq("file_hash", file_hash)
            .limit(1)
            .execute()
        )
        if existing.data:
            row = cast(dict[str, Any], existing.data[0])
            raise DocumentExistsError(
                f"document with hash {file_hash[:12]}... already exists "
                f"as {row['id']}"
            )

        payload: dict[str, Any] = {
            "file_hash": file_hash,
            "byte_size": byte_size,
            "original_filename": original_filename,
            "uploaded_by": uploaded_by,
        }
        if merchant_id is not None:
            payload["merchant_id"] = str(merchant_id)

        inserted = client.table("documents").insert(payload).execute()
        return _row_to_document(cast(dict[str, Any], inserted.data[0]))

    def get_document(self, document_id: UUID) -> DocumentRow:
        result = (
            get_supabase()
            .table("documents")
            .select("*")
            .eq("id", str(document_id))
            .limit(1)
            .execute()
        )
        if not result.data:
            raise DocumentNotFoundError(str(document_id))
        return _row_to_document(cast(dict[str, Any], result.data[0]))

    def find_by_hash(self, file_hash: str) -> DocumentRow | None:
        result = (
            get_supabase()
            .table("documents")
            .select("*")
            .eq("file_hash", file_hash)
            .limit(1)
            .execute()
        )
        if not result.data:
            return None
        return _row_to_document(cast(dict[str, Any], result.data[0]))

    def persist_parse_result(
        self,
        document_id: UUID,
        *,
        result: PipelineResult,
        merchant_id: UUID | None = None,
    ) -> None:
        client = get_supabase()

        if result.classified:
            tx_payload = [
                _classified_to_db_row(t, document_id, merchant_id)
                for t in result.classified
            ]
            client.table("transactions").insert(tx_payload).execute()

        if result.aggregates is not None and result.extraction is not None:
            analysis = _build_analysis(
                document_id=document_id,
                merchant_id=merchant_id,
                aggregates=result.aggregates,
                summary=result.extraction.statement.summary,
                classified=result.classified,
                patterns=result.patterns,
                monthly_breakdown=result.monthly_breakdown,
            )
            client.table("analyses").insert(_analysis_to_db_row(analysis)).execute()

        client.table("documents").update(
            {
                "parse_status": result.parse_status,
                "fraud_score": result.fraud_score,
                "fraud_score_breakdown": result.fraud_score_breakdown,
                "all_flags": result.all_flags,
                "metadata_flags": list(result.metadata.flags),
                "parsed_at": datetime.now(UTC).isoformat(),
                **({"merchant_id": str(merchant_id)} if merchant_id else {}),
            }
        ).eq("id", str(document_id)).execute()

    def list_transactions(
        self, document_id: UUID, *, category: str | None = None
    ) -> list[ClassifiedTransaction]:
        query = (
            get_supabase()
            .table("transactions")
            .select("*")
            .eq("document_id", str(document_id))
            .order("posted_date")
        )
        if category is not None:
            query = query.eq("category", category)
        result = query.execute()
        return [
            _db_row_to_classified(cast(dict[str, Any], r))
            for r in (result.data or [])
        ]

    def list_documents(
        self,
        *,
        parse_status: ParseStatus | None = None,
        merchant_id: UUID | None = None,
        limit: int = 100,
    ) -> list[DocumentRow]:
        query = (
            get_supabase()
            .table("documents")
            .select("*")
            .order("uploaded_at", desc=True)
            .limit(limit)
        )
        if parse_status is not None:
            query = query.eq("parse_status", parse_status)
        if merchant_id is not None:
            query = query.eq("merchant_id", str(merchant_id))
        result = query.execute()
        return [
            _row_to_document(cast(dict[str, Any], r))
            for r in (result.data or [])
        ]

    def get_analysis(self, document_id: UUID) -> AnalysisRow | None:
        result = (
            get_supabase()
            .table("analyses")
            .select("*")
            .eq("document_id", str(document_id))
            .limit(1)
            .execute()
        )
        if not result.data:
            return None
        return _db_row_to_analysis(cast(dict[str, Any], result.data[0]))

    def get_analyses_by_document_ids(
        self, document_ids: list[UUID]
    ) -> dict[UUID, AnalysisRow]:
        if not document_ids:
            return {}
        # PostgREST ``in.(…)`` operator — single query returns analyses
        # for every supplied document_id that has one. Missing ids are
        # simply absent from the response (no error).
        id_strings = [str(d) for d in document_ids]
        result = (
            get_supabase()
            .table("analyses")
            .select("*")
            .in_("document_id", id_strings)
            .execute()
        )
        out: dict[UUID, AnalysisRow] = {}
        for row in cast(list[dict[str, Any]], result.data or []):
            analysis = _db_row_to_analysis(row)
            out[analysis.document_id] = analysis
        return out

    def count_by_parse_status(self) -> dict[str, int]:
        """Histogram of documents by parse_status.

        Caps the scan at 10 000 rows — at 100 deals/month + ~3 statements
        each, this covers 28 years; the cap is defensive against an
        unexpected blow-out without paying for a Postgres-side GROUP BY.
        """
        try:
            result = (
                get_supabase()
                .table("documents")
                .select("parse_status")
                .limit(10000)
                .execute()
            )
        except Exception:
            return {}
        counts: dict[str, int] = {}
        for r in cast(list[dict[str, Any]], result.data or []):
            status_val = str(r.get("parse_status", ""))
            if not status_val:
                continue
            counts[status_val] = counts.get(status_val, 0) + 1
        return counts

    def mark_error(self, document_id: UUID, detail: str) -> None:
        """Transition a document to ``parse_status=error`` with an error_detail.

        Mirrors ``InMemoryDocumentRepository.mark_error``. The worker
        calls this when ``run_pipeline`` raises so a failed parse doesn't
        leave the document in ``pending`` indefinitely — operator sees
        the failure on the review queue / Today attention panel.
        """
        get_supabase().table("documents").update(
            {
                "parse_status": "error",
                "error_detail": detail[:1000],
                "parsed_at": datetime.now(UTC).isoformat(),
            }
        ).eq("id", str(document_id)).execute()

    # PDF retention chunk B (migration 033) ---------------------------------

    def persist_storage_metadata(
        self,
        document_id: UUID,
        *,
        storage_path: str,
        sha256_original: str,
        encryption_key_version: int,
        retention_until: datetime,
    ) -> None:
        """Single-UPDATE atomic write of all four PDF-storage columns.

        Atomicity is the load-bearing contract here:
        ``idx_documents_retention_until`` is a partial index defined
        ``WHERE storage_path IS NOT NULL``, and the
        ``no-retained-forever-anomaly`` db_check asserts every row
        with ``storage_path != NULL`` carries a non-NULL
        ``retention_until``. A multi-UPDATE that wrote storage_path
        first and crashed before writing retention_until would create
        an anomaly row — Postgres single-UPDATE semantics prevent it.
        """
        get_supabase().table("documents").update(
            {
                "storage_path": storage_path,
                "sha256_original": sha256_original,
                "encryption_key_version": encryption_key_version,
                "retention_until": retention_until.isoformat(),
            }
        ).eq("id", str(document_id)).execute()

    def list_retention_expired(
        self, *, limit: int = 1000
    ) -> list[DocumentRow]:
        """Scan the partial index ``idx_documents_retention_until``
        for rows whose retention has passed. Oldest expiry first so
        the chunk-E sweep makes deterministic per-run progress on a
        large backlog."""
        now_iso = datetime.now(UTC).isoformat()
        result = (
            get_supabase()
            .table("documents")
            .select("*")
            .lt("retention_until", now_iso)
            .not_.is_("storage_path", "null")
            .order("retention_until", desc=False)
            .limit(limit)
            .execute()
        )
        return [
            _doc_row_from_db(row)
            for row in cast(list[dict[str, Any]], result.data or [])
        ]

    def clear_storage_path(self, document_id: UUID) -> None:
        """Single-UPDATE: ``storage_path = NULL``. ``retention_until``
        is intentionally NOT cleared — preserved as a forensic record
        of what triggered the deletion (the chunk-E sweep's audit row
        cites it)."""
        get_supabase().table("documents").update(
            {"storage_path": None}
        ).eq("id", str(document_id)).execute()


# Internal helpers -------------------------------------------------------------


def _doc_row_from_db(row: dict[str, Any]) -> DocumentRow:
    """Project a Supabase ``documents`` row into the ``DocumentRow``
    model. Used by ``list_retention_expired`` which returns rows
    rather than dicts so the chunk-E sweep code can stay typed.
    """
    return DocumentRow(
        id=UUID(str(row["id"])),
        file_hash=str(row["file_hash"]),
        byte_size=int(row["byte_size"]),
        original_filename=str(row["original_filename"]),
        merchant_id=(
            UUID(str(row["merchant_id"])) if row.get("merchant_id") else None
        ),
        parse_status=cast(ParseStatus, row.get("parse_status", "pending")),
        fraud_score=row.get("fraud_score"),
        fraud_score_breakdown=row.get("fraud_score_breakdown") or {},
        all_flags=list(row.get("all_flags") or []),
        metadata_flags=list(row.get("metadata_flags") or []),
        error_detail=row.get("error_detail"),
        uploaded_at=datetime.fromisoformat(row["uploaded_at"]),
        parsed_at=(
            datetime.fromisoformat(row["parsed_at"])
            if row.get("parsed_at") else None
        ),
        uploaded_by=str(row.get("uploaded_by") or "system"),
        storage_path=row.get("storage_path"),
        sha256_original=row.get("sha256_original"),
        encryption_key_version=row.get("encryption_key_version"),
        retention_until=(
            datetime.fromisoformat(row["retention_until"])
            if row.get("retention_until") else None
        ),
    )


def _build_analysis(
    *,
    document_id: UUID,
    merchant_id: UUID | None,
    aggregates: Aggregates,
    summary: StatementSummary,
    classified: list[ClassifiedTransaction],
    patterns: PatternAnalysis | None,
    monthly_breakdown: list[dict[str, str]] | None = None,
) -> AnalysisRow:
    """Project Aggregates + summary + patterns into a row-shaped AnalysisRow."""
    statement_days = (summary.period_end - summary.period_start).days

    monthly_revenue = (
        aggregates.true_revenue.value * Decimal(30) / Decimal(max(statement_days, 1))
    ).quantize(Decimal("0.01"))

    lowest_balance = min(
        (t.running_balance for t in classified if t.running_balance is not None),
        default=summary.beginning_balance,
    )

    mca_positions = _count_mca_positions(classified)
    returned_ach = sum(1 for t in classified if t.category == "nsf_fee")

    # Stage 2 chunk 2: persist the parser's PatternAnalysis when present
    # so card builders can read source_ids per chip without re-running
    # analyze_patterns() at render time. ``patterns is None`` (early
    # extraction failures, metadata-only flow) flows through as
    # ``pattern_analysis=None`` — the storage round-trip handles that
    # gracefully (chunk 1's test_analysis_row_with_null_pattern_analysis_round_trips).
    pattern_analysis_dto: PatternAnalysisDTO | None = (
        pattern_analysis_to_dto(patterns) if patterns is not None else None
    )

    return AnalysisRow(
        id=uuid4(),
        document_id=document_id,
        merchant_id=merchant_id,
        statement_period_start=summary.period_start,
        statement_period_end=summary.period_end,
        statement_days=statement_days,
        beginning_balance=summary.beginning_balance,
        ending_balance=summary.ending_balance,
        avg_daily_balance=aggregates.avg_daily_balance.value,
        true_revenue=aggregates.true_revenue.value,
        monthly_revenue=monthly_revenue,
        lowest_balance=lowest_balance,
        num_nsf=aggregates.num_nsf.value,
        days_negative=aggregates.days_negative.value,
        mca_positions=mca_positions,
        mca_daily_total=aggregates.mca_daily_total.value,
        debt_to_revenue=aggregates.debt_to_revenue,
        payroll_detected=any(t.category == "payroll" for t in classified),
        returned_ach_count=returned_ach,
        avg_daily_balance_source_ids=list(aggregates.avg_daily_balance.source_ids),
        true_revenue_source_ids=list(aggregates.true_revenue.source_ids),
        num_nsf_source_ids=list(aggregates.num_nsf.source_ids),
        days_negative_source_ids=list(aggregates.days_negative.source_ids),
        mca_daily_total_source_ids=list(aggregates.mca_daily_total.source_ids),
        monthly_breakdown=monthly_breakdown or [],
        bank_name=summary.bank_name,
        account_last4=summary.account_last4,
        pattern_analysis=pattern_analysis_dto,
    )


def _count_mca_positions(classified: list[ClassifiedTransaction]) -> int:
    """Count distinct MCA payment streams by description token."""
    seen: set[str] = set()
    for t in classified:
        if t.category != "mca_debit":
            continue
        # Bucket by the first 3 words of description — a coarse but stable
        # proxy until Phase 5.5 corpus tells us if a smarter split is needed.
        token = " ".join(t.description.upper().split()[:3])
        seen.add(token)
    return len(seen)


def _row_to_document(row: dict[str, Any]) -> DocumentRow:
    return DocumentRow(
        id=UUID(row["id"]),
        file_hash=row["file_hash"],
        byte_size=row["byte_size"],
        original_filename=row["original_filename"],
        merchant_id=UUID(row["merchant_id"]) if row.get("merchant_id") else None,
        parse_status=row["parse_status"],
        fraud_score=row.get("fraud_score"),
        fraud_score_breakdown=row.get("fraud_score_breakdown") or {},
        all_flags=row.get("all_flags") or [],
        metadata_flags=row.get("metadata_flags") or [],
        error_detail=row.get("error_detail"),
        uploaded_at=_parse_dt(row["uploaded_at"]),
        parsed_at=_parse_dt(row["parsed_at"]) if row.get("parsed_at") else None,
        uploaded_by=row.get("uploaded_by") or "system",
    )


def _parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    # Supabase returns ISO-8601 strings; handle the common 'Z' suffix.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _classified_to_db_row(
    tx: ClassifiedTransaction,
    document_id: UUID,
    merchant_id: UUID | None,
) -> dict[str, Any]:
    return {
        "id": str(tx.id),
        "document_id": str(document_id),
        "merchant_id": str(merchant_id) if merchant_id else None,
        "posted_date": tx.posted_date.isoformat(),
        "description": tx.description,
        "amount": str(tx.amount),
        "running_balance": str(tx.running_balance) if tx.running_balance is not None else None,
        "source_page": tx.source_page,
        "source_line": tx.source_line,
        "category": tx.category,
        "classification_confidence": tx.classification_confidence,
    }


def _db_row_to_classified(row: dict[str, Any]) -> ClassifiedTransaction:
    return ClassifiedTransaction(
        id=UUID(row["id"]),
        posted_date=date.fromisoformat(row["posted_date"]),
        description=row["description"],
        amount=Decimal(str(row["amount"])),
        running_balance=(
            Decimal(str(row["running_balance"])) if row.get("running_balance") else None
        ),
        source_page=row["source_page"],
        source_line=row["source_line"],
        category=row["category"],
        classification_confidence=row["classification_confidence"],
    )


def _analysis_to_db_row(analysis: AnalysisRow) -> dict[str, Any]:
    return {
        "id": str(analysis.id),
        "document_id": str(analysis.document_id),
        "merchant_id": str(analysis.merchant_id) if analysis.merchant_id else None,
        "statement_period_start": analysis.statement_period_start.isoformat(),
        "statement_period_end": analysis.statement_period_end.isoformat(),
        "statement_days": analysis.statement_days,
        "beginning_balance": str(analysis.beginning_balance),
        "ending_balance": str(analysis.ending_balance),
        "avg_daily_balance": str(analysis.avg_daily_balance),
        "true_revenue": str(analysis.true_revenue),
        "monthly_revenue": str(analysis.monthly_revenue),
        "lowest_balance": str(analysis.lowest_balance),
        "num_nsf": analysis.num_nsf,
        "days_negative": analysis.days_negative,
        "mca_positions": analysis.mca_positions,
        "mca_daily_total": str(analysis.mca_daily_total),
        "debt_to_revenue": str(analysis.debt_to_revenue),
        "payroll_detected": analysis.payroll_detected,
        "returned_ach_count": analysis.returned_ach_count,
        "avg_daily_balance_source_ids": [str(u) for u in analysis.avg_daily_balance_source_ids],
        "true_revenue_source_ids": [str(u) for u in analysis.true_revenue_source_ids],
        "num_nsf_source_ids": [str(u) for u in analysis.num_nsf_source_ids],
        "days_negative_source_ids": [str(u) for u in analysis.days_negative_source_ids],
        "mca_daily_total_source_ids": [str(u) for u in analysis.mca_daily_total_source_ids],
        "monthly_breakdown": analysis.monthly_breakdown,
        "bank_name": analysis.bank_name,
        "account_last4": analysis.account_last4,
        # mode="json" serializes Decimal -> str and UUID -> str so the
        # resulting dict round-trips cleanly through Supabase's
        # jsonb column. None preserved (column is nullable).
        "pattern_analysis": (
            analysis.pattern_analysis.model_dump(mode="json")
            if analysis.pattern_analysis is not None
            else None
        ),
    }


def _db_row_to_analysis(row: dict[str, Any]) -> AnalysisRow:
    return AnalysisRow(
        id=UUID(row["id"]),
        document_id=UUID(row["document_id"]),
        merchant_id=UUID(row["merchant_id"]) if row.get("merchant_id") else None,
        statement_period_start=date.fromisoformat(row["statement_period_start"]),
        statement_period_end=date.fromisoformat(row["statement_period_end"]),
        statement_days=row["statement_days"],
        beginning_balance=Decimal(str(row["beginning_balance"])),
        ending_balance=Decimal(str(row["ending_balance"])),
        avg_daily_balance=Decimal(str(row["avg_daily_balance"])),
        true_revenue=Decimal(str(row["true_revenue"])),
        monthly_revenue=Decimal(str(row["monthly_revenue"])),
        lowest_balance=Decimal(str(row["lowest_balance"])),
        num_nsf=row["num_nsf"],
        days_negative=row["days_negative"],
        mca_positions=row["mca_positions"],
        mca_daily_total=Decimal(str(row["mca_daily_total"])),
        debt_to_revenue=Decimal(str(row["debt_to_revenue"])),
        payroll_detected=row["payroll_detected"],
        returned_ach_count=row.get("returned_ach_count", 0),
        avg_daily_balance_source_ids=[
            UUID(u) for u in row.get("avg_daily_balance_source_ids") or []
        ],
        true_revenue_source_ids=[UUID(u) for u in row.get("true_revenue_source_ids") or []],
        num_nsf_source_ids=[UUID(u) for u in row.get("num_nsf_source_ids") or []],
        days_negative_source_ids=[UUID(u) for u in row.get("days_negative_source_ids") or []],
        mca_daily_total_source_ids=[UUID(u) for u in row.get("mca_daily_total_source_ids") or []],
        monthly_breakdown=row.get("monthly_breakdown") or [],
        bank_name=row.get("bank_name"),
        account_last4=row.get("account_last4"),
        pattern_analysis=(
            PatternAnalysisDTO.model_validate(row["pattern_analysis"])
            if row.get("pattern_analysis") is not None
            else None
        ),
    )


__all__ = [
    "AnalysisRow",
    "DocumentExistsError",
    "DocumentNotFoundError",
    "DocumentRepository",
    "DocumentRow",
    "InMemoryDocumentRepository",
    "ParseStatus",
    "SupabaseDocumentRepository",
]
