"""Re-run the full parse pipeline on every document stuck at
``manual_review`` or ``error`` (DEFAULT: dry-run, zero writes).

Use case: a parser upgrade (prompt change, new validation rule, new
detector) makes documents that previously failed potentially parseable
again. This script walks every ``manual_review`` / ``error`` document in
prod, re-runs ``aegis.parser.pipeline.run_pipeline`` against the
plaintext fetched from ``pdf_store``, and reports the delta to a CSV.

Per CLAUDE.md operating-principles §1 the script is DRY-RUN by default —
re-parsing alone touches Bedrock (cost) but never the database. Add
``--apply`` to also persist the new result via
``SupabaseDocumentRepository.persist_parse_result`` and audit each
write to ``audit_log``.

Re-running the LLM on a stale document IS allowed (different from
``.claude/rules/architecture.md``'s "never retry the LLM after a failed
validation" rule, which forbids in-flight retries INSIDE a single parse
run; that rule keeps the validation firewall intact and is not
weakened by this script). What this script does is a controlled
forward-evolution: today's pipeline against yesterday's PDF.

CSV schema (``reparse_results.csv`` in cwd, or ``--output PATH``):

    document_id, merchant_name, bank_name,
    old_status, new_status,
    old_fraud_score, new_fraud_score, parse_errors

Exit codes (mirror ``scripts/track_a_historical_lookback.py``):

  * ``0`` — every document re-parsed cleanly and either landed
            ``proceed`` / ``review`` or improved its parse_status.
  * ``1`` — runtime error (Supabase / pdf_store / Bedrock init failed,
            settings missing, etc.).
  * ``3`` — at least one document could not be re-parsed (exception
            during pipeline) OR stayed at ``manual_review`` / ``error``
            after re-parse. The CSV's ``parse_errors`` column carries
            the diagnostic. Operator triage required.

Usage (on the prod box, with ``/etc/aegis/aegis.env`` sourced)::

    set -a; source /etc/aegis/aegis.env; set +a
    cd /opt/aegis

    # dry-run: read prod, hit Bedrock, write CSV, never touch DB
    .venv/bin/python scripts/reparse_corpus.py

    # apply: persist the new pipeline result + write audit_log row per doc
    .venv/bin/python scripts/reparse_corpus.py --apply

    # narrower scan
    .venv/bin/python scripts/reparse_corpus.py --limit 10
    .venv/bin/python scripts/reparse_corpus.py --status manual_review

This script lives at ``scripts/`` (flat) alongside the other read-only
historical / re-evaluation helpers. ``scripts/audit/`` is reserved for
prod-WRITE side-effect scripts driven by audit findings; this script
qualifies for ``audit/`` only in ``--apply`` mode, but is kept flat
because the default (and overwhelmingly common) invocation is the
dry-run read.
"""

from __future__ import annotations

import argparse
import csv
import sys
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from uuid import UUID

from aegis.audit import AuditLog, SupabaseAuditLog
from aegis.merchants.repository import (
    MerchantNotFoundError,
    SupabaseMerchantRepository,
)
from aegis.parser.pipeline import PipelineResult, run_pipeline
from aegis.pdf_store.repository import (
    PdfStoreNotFoundError,
    SupabasePdfStoreRepository,
)
from aegis.storage import (
    DocumentRow,
    ParseStatus,
    SupabaseDocumentRepository,
)

# Exit codes — keep aligned with sibling scripts.
EXIT_OK: Final[int] = 0
EXIT_RUNTIME_ERROR: Final[int] = 1
EXIT_ISSUES_FOUND: Final[int] = 3

# Default output filename relative to cwd.
_DEFAULT_OUTPUT: Final[str] = "reparse_results.csv"

# Statuses we sweep.
_REPARSE_TARGETS: Final[tuple[ParseStatus, ...]] = ("manual_review", "error")

# Statuses we consider clean — anything else after re-parse counts as an
# "issue" for the exit-3 gate.
_CLEAN_STATUSES: Final[frozenset[str]] = frozenset({"proceed", "review"})


# ─────────────────────────────────────────────────────────────────────
# Pure-data row shape for CSV emission
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReparseRow:
    """One document's reparse result, ready to write to CSV."""

    document_id: str
    merchant_name: str
    bank_name: str
    old_status: str
    new_status: str
    old_fraud_score: str
    new_fraud_score: str
    parse_errors: str

    @property
    def is_issue(self) -> bool:
        """An issue is any non-clean post-state.

        ``parse_errors`` non-empty marks an exception path (pipeline
        raised). ``new_status`` outside the clean set marks a doc that
        re-ran but didn't advance. Either counts toward exit code 3.
        """
        return bool(self.parse_errors) or self.new_status not in _CLEAN_STATUSES


_CSV_HEADER: Final[tuple[str, ...]] = (
    "document_id",
    "merchant_name",
    "bank_name",
    "old_status",
    "new_status",
    "old_fraud_score",
    "new_fraud_score",
    "parse_errors",
)


# ─────────────────────────────────────────────────────────────────────
# Pipeline-result → CSV-row + parse_errors extraction
# ─────────────────────────────────────────────────────────────────────


def _join_errors(result: PipelineResult) -> str:
    """Compact, grep-friendly summary of why a parse landed where it did.

    Combines validation failures and any ``[ERROR]`` / ``[META]`` /
    ``[MATH]`` flags from ``all_flags``. Semicolon-separated so the CSV
    column stays one cell.
    """
    chunks: list[str] = []
    chunks.extend(result.validation.failures)
    for f in result.all_flags:
        if isinstance(f, str) and (
            f.startswith("[ERROR]") or f.startswith("[META]") or f.startswith("[MATH]")
        ):
            chunks.append(f)
    # De-dup while preserving order so the column doesn't repeat the
    # same failure code twice (the validation gate AND the math flag
    # often emit the same string).
    seen: set[str] = set()
    out: list[str] = []
    for c in chunks:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return "; ".join(out)


def _row_from_result(
    *,
    doc: DocumentRow,
    merchant_name: str,
    result: PipelineResult | None,
    error_text: str | None,
) -> ReparseRow:
    """Compose the CSV row from the document + post-reparse result.

    ``result`` is ``None`` when the pipeline raised — ``error_text`` then
    carries the exception summary. Either ``result`` or ``error_text``
    must be supplied; both being None would mean a logic bug above.

    ``bank_name`` reads off the NEW pipeline extraction
    (``result.extraction.statement.summary.bank_name``) — by definition
    every sweep target failed before, so there is no prior analysis row
    to read a stored ``bank_name`` from. Empty string when extraction
    didn't recover a name (e.g. page-router low-confidence early exit).
    """
    new_status: str
    new_fraud_score: str
    parse_errors: str
    bank_name: str

    if result is not None:
        new_status = result.parse_status
        new_fraud_score = str(result.fraud_score)
        parse_errors = _join_errors(result)
        bank_name = (
            result.extraction.statement.summary.bank_name or ""
            if result.extraction is not None
            else ""
        )
    else:
        new_status = "error"
        new_fraud_score = "—"
        parse_errors = error_text or "unknown pipeline failure"
        bank_name = ""

    return ReparseRow(
        document_id=str(doc.id),
        merchant_name=merchant_name,
        bank_name=bank_name,
        old_status=doc.parse_status,
        new_status=new_status,
        old_fraud_score=str(doc.fraud_score) if doc.fraud_score is not None else "—",
        new_fraud_score=new_fraud_score,
        parse_errors=parse_errors,
    )


# ─────────────────────────────────────────────────────────────────────
# Per-document reparse
# ─────────────────────────────────────────────────────────────────────


def _resolve_merchant_name(
    merchants_repo: SupabaseMerchantRepository,
    merchant_id: UUID | None,
) -> str:
    """Return the merchant's business_name, or a stable placeholder.

    Orphan documents (no merchant_id) and deleted merchants both render
    as a placeholder rather than blowing up the CSV row — the dossier
    page tolerates the same condition.
    """
    if merchant_id is None:
        return "(no merchant)"
    try:
        merchant = merchants_repo.get(merchant_id)
    except MerchantNotFoundError:
        return f"(merchant {str(merchant_id)[:8]} missing)"
    return merchant.business_name


def reparse_one(
    *,
    doc: DocumentRow,
    pdf_store: SupabasePdfStoreRepository,
    merchants_repo: SupabaseMerchantRepository,
    document_repo: SupabaseDocumentRepository,
    audit: AuditLog,
    llm_factory: object,
    apply_writes: bool,
) -> ReparseRow:
    """Re-run the pipeline on one document and emit its CSV row.

    Apply-mode also persists the new result + writes an audit row.
    Dry-run mode never touches the DB.

    ``llm_factory`` is the production ``LLMClient`` (typed as ``object``
    here so the public seam stays import-cheap for tests that exercise
    the pure CSV emission helpers).
    """
    merchant_name = _resolve_merchant_name(merchants_repo, doc.merchant_id)

    # 1. Fetch the plaintext PDF from pdf_store.
    try:
        plaintext = pdf_store.fetch_plaintext(doc.id)
    except PdfStoreNotFoundError:
        return _row_from_result(
            doc=doc,
            merchant_name=merchant_name,
            result=None,
            error_text="pdf_store: no row (legacy doc pre-migration 060?)",
        )
    except Exception as exc:
        return _row_from_result(
            doc=doc,
            merchant_name=merchant_name,
            result=None,
            error_text=f"pdf_store: {type(exc).__name__}: {exc}",
        )

    # 2. Write to a temp PDF for the pipeline (run_pipeline takes a
    #    path — analyze_metadata uses pikepdf which needs a real file).
    #    NamedTemporaryFile with delete=False is the cross-platform
    #    pattern: open it on Linux, close it on Windows, unlink in the
    #    finally block.
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False, prefix="aegis-reparse-")
    try:
        tmp.write(plaintext)
        tmp.close()
        try:
            result = run_pipeline(tmp.name, llm_factory)  # type: ignore[arg-type]
        except Exception as exc:
            return _row_from_result(
                doc=doc,
                merchant_name=merchant_name,
                result=None,
                error_text=f"pipeline: {type(exc).__name__}: {exc}",
            )
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    row = _row_from_result(doc=doc, merchant_name=merchant_name, result=result, error_text=None)

    # 3. Apply: persist + audit. Dry-run skips this entire block.
    if apply_writes:
        try:
            document_repo.persist_parse_result(doc.id, result=result, merchant_id=doc.merchant_id)
            audit.record(
                actor="reparse_corpus_script",
                action="document.reparse.applied",
                subject_type="document",
                subject_id=doc.id,
                details={
                    "old_status": doc.parse_status,
                    "new_status": result.parse_status,
                    "old_fraud_score": doc.fraud_score,
                    "new_fraud_score": result.fraud_score,
                    "parse_errors": row.parse_errors,
                },
            )
        except Exception as exc:
            return _row_from_result(
                doc=doc,
                merchant_name=merchant_name,
                result=None,
                error_text=f"apply: {type(exc).__name__}: {exc}",
            )

    return row


# ─────────────────────────────────────────────────────────────────────
# Sweep + CSV output
# ─────────────────────────────────────────────────────────────────────


def _collect_targets(
    document_repo: SupabaseDocumentRepository,
    *,
    statuses: tuple[ParseStatus, ...],
    limit_per_status: int,
) -> list[DocumentRow]:
    """List every document at one of the target statuses, capped per
    status.

    Two calls (one per status) because ``list_documents`` filters by a
    single ``parse_status``. Cheaper to make N requests than to fetch
    the whole table and filter client-side.
    """
    out: list[DocumentRow] = []
    for status in statuses:
        out.extend(document_repo.list_documents(parse_status=status, limit=limit_per_status))
    return out


def write_csv(rows: list[ReparseRow], stream: object) -> None:
    """Emit the CSV — header on row 1, one ReparseRow per data row."""
    writer = csv.writer(stream)  # type: ignore[arg-type]
    writer.writerow(_CSV_HEADER)
    for r in rows:
        writer.writerow(
            (
                r.document_id,
                r.merchant_name,
                r.bank_name,
                r.old_status,
                r.new_status,
                r.old_fraud_score,
                r.new_fraud_score,
                r.parse_errors,
            )
        )


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Re-run the parse pipeline on every manual_review / error "
            "document and emit a delta CSV. DRY-RUN by default; pass "
            "--apply to also persist."
        )
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Persist the new pipeline result via "
            "SupabaseDocumentRepository.persist_parse_result and write "
            "an audit_log row per touched document. Default is dry-run."
        ),
    )
    p.add_argument(
        "--status",
        choices=("manual_review", "error", "both"),
        default="both",
        help="Which parse_status to sweep. Default both.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=500,
        help=(
            "Per-status document cap. At 100 deals/month + ~3 statements "
            "each, 500 per status covers >1 year of the long tail."
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
    object,
]:
    """Construct every prod-backed dependency the sweep needs.

    Lazy imports so unit tests that exercise the pure CSV helpers can
    import this module without Bedrock / Supabase env vars present.
    Returns the LLM as ``object`` — the production ``BedrockClient``
    satisfies the ``LLMClient`` protocol structurally.
    """
    from aegis.llm import BedrockClient

    return (
        SupabaseDocumentRepository(),
        SupabaseMerchantRepository(),
        SupabasePdfStoreRepository(),
        SupabaseAuditLog(),
        BedrockClient(),
    )


def _open_output_stream(path: str) -> object:
    """Open the CSV destination — stdout when ``path`` is ``"-"``,
    otherwise a writable file in cwd.

    Returns the file object; caller is responsible for closing if
    different from stdout.
    """
    if path == "-":
        return sys.stdout
    return Path(path).open("w", encoding="utf-8", newline="")


def main() -> int:
    args = _parse_args()

    if args.status == "both":
        statuses = _REPARSE_TARGETS
    else:
        statuses = (args.status,)

    try:
        (
            document_repo,
            merchants_repo,
            pdf_store,
            audit,
            llm,
        ) = _load_dependencies()
    except Exception as exc:
        print(
            f"ERROR: could not initialise dependencies: {exc}",
            file=sys.stderr,
        )
        traceback.print_exc(file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    try:
        targets = _collect_targets(document_repo, statuses=statuses, limit_per_status=args.limit)
    except Exception as exc:
        print(f"ERROR: target enumeration failed: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    rows: list[ReparseRow] = []
    for doc in targets:
        rows.append(
            reparse_one(
                doc=doc,
                pdf_store=pdf_store,
                merchants_repo=merchants_repo,
                document_repo=document_repo,
                audit=audit,
                llm_factory=llm,
                apply_writes=args.apply,
            )
        )

    stream = _open_output_stream(args.output)
    try:
        write_csv(rows, stream)
    finally:
        if stream is not sys.stdout:
            stream.close()  # type: ignore[attr-defined]

    total = len(rows)
    issues = sum(1 for r in rows if r.is_issue)
    improved = sum(
        1 for r in rows if r.new_status in _CLEAN_STATUSES and r.old_status not in _CLEAN_STATUSES
    )
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(
        f"# mode={mode} scanned={total} improved={improved} issues={issues}",
        file=sys.stderr,
    )
    return EXIT_ISSUES_FOUND if issues > 0 else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
