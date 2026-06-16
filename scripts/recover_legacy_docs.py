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

Dedup posture (matches the from-close route exactly):

  * Compute ``sha256(plaintext)`` on every Close PDF.
  * ``repository.find_by_hash(sha)`` → if a row exists, **skip** the
    attachment. The PDF is already in AEGIS; whatever state its
    document row is in (manual_review / error / proceed / review) is
    NOT this script's concern. If the operator wants to re-evaluate
    legacy docs whose SHA matches a Close attachment, the right tool
    is ``reparse_corpus.py`` once the same PDF lands here via a fresh
    ingest with a different SHA, OR a follow-up script that targets
    ``pdf_store`` backfill specifically. See the trailing comment in
    `_ingest_attachment` for the alternative semantic if you'd rather
    have backfill behavior.
  * ``find_by_hash`` miss → **fresh ingest**: create a new
    ``documents`` row, write the PDF to ``aegis_upload_dir`` as a
    temp file, run ``aegis.parser.pipeline.run_pipeline``, persist via
    ``persist_parse_result``, seal into ``pdf_store``, delete the
    temp file.

Per-merchant CSV summary (``recover_legacy_docs.csv`` by default):

    merchant_id, merchant_name, close_lead_id,
    attachments_found, attachments_reingested, attachments_skipped,
    parse_results, errors

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
from typing import Final
from uuid import UUID

from aegis.audit import AuditLog, SupabaseAuditLog
from aegis.close.client import (
    CloseAttachment,
    CloseClient,
    CloseError,
)
from aegis.config import get_settings
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import (
    MerchantNotFoundError,
    SupabaseMerchantRepository,
)
from aegis.parser.pipeline import PipelineResult, run_pipeline
from aegis.pdf_store.repository import SupabasePdfStoreRepository
from aegis.storage import (
    DocumentExistsError,
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

# Actor stamp used on audit rows + create_document.uploaded_by.
_ACTOR: Final[str] = "recover_legacy_docs_script"


# ─────────────────────────────────────────────────────────────────────
# Pure-data row shapes for CSV emission
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AttachmentOutcome:
    """Per-attachment result inside one merchant's processing pass."""

    attachment_id: str
    filename: str
    sha256: str
    action: str  # "skip_duplicate" | "ingest_dry_run" | "ingest" | "error"
    document_id: str  # only populated for ingest paths
    parse_status: str  # only populated for ingest (post-pipeline)
    detail: str


@dataclass(frozen=True)
class MerchantOutcome:
    """One backlog merchant's recovery summary, ready to CSV-emit."""

    merchant_id: str
    merchant_name: str
    close_lead_id: str
    attachments_found: int
    attachments_reingested: int
    attachments_skipped: int
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
    "attachments_skipped",
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
        # Run the pipeline inline. Same call signature the arq worker
        # uses at workers.py:210.
        result: PipelineResult = run_pipeline(tmp.name, llm)  # type: ignore[arg-type]
        document_repo.persist_parse_result(document.id, result=result, merchant_id=merchant.id)
        # pdf_store.store mirrors the worker's chunk-B step. Keeps the
        # plaintext-at-rest rule by sealing into Postgres BEFORE the
        # finally-block deletes the temp file.
        pdf_store.store(document_id=document.id, plaintext=file_bytes)
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
) -> AttachmentOutcome:
    """Resolve one attachment: download, dedup, (optionally) ingest.

    Defensive on every external call — Close failures, body-size
    overruns, dedup matches and pipeline crashes each compose a
    distinct ``AttachmentOutcome.action`` so the operator's CSV makes
    sense at a glance.
    """
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
            detail=f"close_download: {type(exc).__name__}: {exc}",
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
        # SAME-SHA HIT. Strict skip — matches the from-close route's
        # dedup semantic.  If you later want a pdf_store-backfill
        # branch (recover the legacy doc's plaintext into pdf_store
        # when the existing row has no seal), this is the place: read
        # whether `pdf_store.fetch_plaintext(existing.id)` raises
        # `PdfStoreNotFoundError`, and on miss call `pdf_store.store`
        # against `existing.id`. Left out of the default behavior
        # because it changes the parse_status invariants for the
        # legacy row and the user-facing spec was explicit about
        # "skip if already stored."
        return AttachmentOutcome(
            attachment_id=attachment.id,
            filename=filename,
            sha256=file_hash,
            action="skip_duplicate",
            document_id=str(existing.id),
            parse_status=existing.parse_status,
            detail=f"sha matches existing document {existing.id} ({existing.parse_status})",
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
    except Exception as exc:
        return AttachmentOutcome(
            attachment_id=attachment.id,
            filename=filename,
            sha256=file_hash,
            action="error",
            document_id="",
            parse_status="",
            detail=f"ingest: {type(exc).__name__}: {exc}",
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

    Skips attachments that didn't reach the parse step
    (skip_duplicate, ingest_dry_run, error before pipeline).
    """
    counts: Counter[str] = Counter()
    for o in outcomes:
        if o.action == "ingest" and o.parse_status:
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
            attachments_skipped=0,
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
            attachments_skipped=0,
            parse_results="",
            errors=f"close_list: {type(exc).__name__}: {exc}",
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
            )
        )

    reingested = sum(1 for o in per_attachment if o.action == "ingest")
    skipped = sum(1 for o in per_attachment if o.action == "skip_duplicate")
    errors = "; ".join(o.detail for o in per_attachment if o.action == "error")
    return MerchantOutcome(
        merchant_id=str(merchant.id),
        merchant_name=merchant.business_name,
        close_lead_id=merchant.close_lead_id,
        attachments_found=len(attachments),
        attachments_reingested=reingested,
        attachments_skipped=skipped,
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
                r.attachments_skipped,
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
    skipped = sum(r.attachments_skipped for r in rows)
    issues = sum(1 for r in rows if r.is_issue)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(
        f"# mode={mode} merchants={total} attachments_found={found} "
        f"reingested={reingested} skipped={skipped} issues={issues}",
        file=sys.stderr,
    )
    return EXIT_ISSUES_FOUND if issues > 0 else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
