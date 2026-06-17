"""Recover the legacy-doc backlog by pulling original bank statements
from Close attachments and ingesting them through the full parse
pipeline + pdf_store seal.

Context (2026-06-16): every document at ``parse_status in
('manual_review', 'error')`` is pre-migration-060 — the encrypted
plaintext was never persisted into ``pdf_store``. ``reparse_corpus.py``
proved this when it bailed at ``PdfStoreNotFoundError`` on all 25 rows.
The original PDFs DO survive on the merchant's Close Lead as note /
email attachments. This script walks every merchant in that backlog,
asks Close for the PDF attachments on their Lead, and (in ``--apply``
mode) runs each one through the same fetch → SHA-dedup → ingest →
parse → ``pdf_store`` seal sequence that ``POST /uploads/from-close``
already implements at ``aegis/api/routes/upload.py``.

Per CLAUDE.md operating-principles §1 the script is DRY-RUN by default.
Dry-run still HITS Close (lists + downloads, costs network round-trips
+ a small Close-API quota slice) so the operator can preview which
attachments would be ingested without paying the Bedrock token cost.

Dedup posture (two modes — strict default, opt-in backfill):

  * Compute ``sha256(plaintext)`` on every Close PDF.
  * ``repository.find_by_hash(sha)`` → if a row exists:
      - **Default** (no ``--backfill-sha-matches``): skip the
        attachment. Matches the from-close route's posture exactly.
        Whatever state the existing row is in (manual_review / error
        / proceed / review) is NOT touched.
      - **With ``--backfill-sha-matches`` (alongside ``--apply``)**:
        check ``pdf_store.fetch_plaintext(existing.id)``. If sealed,
        skip (truly nothing to do). If NOT sealed (legacy pre-mig-060
        row), this is the recovery case: seal plaintext into
        ``pdf_store`` under the existing document_id, clear any prior
        transactions / analyses rows, and re-run the parse pipeline
        against the existing row. The doc UUID stays the same; the
        parse status, fraud_score, transactions, and analyses are
        rewritten by today's pipeline. Audited as
        ``document.pdf_store_backfilled``.
  * ``find_by_hash`` miss → **fresh ingest**: create a new
    ``documents`` row, write the PDF to ``aegis_upload_dir`` as a
    temp file, run ``aegis.parser.pipeline.run_pipeline``, persist via
    ``persist_parse_result``, seal into ``pdf_store``, delete the
    temp file.

Per-merchant CSV summary (``recover_legacy_docs.csv`` by default):

    merchant_id, merchant_name, close_lead_id,
    attachments_found, attachments_reingested, attachments_backfilled,
    attachments_skipped, parse_results, errors

Exit codes (mirror ``scripts/track_a_historical_lookback.py``):

  * ``0`` — every backlog merchant processed cleanly (zero errors).
  * ``1`` — runtime error (Supabase / Close / Bedrock / pdf_store
            init failed, settings missing, etc.).
  * ``3`` — at least one merchant failed (no close_lead_id, Close API
            error, parse exception, etc.). Operator triage required;
            the CSV's ``errors`` column carries the diagnostic.

Usage (on the prod box, with ``/etc/aegis/aegis.env`` sourced)::

    set -a; source /etc/aegis/aegis.env; set +a
    cd /opt/aegis

    # dry-run: list attachments per backlog merchant, print plan
    .venv/bin/python scripts/recover_legacy_docs.py

    # apply: ingest each new attachment end-to-end
    .venv/bin/python scripts/recover_legacy_docs.py --apply

    # apply + backfill: also seal+reparse SHA-matching pre-mig-060 docs
    .venv/bin/python scripts/recover_legacy_docs.py --apply --backfill-sha-matches

    # narrower scan
    .venv/bin/python scripts/recover_legacy_docs.py --limit 5

This script lives at ``scripts/`` (flat). ``--apply`` mode writes to
prod (documents, transactions, analyses, audit_log, pdf_store), but
the default invocation is the dry-run preview — same posture as
``reparse_corpus.py``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import sys
import tempfile
import traceback
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast
from uuid import UUID

from aegis.audit import AuditLog, SupabaseAuditLog
from aegis.bank_layouts.models import BankLayoutRow
from aegis.close.client import (
    CloseAttachment,
    CloseClient,
    CloseError,
)
from aegis.close.field_map import filename_is_non_statement
from aegis.config import get_settings
from aegis.db import get_supabase
from aegis.logger import get_logger
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import (
    MerchantNotFoundError,
    SupabaseMerchantRepository,
)
from aegis.parser.extract import ExtractionError
from aegis.parser.metadata import PdfEncryptedError
from aegis.parser.pipeline import PipelineResult, run_pipeline
from aegis.pdf_store.repository import (
    PdfStoreNotFoundError,
    SupabasePdfStoreRepository,
)
from aegis.storage import (
    DocumentExistsError,
    DocumentNotFoundError,
    DocumentRow,
    ParseStatus,
    SupabaseDocumentRepository,
)

# Exit codes — keep aligned with sibling scripts.
EXIT_OK: Final[int] = 0
EXIT_RUNTIME_ERROR: Final[int] = 1
EXIT_ISSUES_FOUND: Final[int] = 3

# Default output filename relative to cwd.
_DEFAULT_OUTPUT: Final[str] = "recover_legacy_docs.csv"

# Backlog buckets — same set the reparse script sweeps.
_BACKLOG_STATUSES: Final[tuple[ParseStatus, ...]] = ("manual_review", "error")

# Per-status doc cap used to enumerate the backlog merchants. The
# script only needs distinct merchant_ids out of the result, not every
# row, so the cap is generous: 500 manual_review + 500 error docs
# covers >5 years at current ingest velocity.
_BACKLOG_LIMIT: Final[int] = 500

_log = get_logger(__name__)

# Actor stamp used on audit rows + create_document.uploaded_by.
_ACTOR: Final[str] = "recover_legacy_docs_script"

# Hint string appended to the extraction prompt on the SECOND-pass retry
# when the first pass returned a partial ExtractedStatement (period_start
# / period_end / deposit_total came back null). Empirically Bedrock
# sometimes drops the summary block when the statement period is in a
# non-standard header location (multi-column layouts, the top of page 2,
# etc.); a one-sentence reminder usually recovers it. The prompt
# pipeline already wraps any hint with the "Layout hints from prior
# successful parses of this bank:" prefix, so this string slots into
# that frame.
_PERIOD_RETRY_HINT: Final[str] = (
    "RETRY GUIDANCE: the previous extraction returned a null statement "
    "period. The statement period dates are usually printed near a "
    "heading like 'Statement Period', 'For the Period', or 'Account "
    "Activity From … To …', on either the first page or the top of "
    "page 2. Find them, and populate `summary.period_start` + "
    "`summary.period_end` in ISO YYYY-MM-DD form. Populate "
    "`summary.deposit_total` + `summary.withdrawal_total` from the "
    "summary block printed near the period (typically a small table "
    "of `Total Deposits / Total Withdrawals / Ending Balance` rows). "
    "Use 0 when a category truly does not appear on this statement."
)


# ─────────────────────────────────────────────────────────────────────
# Pure-data row shapes for CSV emission
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AttachmentOutcome:
    """Per-attachment result inside one merchant's processing pass."""

    attachment_id: str
    filename: str
    sha256: str
    # "skip_duplicate" | "ingest_dry_run" | "ingest" |
    # "backfill_dry_run" | "backfill" | "error"
    action: str
    document_id: str  # only populated for ingest / backfill paths
    parse_status: str  # only populated for ingest / backfill (post-pipeline)
    detail: str


@dataclass(frozen=True)
class MerchantOutcome:
    """One backlog merchant's recovery summary, ready to CSV-emit."""

    merchant_id: str
    merchant_name: str
    close_lead_id: str
    attachments_found: int
    attachments_reingested: int
    attachments_backfilled: int
    attachments_skipped: int
    # Pre-flight filename-filter rejections — counted separately so the
    # operator's CSV can distinguish "Close had this PDF, we rejected
    # it as obviously-not-a-statement" from
    # "Close had this PDF, we already have it (SHA dedup)".
    attachments_skipped_non_statement: int
    parse_results: str  # compact: "proceed:2,review:1"
    errors: str
    _per_attachment: tuple[AttachmentOutcome, ...] = field(default_factory=tuple, repr=False)

    @property
    def is_issue(self) -> bool:
        """An issue is any merchant with a non-empty ``errors`` column,
        OR a backlog merchant whose Close Lead exposed zero PDF
        attachments to ingest (the operator should investigate either
        a missing Close link or a Lead with the wrong attachments).
        """
        return bool(self.errors) or self.attachments_found == 0


_CSV_HEADER: Final[tuple[str, ...]] = (
    "merchant_id",
    "merchant_name",
    "close_lead_id",
    "attachments_found",
    "attachments_reingested",
    "attachments_backfilled",
    "attachments_skipped",
    "attachments_skipped_non_statement",
    "parse_results",
    "errors",
)


# ─────────────────────────────────────────────────────────────────────
# Backlog enumeration
# ─────────────────────────────────────────────────────────────────────


def collect_backlog_merchants(
    document_repo: SupabaseDocumentRepository,
    merchants_repo: SupabaseMerchantRepository,
    *,
    limit_per_status: int = _BACKLOG_LIMIT,
) -> list[MerchantRow]:
    """Return the distinct merchants behind the manual_review / error
    backlog.

    The backlog list is computed from ``documents`` (not from
    ``reparse_results.csv``) so the script doesn't depend on a prior
    artifact. Docs whose ``merchant_id`` is NULL (orphans) are dropped
    — they have no Close Lead to look up.
    """
    merchant_ids: set[UUID] = set()
    for status in _BACKLOG_STATUSES:
        for doc in document_repo.list_documents(parse_status=status, limit=limit_per_status):
            if doc.merchant_id is not None:
                merchant_ids.add(doc.merchant_id)

    out: list[MerchantRow] = []
    for mid in sorted(merchant_ids):
        try:
            out.append(merchants_repo.get(mid))
        except MerchantNotFoundError:
            # The doc points at a deleted merchant — nothing we can
            # recover, skip silently. The reparse_corpus surfaces these
            # as ``(merchant missing)`` rows already.
            continue
    return out


# ─────────────────────────────────────────────────────────────────────
# Pipeline retry — partial-extraction recovery
# ─────────────────────────────────────────────────────────────────────


class _RetryHintBankLayouts:
    """Minimal ``BankLayoutRepository`` stub that injects a single fixed
    hint string for every ``get_hints`` call.

    Used by ``_run_pipeline_with_retry`` to coax a second extraction
    pass: when the first run raises ``ExtractionError`` (typically a
    Pydantic ``ValidationError`` on null summary fields) the retry
    threads this stub into ``run_pipeline``'s ``bank_layouts`` argument
    along with ``known_bank_name="aegis_retry_pass"``. The pipeline's
    ``_build_extraction_prompt_suffix`` then sees a non-None hint and
    appends it to the base extraction prompt, prefixed with the
    canonical "Layout hints from prior successful parses of this bank:".

    Writes (``upsert_success``, ``set_hints``) are deliberate no-ops —
    the retry is ephemeral and must not contaminate the production
    ``bank_layouts`` table with a bogus row for the synthetic
    "aegis_retry_pass" bank.
    """

    def __init__(self, hint: str) -> None:
        self._hint = hint

    def get_hints(self, bank_name: str) -> str | None:
        return self._hint

    def upsert_success(self, *, bank_name: str, fingerprint: dict[str, Any]) -> BankLayoutRow:
        # No-op: the synthetic row never leaves this object. We still
        # return a BankLayoutRow so the protocol shape matches.
        return BankLayoutRow(
            bank_name=bank_name,
            layout_fingerprint=dict(fingerprint),
            successful_parses=0,
        )

    def find_by_bank_name(self, bank_name: str) -> BankLayoutRow | None:
        return None

    def set_hints(self, *, bank_name: str, hints: str) -> BankLayoutRow:
        raise NotImplementedError("_RetryHintBankLayouts is read-only by design")

    def list_all(self) -> list[BankLayoutRow]:
        return []


def _format_error_chain(exc: BaseException, max_depth: int = 3) -> str:
    """Render ``exc`` AND its ``__cause__`` chain as a one-cell CSV string.

    Default error formatting (``f"{type(exc).__name__}: {exc}"``) drops
    the underlying exception when the script's typed wrappers
    ``raise … from exc`` — leaving the operator with
    ``PdfStoreWriteError: failed to write pdf_store row …`` and no
    diagnostic for the real cause underneath. Walking ``__cause__`` for
    up to ``max_depth`` levels surfaces the original error inline.

    Format: ``OuterType: outer message  ←  CauseType: cause message``.
    """
    chain: list[str] = []
    current: BaseException | None = exc
    depth = 0
    seen: set[int] = set()
    while current is not None and depth < max_depth and id(current) not in seen:
        seen.add(id(current))
        chain.append(f"{type(current).__name__}: {current}")
        current = current.__cause__
        depth += 1
    return "  ←  ".join(chain)


def _run_pipeline_with_retry(
    pdf_path: str,
    llm: object,
    *,
    log_context: str,
) -> PipelineResult:
    """Run ``run_pipeline`` once; on ``ExtractionError`` retry once with
    a "find the statement period" hint.

    Two-call cap by design: ``.claude/rules/architecture.md`` bans
    LLM retries after a successful validation gate, but extraction
    failure is BEFORE the gate — a single guided second pass on the
    same PDF when the first returned null summary fields is the
    intended use case for the bank_layouts hint surface (operator-
    curated hints exist for exactly this kind of bank-specific
    extraction friction). ``log_context`` lands in the structured
    warning the retry emits so the operator can correlate retries to
    the doc / merchant they belong to.
    """
    try:
        return run_pipeline(pdf_path, llm)  # type: ignore[arg-type]
    except ExtractionError as first:
        _log.warning(
            "recover_legacy_docs.extraction_retry context=%s first_error=%s",
            log_context,
            _format_error_chain(first),
        )
        retry_layouts = _RetryHintBankLayouts(_PERIOD_RETRY_HINT)
        try:
            # vision_fallback_on_extraction_error=True activates the
            # third-pass escape hatch in run_pipeline: if this text+hint
            # retry ALSO raises ExtractionError, the pipeline reruns the
            # same PDF through extract_statement_via_vision before
            # surfacing the failure. Without the flag the retry stops at
            # the second text-layer pass — which is exactly where the
            # 2026-06-17 LOAD LIFT + TMF backlog kept landing (Bedrock
            # repeatedly dropped the page-1 period block on identical
            # text input even with the hint appended).
            return run_pipeline(
                pdf_path,
                llm,  # type: ignore[arg-type]
                bank_layouts=retry_layouts,
                known_bank_name="aegis_retry_pass",
                vision_fallback_on_extraction_error=True,
            )
        except ExtractionError as second:
            # Re-raise the SECOND failure with the FIRST attached as
            # cause so the CSV's error column shows both passes lost.
            # Without the chain link the operator sees only the retry's
            # error and may waste time investigating retry-specific
            # extraction noise rather than the underlying first-pass
            # gap.
            raise second from first


# ─────────────────────────────────────────────────────────────────────
# Per-attachment ingest
# ─────────────────────────────────────────────────────────────────────


def _ingest_attachment(
    *,
    attachment: CloseAttachment,
    file_bytes: bytes,
    merchant: MerchantRow,
    document_repo: SupabaseDocumentRepository,
    pdf_store: SupabasePdfStoreRepository,
    audit: AuditLog,
    llm: object,
    upload_dir: Path,
) -> tuple[str, str]:
    """Run the full ingest path on a fresh Close attachment.

    Mirrors ``persist_pdf_upload`` + the worker's ``run_pipeline`` +
    ``persist_parse_result`` + ``pdf_store.store`` sequence inline so
    the script doesn't need an arq worker in the loop.

    Returns ``(document_id_str, parse_status_str)``. Raises any
    exception unchanged — the caller's per-attachment try/except
    composes the error message.
    """
    file_hash = hashlib.sha256(file_bytes).hexdigest()
    document = document_repo.create_document(
        file_hash=file_hash,
        byte_size=len(file_bytes),
        original_filename=attachment.name,
        uploaded_by=_ACTOR,
        merchant_id=merchant.id,
    )
    audit.record(
        actor=_ACTOR,
        action="document.upload",
        subject_type="document",
        subject_id=document.id,
        details={
            "file_hash": file_hash,
            "byte_size": len(file_bytes),
            "original_filename": attachment.name,
            "merchant_id": str(merchant.id),
            "close_lead_id": merchant.close_lead_id,
            "source": "close_attachment",
            "close_attachment_id": attachment.id,
        },
    )

    upload_dir.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        suffix=".pdf",
        delete=False,
        prefix=f"{_ACTOR}-",
        dir=str(upload_dir),
    )
    try:
        tmp.write(file_bytes)
        tmp.close()
        # Run the pipeline inline. ``_run_pipeline_with_retry`` is the
        # wrapper that gives partial-extraction failures a second pass
        # with the "find statement period" hint (see Fix 2 in the
        # follow-up batch on 2026-06-16). On a fully-clean PDF this is
        # identical to a single run_pipeline call.
        result: PipelineResult = _run_pipeline_with_retry(
            tmp.name,
            llm,
            log_context=f"ingest doc={document.id} att={attachment.id}",
        )
        # Seal pdf_store BEFORE persist_parse_result. If the seal
        # raises, the documents row stays at parse_status="pending"
        # with no transactions / analyses — a clean "not-yet-parsed"
        # state that future re-runs can retry without producing
        # duplicate rows. Order reversed from the initial commit
        # (c531eff) after live --apply on 2026-06-16 hit
        # PdfStoreWriteError and left docs with parse_status set but
        # no plaintext seal; those half-states are now reachable via
        # the --backfill-sha-matches branch, but minimising
        # half-states at the source is the better fix going forward.
        pdf_store.store(document_id=document.id, plaintext=file_bytes)
        document_repo.persist_parse_result(document.id, result=result, merchant_id=merchant.id)
        audit.record(
            actor=_ACTOR,
            action="document.recovered",
            subject_type="document",
            subject_id=document.id,
            details={
                "merchant_id": str(merchant.id),
                "close_lead_id": merchant.close_lead_id,
                "close_attachment_id": attachment.id,
                "parse_status": result.parse_status,
                "fraud_score": result.fraud_score,
            },
        )
        return (str(document.id), result.parse_status)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def _backfill_pdf_store_and_reparse(
    *,
    existing_doc: DocumentRow,
    file_bytes: bytes,
    close_lead_id: str,
    document_repo: SupabaseDocumentRepository,
    pdf_store: SupabasePdfStoreRepository,
    audit: AuditLog,
    llm: object,
    upload_dir: Path,
) -> str:
    """Seal plaintext into ``pdf_store`` under ``existing_doc.id`` and
    re-run the parse pipeline against that same document_id.

    Used only when ``--backfill-sha-matches --apply`` is set AND the
    pdf_store row for ``existing_doc.id`` is missing (the pre-mig-060
    legacy-row recovery case).

    Order matters: pipeline runs first against a temp file, then
    ``pdf_store.store`` seals the bytes BEFORE the destructive
    ``persist_parse_result`` step rewrites the documents row. If the
    seal raises, we abort before mutating any database state, so a
    follow-up run can retry cleanly. Prior ``transactions`` and
    ``analyses`` rows for this document_id are deleted before
    ``persist_parse_result`` to avoid duplicate-row inserts from the
    pipeline's fresh classification + aggregation; for
    manual_review / error legacy docs no such rows should exist, but
    the cleanup is defensive against partial prior runs.

    Returns the new ``parse_status``.
    """
    upload_dir.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        suffix=".pdf",
        delete=False,
        prefix=f"{_ACTOR}-backfill-",
        dir=str(upload_dir),
    )
    try:
        tmp.write(file_bytes)
        tmp.close()
        result: PipelineResult = _run_pipeline_with_retry(
            tmp.name,
            llm,
            log_context=f"backfill doc={existing_doc.id}",
        )

        # Seal first — idempotent upsert on document_id PK (pdf_store
        # repo line ~159). If this raises (write error / settings drift),
        # the documents row is still untouched and a re-run can retry.
        pdf_store.store(document_id=existing_doc.id, plaintext=file_bytes)

        # Defensive wipe of prior transactions + analyses so the
        # persist_parse_result inserts below can't conflict. Same
        # delete order as scripts/_reparse_one.py respects FK
        # constraints (transactions FK analyses → no, both FK
        # documents; either order is fine, but matching the sibling
        # script keeps operator muscle-memory consistent).
        sb = get_supabase()
        sb.table("transactions").delete().eq("document_id", str(existing_doc.id)).execute()
        sb.table("analyses").delete().eq("document_id", str(existing_doc.id)).execute()

        document_repo.persist_parse_result(
            existing_doc.id,
            result=result,
            merchant_id=existing_doc.merchant_id,
        )
        audit.record(
            actor=_ACTOR,
            action="document.pdf_store_backfilled",
            subject_type="document",
            subject_id=existing_doc.id,
            details={
                "merchant_id": (
                    str(existing_doc.merchant_id) if existing_doc.merchant_id else None
                ),
                "close_lead_id": close_lead_id,
                "old_parse_status": existing_doc.parse_status,
                "new_parse_status": result.parse_status,
                "old_fraud_score": existing_doc.fraud_score,
                "new_fraud_score": result.fraud_score,
            },
        )
        return result.parse_status
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def _process_attachment(
    *,
    attachment: CloseAttachment,
    close_client: CloseClient,
    merchant: MerchantRow,
    document_repo: SupabaseDocumentRepository,
    pdf_store: SupabasePdfStoreRepository,
    audit: AuditLog,
    llm: object,
    upload_dir: Path,
    max_upload_bytes: int,
    apply_writes: bool,
    backfill_sha_matches: bool,
) -> AttachmentOutcome:
    """Resolve one attachment: filter, download, dedup, (optionally) ingest.

    Defensive on every external call — Close failures, body-size
    overruns, dedup matches and pipeline crashes each compose a
    distinct ``AttachmentOutcome.action`` so the operator's CSV makes
    sense at a glance.
    """
    # Pre-flight filename filter — BEFORE download. The 2026-06-16
    # --apply pass burnt Bedrock tokens trying to parse driver's
    # licences, voided cheques, signed contracts, etc. that Close
    # operators routinely attach alongside the actual statements.
    # ``filename_is_non_statement`` does case-insensitive substring
    # match against an operator-curated deny list; a hit short-circuits
    # the download + parse path. Audit is per-merchant via the CSV;
    # individual deny rows do not write to audit_log to keep the table
    # focused on real data movements.
    deny_term = filename_is_non_statement(attachment.name)
    if deny_term is not None:
        return AttachmentOutcome(
            attachment_id=attachment.id,
            filename=attachment.name,
            sha256="",
            action="skip_non_statement",
            document_id="",
            parse_status="",
            detail=(f"skip_reason: non_statement_filename (matched deny term {deny_term!r})"),
        )

    # Download bytes + filename. download_attachment relies on the
    # URL cache populated by the caller's list_lead_attachments call.
    try:
        file_bytes, filename = close_client.download_attachment(attachment.id)
    except CloseError as exc:
        return AttachmentOutcome(
            attachment_id=attachment.id,
            filename=attachment.name,
            sha256="",
            action="error",
            document_id="",
            parse_status="",
            detail=f"close_download: {_format_error_chain(exc)}",
        )

    if len(file_bytes) > max_upload_bytes:
        return AttachmentOutcome(
            attachment_id=attachment.id,
            filename=filename,
            sha256="",
            action="error",
            document_id="",
            parse_status="",
            detail=(f"close attachment exceeds {max_upload_bytes} bytes (got {len(file_bytes)})"),
        )

    file_hash = hashlib.sha256(file_bytes).hexdigest()
    existing = document_repo.find_by_hash(file_hash)
    if existing is not None:
        if not backfill_sha_matches:
            # Strict-default skip — matches the from-close route's
            # dedup semantic. The existing row (whatever its parse_status
            # or pdf_store seal state) is not this script's concern.
            return AttachmentOutcome(
                attachment_id=attachment.id,
                filename=filename,
                sha256=file_hash,
                action="skip_duplicate",
                document_id=str(existing.id),
                parse_status=existing.parse_status,
                detail=f"sha matches existing document {existing.id} ({existing.parse_status})",
            )

        # backfill_sha_matches mode: read pdf_store. Sealed → skip
        # (recovery has nothing to do). Missing → backfill candidate.
        try:
            pdf_store.fetch_plaintext(existing.id)
            return AttachmentOutcome(
                attachment_id=attachment.id,
                filename=filename,
                sha256=file_hash,
                action="skip_duplicate",
                document_id=str(existing.id),
                parse_status=existing.parse_status,
                detail=(
                    f"sha matches existing document {existing.id} "
                    f"({existing.parse_status}) + pdf_store sealed"
                ),
            )
        except PdfStoreNotFoundError:
            pass  # fall through to backfill branch below
        except Exception as exc:
            return AttachmentOutcome(
                attachment_id=attachment.id,
                filename=filename,
                sha256=file_hash,
                action="error",
                document_id=str(existing.id),
                parse_status="",
                detail=f"backfill_seal_check: {_format_error_chain(exc)}",
            )

        if not apply_writes:
            return AttachmentOutcome(
                attachment_id=attachment.id,
                filename=filename,
                sha256=file_hash,
                action="backfill_dry_run",
                document_id=str(existing.id),
                parse_status=existing.parse_status,
                detail=(
                    f"would backfill pdf_store + re-parse existing "
                    f"document {existing.id} (currently {existing.parse_status})"
                ),
            )

        try:
            new_status = _backfill_pdf_store_and_reparse(
                existing_doc=existing,
                file_bytes=file_bytes,
                close_lead_id=merchant.close_lead_id or "",
                document_repo=document_repo,
                pdf_store=pdf_store,
                audit=audit,
                llm=llm,
                upload_dir=upload_dir,
            )
        except PdfEncryptedError as exc:
            # Password-protected PDF. Surface a clear, grep-friendly
            # label rather than the raw exception so the operator
            # filtering the CSV with ``grep password_protected`` finds
            # every such row in one pass. Matches the convention the
            # upload route's analyse-metadata step uses for the same
            # condition (operator-facing error code, no script crash).
            return AttachmentOutcome(
                attachment_id=attachment.id,
                filename=filename,
                sha256=file_hash,
                action="error",
                document_id=str(existing.id),
                parse_status="",
                detail=f"backfill: password_protected: {exc}",
            )
        except Exception as exc:
            return AttachmentOutcome(
                attachment_id=attachment.id,
                filename=filename,
                sha256=file_hash,
                action="error",
                document_id=str(existing.id),
                parse_status="",
                detail=f"backfill: {_format_error_chain(exc)}",
            )

        return AttachmentOutcome(
            attachment_id=attachment.id,
            filename=filename,
            sha256=file_hash,
            action="backfill",
            document_id=str(existing.id),
            parse_status=new_status,
            detail=(
                f"backfilled {existing.id}: parse_status {existing.parse_status} → {new_status}"
            ),
        )

    if not apply_writes:
        return AttachmentOutcome(
            attachment_id=attachment.id,
            filename=filename,
            sha256=file_hash,
            action="ingest_dry_run",
            document_id="",
            parse_status="",
            detail=f"would ingest + parse + seal ({len(file_bytes)} bytes)",
        )

    try:
        document_id, parse_status = _ingest_attachment(
            attachment=CloseAttachment(
                id=attachment.id,
                name=filename,
                content_type=attachment.content_type,
                size=len(file_bytes),
                url=attachment.url,
            ),
            file_bytes=file_bytes,
            merchant=merchant,
            document_repo=document_repo,
            pdf_store=pdf_store,
            audit=audit,
            llm=llm,
            upload_dir=upload_dir,
        )
    except DocumentExistsError as exc:
        # Race against another ingest path landing the same SHA between
        # our find_by_hash and our create_document. Surface as a soft
        # skip rather than an error — the doc IS in AEGIS, just not
        # because of us.
        return AttachmentOutcome(
            attachment_id=attachment.id,
            filename=filename,
            sha256=file_hash,
            action="skip_duplicate",
            document_id="",
            parse_status="",
            detail=f"race: {exc}",
        )
    except PdfEncryptedError as exc:
        # Password-protected PDF. ``analyze_metadata`` (the first call
        # inside ``run_pipeline``) raises this before any LLM cost is
        # incurred — pikepdf can't open the file, so we never paid for
        # the parse. The CSV column is grep-friendly: a single
        # ``grep password_protected recover_legacy_docs.csv`` returns
        # every Close attachment the operator needs to re-export from
        # the originating bank with the password stripped. The
        # ``documents`` row created above by ``create_document`` stays
        # at parse_status="pending" with no transactions / analyses —
        # the same half-state Fix 1's seal-before-persist ordering
        # leaves behind for any pipeline failure, and the same one
        # ``--cleanup-orphans`` will sweep.
        return AttachmentOutcome(
            attachment_id=attachment.id,
            filename=filename,
            sha256=file_hash,
            action="error",
            document_id="",
            parse_status="",
            detail=f"ingest: password_protected: {exc}",
        )
    except Exception as exc:
        return AttachmentOutcome(
            attachment_id=attachment.id,
            filename=filename,
            sha256=file_hash,
            action="error",
            document_id="",
            parse_status="",
            detail=f"ingest: {_format_error_chain(exc)}",
        )

    return AttachmentOutcome(
        attachment_id=attachment.id,
        filename=filename,
        sha256=file_hash,
        action="ingest",
        document_id=document_id,
        parse_status=parse_status,
        detail=f"ingested → parse_status={parse_status}",
    )


# ─────────────────────────────────────────────────────────────────────
# Per-merchant orchestration
# ─────────────────────────────────────────────────────────────────────


def _compact_parse_status_summary(outcomes: list[AttachmentOutcome]) -> str:
    """One-cell summary of post-ingest parse_statuses across one
    merchant's attachments. e.g. ``proceed:2,manual_review:1``.

    Counts both fresh ``ingest`` and ``backfill`` actions — both
    produced a real pipeline run that landed a parse_status. Skips
    attachments that didn't reach the parse step (skip_duplicate,
    ingest_dry_run, backfill_dry_run, error before pipeline).
    """
    counts: Counter[str] = Counter()
    for o in outcomes:
        if o.action in ("ingest", "backfill") and o.parse_status:
            counts[o.parse_status] += 1
    if not counts:
        return ""
    return ",".join(f"{status}:{n}" for status, n in sorted(counts.items()))


def process_merchant(
    merchant: MerchantRow,
    *,
    close_client: CloseClient,
    document_repo: SupabaseDocumentRepository,
    pdf_store: SupabasePdfStoreRepository,
    audit: AuditLog,
    llm: object,
    upload_dir: Path,
    max_upload_bytes: int,
    apply_writes: bool,
    backfill_sha_matches: bool,
) -> MerchantOutcome:
    """Resolve one backlog merchant end-to-end.

    Composes per-attachment outcomes into the MerchantOutcome shape
    the CSV consumes. Errors at the Close-list step short-circuit
    (no attachments to walk); errors per-attachment accumulate into
    the merchant's ``errors`` cell so the operator sees them all in
    one row instead of having to grep a per-attachment log.
    """
    if merchant.close_lead_id is None:
        return MerchantOutcome(
            merchant_id=str(merchant.id),
            merchant_name=merchant.business_name,
            close_lead_id="",
            attachments_found=0,
            attachments_reingested=0,
            attachments_backfilled=0,
            attachments_skipped=0,
            attachments_skipped_non_statement=0,
            parse_results="",
            errors="merchant has no close_lead_id",
        )

    try:
        attachments = close_client.list_lead_attachments(merchant.close_lead_id)
    except CloseError as exc:
        return MerchantOutcome(
            merchant_id=str(merchant.id),
            merchant_name=merchant.business_name,
            close_lead_id=merchant.close_lead_id,
            attachments_found=0,
            attachments_reingested=0,
            attachments_backfilled=0,
            attachments_skipped=0,
            attachments_skipped_non_statement=0,
            parse_results="",
            errors=f"close_list: {_format_error_chain(exc)}",
        )

    per_attachment: list[AttachmentOutcome] = []
    for att in attachments:
        per_attachment.append(
            _process_attachment(
                attachment=att,
                close_client=close_client,
                merchant=merchant,
                document_repo=document_repo,
                pdf_store=pdf_store,
                audit=audit,
                llm=llm,
                upload_dir=upload_dir,
                max_upload_bytes=max_upload_bytes,
                apply_writes=apply_writes,
                backfill_sha_matches=backfill_sha_matches,
            )
        )

    reingested = sum(1 for o in per_attachment if o.action == "ingest")
    backfilled = sum(1 for o in per_attachment if o.action == "backfill")
    skipped = sum(1 for o in per_attachment if o.action == "skip_duplicate")
    skipped_non_statement = sum(1 for o in per_attachment if o.action == "skip_non_statement")
    errors = "; ".join(o.detail for o in per_attachment if o.action == "error")
    return MerchantOutcome(
        merchant_id=str(merchant.id),
        merchant_name=merchant.business_name,
        close_lead_id=merchant.close_lead_id,
        attachments_found=len(attachments),
        attachments_reingested=reingested,
        attachments_backfilled=backfilled,
        attachments_skipped=skipped,
        attachments_skipped_non_statement=skipped_non_statement,
        parse_results=_compact_parse_status_summary(per_attachment),
        errors=errors,
        _per_attachment=tuple(per_attachment),
    )


# ─────────────────────────────────────────────────────────────────────
# CSV output
# ─────────────────────────────────────────────────────────────────────


def write_csv(rows: list[MerchantOutcome], stream: object) -> None:
    """Emit the per-merchant CSV — header + one row per backlog
    merchant. ``_per_attachment`` stays off the CSV; it's a debug
    surface the test layer can introspect.
    """
    writer = csv.writer(stream)  # type: ignore[arg-type]
    writer.writerow(_CSV_HEADER)
    for r in rows:
        writer.writerow(
            (
                r.merchant_id,
                r.merchant_name,
                r.close_lead_id,
                r.attachments_found,
                r.attachments_reingested,
                r.attachments_backfilled,
                r.attachments_skipped,
                r.attachments_skipped_non_statement,
                r.parse_results,
                r.errors,
            )
        )


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Recover the manual_review / error backlog by pulling original "
            "PDFs from each merchant's Close Lead attachments. DRY-RUN by "
            "default; pass --apply to ingest + parse + seal."
        )
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Ingest each new Close attachment via "
            "DocumentRepository.create_document + run_pipeline + "
            "persist_parse_result + pdf_store.store; write the matching "
            "audit_log rows. Default is dry-run (list + download + dedup, "
            "no DB writes)."
        ),
    )
    p.add_argument(
        "--backfill-sha-matches",
        action="store_true",
        help=(
            "Extend the SHA-match branch: when a Close attachment's SHA "
            "matches an existing AEGIS document AND that document has no "
            "pdf_store seal (the pre-mig-060 legacy-row case), seal the "
            "plaintext into pdf_store under the existing document_id and "
            "re-run the pipeline against that same row. Combine with "
            "--apply to actually write; alone it shows what WOULD be "
            "backfilled. Without this flag a SHA match is an unconditional "
            "skip (the strict from-close-route default)."
        ),
    )
    p.add_argument(
        "--cleanup-orphans",
        action="store_true",
        help=(
            "Separate maintenance mode: skip the Close traversal entirely "
            "and sweep ``documents`` rows where ``uploaded_by = "
            f"'{_ACTOR}'`` AND ``parsed_at IS NULL``. These are "
            "documents created by a prior --apply run whose pipeline "
            "failed downstream (pdf_store write, extraction, etc.) and "
            "left the row at parse_status='pending' with no children. "
            "Dry-run by default — prints the orphan list; add --apply to "
            "delete each row and write a ``document.orphan_cleanup`` "
            "audit row per delete. The CASCADE on transactions / "
            "analyses / pdf_store cleans up any half-state child rows "
            "in the same transaction."
        ),
    )
    p.add_argument(
        "--vision-retry",
        action="store_true",
        help=(
            "Separate maintenance mode: skip the Close traversal entirely "
            "and re-run the pipeline (with the vision third-pass fallback "
            "enabled) on every ``manual_review`` document that has a "
            "pdf_store-sealed plaintext AND no analyses row — i.e. the "
            "doc that landed in ``manual_review`` because extraction "
            "raised before validation, not because validation failed. "
            "Dry-run by default: lists the candidate docs without re-"
            "running. Combine with --apply to actually re-extract via "
            "Bedrock vision, persist the new analyses + transactions "
            "rows, update parse_status, and write a "
            "``document.vision_retried`` audit row per attempt. Bypasses "
            "the standard merchant→Close-attachment loop entirely; the "
            "PDF source is ``pdf_store.fetch_plaintext`` and the "
            "selector ignores docs lacking ``storage_path`` (those need "
            "the regular --backfill-sha-matches path via a Close re-"
            "fetch)."
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Process at most this many backlog merchants. Useful for "
            "a guarded first run on prod. Default: every merchant."
        ),
    )
    p.add_argument(
        "--output",
        default=_DEFAULT_OUTPUT,
        help=(
            "CSV output path relative to cwd "
            f"(default: {_DEFAULT_OUTPUT}). Use '-' to write to stdout."
        ),
    )
    return p.parse_args()


def _load_dependencies() -> tuple[
    SupabaseDocumentRepository,
    SupabaseMerchantRepository,
    SupabasePdfStoreRepository,
    AuditLog,
    CloseClient,
    object,
    Path,
    int,
]:
    """Construct every prod-backed dependency the recovery needs.

    Lazy import of BedrockClient so unit tests can import the script's
    pure helpers without an AWS env.
    """
    from aegis.llm import BedrockClient

    settings = get_settings()
    audit: AuditLog = SupabaseAuditLog()
    return (
        SupabaseDocumentRepository(),
        SupabaseMerchantRepository(),
        SupabasePdfStoreRepository(),
        audit,
        CloseClient(audit=audit),
        BedrockClient(),
        settings.aegis_upload_dir,
        settings.aegis_max_upload_bytes,
    )


def _open_output_stream(path: str) -> object:
    """Open the CSV destination — stdout when ``path`` is ``"-"``."""
    if path == "-":
        return sys.stdout
    return Path(path).open("w", encoding="utf-8", newline="")


# ─────────────────────────────────────────────────────────────────────
# Orphan cleanup mode (--cleanup-orphans)
# ─────────────────────────────────────────────────────────────────────


def _cleanup_orphans(*, audit: AuditLog, apply_writes: bool) -> tuple[int, int]:
    """Sweep documents rows this script created that never reached a
    parsed state.

    Selector: ``uploaded_by = _ACTOR`` AND ``parsed_at IS NULL``. These
    are the half-state rows the 2026-06-16 --apply pass left behind
    when ``pdf_store.store`` raised on the BYTEA-write path (Fix 1's
    seal-before-persist reorder prevents new ones, but the existing
    pile needs an explicit cleanup), plus any new failure during a
    future --apply pass that errored before persist_parse_result
    landed.

    Dry-run prints the orphan list; --apply deletes each row and writes
    a ``document.orphan_cleanup`` audit row. The documents → transactions
    / analyses / pdf_store CASCADE drops any half-state child rows in
    the same DB transaction, so a single ``documents.delete`` per row
    is enough.

    Returns ``(found, deleted)``. In dry-run mode ``deleted`` is always 0.
    """
    sb = get_supabase()
    result = (
        sb.table("documents")
        .select("id, file_hash, original_filename, uploaded_at, merchant_id")
        .eq("uploaded_by", _ACTOR)
        .is_("parsed_at", "null")
        .execute()
    )
    rows = cast(list[dict[str, Any]], result.data or [])
    print(
        f"# orphan-cleanup: found {len(rows)} orphan(s) "
        f"(uploaded_by={_ACTOR!r}, parsed_at IS NULL)",
        file=sys.stderr,
    )
    for r in rows:
        print(
            f"#   id={r.get('id')}  uploaded_at={r.get('uploaded_at')}  "
            f"merchant_id={r.get('merchant_id')!s:36}  "
            f"filename={(r.get('original_filename') or '?')!r}",
            file=sys.stderr,
        )

    if not apply_writes:
        print(
            "# DRY-RUN: pass --apply to actually delete these rows + write "
            "document.orphan_cleanup audit rows",
            file=sys.stderr,
        )
        return (len(rows), 0)

    deleted = 0
    for r in rows:
        try:
            doc_id = UUID(str(r["id"]))
        except (ValueError, KeyError) as exc:
            print(
                f"ERROR: skipping malformed row {r!r}: {exc}",
                file=sys.stderr,
            )
            continue
        try:
            sb.table("documents").delete().eq("id", str(doc_id)).execute()
            audit.record(
                actor=_ACTOR,
                action="document.orphan_cleanup",
                subject_type="document",
                subject_id=doc_id,
                details={
                    "file_hash": r.get("file_hash"),
                    "original_filename": r.get("original_filename"),
                    "uploaded_at": r.get("uploaded_at"),
                    "merchant_id": r.get("merchant_id"),
                    "reason": "orphan_pending_no_pipeline_completion",
                },
            )
            deleted += 1
        except Exception as exc:
            print(
                f"ERROR: delete failed for {doc_id}: {_format_error_chain(exc)}",
                file=sys.stderr,
            )

    print(
        f"# orphan-cleanup: deleted {deleted}/{len(rows)}",
        file=sys.stderr,
    )
    return (len(rows), deleted)


# ─────────────────────────────────────────────────────────────────────
# Vision-retry mode (--vision-retry)
# ─────────────────────────────────────────────────────────────────────


def _select_vision_retry_candidates() -> list[dict[str, Any]]:
    """Identify ``manual_review`` docs whose extraction never completed.

    Selector encodes "stuck at extraction" without requiring an
    ``error_detail`` column we don't currently populate on the failed
    backfill path:

      * ``parse_status = 'manual_review'`` — only the stuck-doc bucket.
      * No row in ``analyses`` for ``document_id`` — a successful
        extraction always produces an analysis row (even when validation
        later routes the doc to ``manual_review``); the absence of the
        row is the persistence-level signal that extraction itself
        failed before the validation gate ran.
      * ``storage_path IS NOT NULL`` AND ``sha256_original IS NOT NULL``
        — the plaintext is fetchable from ``pdf_store``. Docs whose
        prior pipeline died before the seal step are reachable only
        via a Close-attachment re-fetch (separate code path) and are
        skipped here.

    Empirically matches the 2026-06-17 LOAD LIFT + TMF backlog: 5 of
    the 7 stuck docs satisfy all three predicates; the 2 ``list (16)``
    / ``list (17)`` TMF docs lack both ``storage_path`` and SHA and are
    intentionally out of scope for this mode.
    """
    sb = get_supabase()
    docs_q = (
        sb.table("documents")
        .select("id, original_filename, parse_status, storage_path, sha256_original, merchant_id")
        .eq("parse_status", "manual_review")
        .execute()
    )
    candidates: list[dict[str, Any]] = []
    for d in cast(list[dict[str, Any]], docs_q.data or []):
        if not d.get("storage_path") or not d.get("sha256_original"):
            continue
        ana = sb.table("analyses").select("id").eq("document_id", d["id"]).limit(1).execute()
        if ana.data:
            continue
        candidates.append(d)
    return candidates


def _vision_retry(
    *,
    document_repo: SupabaseDocumentRepository,
    pdf_store: SupabasePdfStoreRepository,
    audit: AuditLog,
    llm: object,
    upload_dir: Path,
    apply_writes: bool,
) -> tuple[int, int, int]:
    """Re-run the pipeline on stuck manual_review docs with the vision
    fallback enabled.

    For each candidate selected by ``_select_vision_retry_candidates``:
      1. Fetch the plaintext from ``pdf_store``.
      2. Write to a temp file (pipeline takes a path, not bytes).
      3. Run ``run_pipeline`` with
         ``vision_fallback_on_extraction_error=True`` so the text pass
         is attempted (cheap; non-deterministic Bedrock might succeed
         this time) and on failure the same PDF is rasterized to PNG
         and re-extracted via Claude vision.
      4. Wipe any half-state ``transactions`` / ``analyses`` rows
         (none expected per selector, but defensive) and persist the
         new pipeline result via ``persist_parse_result``.
      5. Emit a ``document.vision_retried`` audit row capturing the
         old vs new ``parse_status`` so the operator can grep for the
         intervention.

    Returns ``(candidate_count, succeeded_to_proceed_or_review, errored)``.
    """
    candidates = _select_vision_retry_candidates()
    print(
        f"# vision-retry: {len(candidates)} candidate(s)",
        file=sys.stderr,
    )
    for c in candidates:
        print(
            f"#   {c['id'][:8]}  {c['original_filename']}",
            file=sys.stderr,
        )

    if not apply_writes:
        print(
            "# DRY-RUN: pass --apply to actually re-run vision + persist",
            file=sys.stderr,
        )
        return (len(candidates), 0, 0)

    sb = get_supabase()
    upload_dir.mkdir(parents=True, exist_ok=True)
    succeeded = 0
    errored = 0
    for c in candidates:
        try:
            doc_id = UUID(c["id"])
        except (ValueError, KeyError) as exc:
            print(f"  malformed row {c!r}: {exc}", file=sys.stderr)
            errored += 1
            continue
        try:
            existing = document_repo.get_document(doc_id)
        except DocumentNotFoundError as exc:
            print(f"  {str(doc_id)[:8]} not found: {exc}", file=sys.stderr)
            errored += 1
            continue
        try:
            plaintext = pdf_store.fetch_plaintext(doc_id)
        except Exception as exc:
            print(
                f"  {str(doc_id)[:8]} pdf_store fetch failed: {_format_error_chain(exc)}",
                file=sys.stderr,
            )
            errored += 1
            continue
        tmp = tempfile.NamedTemporaryFile(
            suffix=".pdf",
            delete=False,
            prefix=f"{_ACTOR}-vision-",
            dir=str(upload_dir),
        )
        try:
            tmp.write(plaintext)
            tmp.close()
            try:
                result: PipelineResult = run_pipeline(
                    tmp.name,
                    llm,  # type: ignore[arg-type]
                    vision_fallback_on_extraction_error=True,
                )
            except Exception as exc:
                print(
                    f"  {str(doc_id)[:8]} pipeline error: {_format_error_chain(exc)}",
                    file=sys.stderr,
                )
                errored += 1
                continue

            # Defensive wipe — the selector requires zero analyses rows
            # but a race against a parallel reparse could land one in
            # between selection and now. transactions FK documents (not
            # analyses) so either order is safe; matches the order
            # ``_backfill_pdf_store_and_reparse`` uses for muscle memory.
            sb.table("transactions").delete().eq("document_id", str(doc_id)).execute()
            sb.table("analyses").delete().eq("document_id", str(doc_id)).execute()
            document_repo.persist_parse_result(
                doc_id,
                result=result,
                merchant_id=existing.merchant_id,
            )
            audit.record(
                actor=_ACTOR,
                action="document.vision_retried",
                subject_type="document",
                subject_id=doc_id,
                details={
                    "merchant_id": (str(existing.merchant_id) if existing.merchant_id else None),
                    "old_parse_status": existing.parse_status,
                    "new_parse_status": result.parse_status,
                    "old_fraud_score": existing.fraud_score,
                    "new_fraud_score": result.fraud_score,
                    "ocr_fallback_used": "[META] ocr_fallback_used" in result.all_flags,
                },
            )
            print(
                f"  {str(doc_id)[:8]} {c['original_filename']!r} "
                f"-> {result.parse_status} "
                f"(ocr={'yes' if '[META] ocr_fallback_used' in result.all_flags else 'no'})",
                file=sys.stderr,
            )
            if result.parse_status in ("proceed", "review"):
                succeeded += 1
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    return (len(candidates), succeeded, errored)


def main() -> int:
    args = _parse_args()

    try:
        (
            document_repo,
            merchants_repo,
            pdf_store,
            audit,
            close_client,
            llm,
            upload_dir,
            max_upload_bytes,
        ) = _load_dependencies()
    except Exception as exc:
        print(
            f"ERROR: could not initialise dependencies: {exc}",
            file=sys.stderr,
        )
        traceback.print_exc(file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    # Cleanup mode is a separate code path — skip Close traversal, skip
    # ingest, just sweep the orphan documents rows. Returns immediately
    # after the sweep. CloseClient was still constructed by
    # _load_dependencies (cheap, no auth-handshake); explicit close
    # below for symmetry with the main path.
    if args.cleanup_orphans:
        try:
            found, deleted = _cleanup_orphans(audit=audit, apply_writes=args.apply)
        finally:
            close_client.close()
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(
            f"# mode={mode} +cleanup-orphans found={found} deleted={deleted}",
            file=sys.stderr,
        )
        return EXIT_ISSUES_FOUND if found > 0 and not args.apply else EXIT_OK

    # Vision-retry is a separate code path that bypasses Close entirely —
    # pulls plaintext from pdf_store and re-runs the pipeline with the
    # text->text+hint->vision fallback enabled. Returns after the sweep.
    if args.vision_retry:
        try:
            found, succeeded, errored = _vision_retry(
                document_repo=document_repo,
                pdf_store=pdf_store,
                audit=audit,
                llm=llm,
                upload_dir=upload_dir,
                apply_writes=args.apply,
            )
        finally:
            close_client.close()
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(
            f"# mode={mode} +vision-retry candidates={found} "
            f"recovered={succeeded} errored={errored}",
            file=sys.stderr,
        )
        return EXIT_ISSUES_FOUND if errored > 0 else EXIT_OK

    try:
        backlog = collect_backlog_merchants(document_repo, merchants_repo)
    except Exception as exc:
        print(f"ERROR: backlog enumeration failed: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    if args.limit is not None:
        backlog = backlog[: args.limit]

    rows: list[MerchantOutcome] = []
    try:
        for merchant in backlog:
            rows.append(
                process_merchant(
                    merchant,
                    close_client=close_client,
                    document_repo=document_repo,
                    pdf_store=pdf_store,
                    audit=audit,
                    llm=llm,
                    upload_dir=upload_dir,
                    max_upload_bytes=max_upload_bytes,
                    apply_writes=args.apply,
                    backfill_sha_matches=args.backfill_sha_matches,
                )
            )
    finally:
        # CloseClient owns an httpx connection pool — explicit close
        # so the script doesn't leave a half-open socket on exit.
        close_client.close()

    stream = _open_output_stream(args.output)
    try:
        write_csv(rows, stream)
    finally:
        if stream is not sys.stdout:
            stream.close()  # type: ignore[attr-defined]

    total = len(rows)
    found = sum(r.attachments_found for r in rows)
    reingested = sum(r.attachments_reingested for r in rows)
    backfilled = sum(r.attachments_backfilled for r in rows)
    skipped = sum(r.attachments_skipped for r in rows)
    skipped_non_statement = sum(r.attachments_skipped_non_statement for r in rows)
    issues = sum(1 for r in rows if r.is_issue)
    mode = "APPLY" if args.apply else "DRY-RUN"
    backfill_mode = " +backfill" if args.backfill_sha_matches else ""
    print(
        f"# mode={mode}{backfill_mode} merchants={total} attachments_found={found} "
        f"reingested={reingested} backfilled={backfilled} skipped={skipped} "
        f"skipped_non_statement={skipped_non_statement} "
        f"issues={issues}",
        file=sys.stderr,
    )
    return EXIT_ISSUES_FOUND if issues > 0 else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
