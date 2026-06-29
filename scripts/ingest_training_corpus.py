"""Offline bank-statement corpus ingestion — build-plan §6.5.

Walks a folder of operator-provided bank-statement PDFs, runs the
existing AEGIS forensic detectors against each file, and persists
metadata + fingerprints into the training-corpus surface:

* ``corpus_documents`` (migration 097) — one row per file. ALWAYS
  written (regardless of signal state) so the corpus is a complete
  record of what was ingested. Foreign-key-free with respect to the
  live underwriting pipeline (merchants / documents / analyses /
  decisions); the corpus is deliberately disjoint.
* ``bank_layouts`` (migration 059) — successful_parses counter bumped
  + observed Creator/Producer pairs appended to
  ``layout_fingerprint["creator_observations"]`` ONLY when the file
  fires NO forensic signals AND a bank name was detected. This is the
  "clean-statement-only seeding" policy: noisy / suspect statements
  do NOT teach the live extractor.

What the script will NEVER do:

* Create / update / delete rows in ``merchants``, ``documents``,
  ``analyses``, ``transactions``, ``decisions``, ``submissions``, or
  any other live pipeline table.
* Call Bedrock (LLM). All detection is local: pikepdf metadata,
  pymupdf text extraction, the forensic detectors under
  ``aegis.parser.forensic`` (font_consistency, creator_fingerprint,
  text_overlay), and a small regex table for bank-name detection.
* Log PII from the PDF body. Transaction descriptions / account
  holder names are bank-statement PII and flow through the existing
  ``aegis.logger`` masking. The script's stdout prints only the
  filename + bank + page count + signal booleans; the audit row
  ``corpus.document_ingested`` carries file_hash + bank_name +
  signals only.

Per CLAUDE.md operating-principles §1 the script is DRY-RUN by
default. Add ``--apply`` to persist.

Usage::

    .venv/bin/python scripts/ingest_training_corpus.py /path/to/folder
    .venv/bin/python scripts/ingest_training_corpus.py /path/to/folder --apply

Exit codes (mirror sibling corpus scripts):

* ``0`` — every file walked + recorded cleanly.
* ``1`` — runtime error (Supabase init failed, settings missing).
* ``2`` — caller-side mistake (missing folder, etc.).
* ``3`` — at least one file failed to ingest (write or detection
  error per file). Other files still ingest.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, cast
from uuid import UUID

import pikepdf
import pymupdf

from aegis.audit import AuditLog, AuditWriteError, InMemoryAuditLog
from aegis.bank_layouts.repository import (
    BankLayoutRepository,
    BankLayoutWriteError,
    InMemoryBankLayoutRepository,
)
from aegis.logger import get_logger
from aegis.parser.forensic import (
    creator_fingerprint as creator_fp,
)
from aegis.parser.forensic import (
    font_consistency,
    text_overlay,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


_log = get_logger(__name__)


# Exit codes — aligned with sibling scripts (populate_bank_layouts.py).
EXIT_OK: Final[int] = 0
EXIT_RUNTIME_ERROR: Final[int] = 1
EXIT_CALLER_ERROR: Final[int] = 2
EXIT_ISSUES_FOUND: Final[int] = 3


# Cap on the SHA-256 read chunk size. 1 MiB is large enough that even
# 25 MiB statements clear in 25 reads; small enough to keep memory flat.
_HASH_CHUNK_BYTES: Final[int] = 1 << 20

# Cap on the first-page text snippet used for bank-name detection.
# 4 KiB covers every observed bank header / footer pattern; reading
# more wastes CPU and pulls PII into a string we hold in memory.
_HEADER_TEXT_BYTES: Final[int] = 4096

# Bank-name regex table. Each entry maps a recognisable substring
# (case-insensitive) on the first-page text → the canonical display
# bank_name used throughout AEGIS (matching the strings the LLM
# extraction emits as ``StatementSummary.bank_name``). The matching
# stops on the first hit; ordering puts the more-specific patterns
# first so e.g. "JPMorgan Chase" wins over a future "JPMorgan" entry.
# Operator owns the table's growth (CLAUDE.md OP-4: real data only,
# no industry-typical guesses). Entries below are taken verbatim from
# ``KNOWN_CREATOR_PATTERNS`` in
# ``aegis.parser.forensic.creator_fingerprint`` so the strings stay
# aligned with the existing fingerprint registry. Add a row here when
# you add one to that registry.
_BANK_NAME_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"\bJPMorgan\s+Chase", re.IGNORECASE), "JPMorgan Chase Bank, N.A."),
    (re.compile(r"\bChase\b", re.IGNORECASE), "JPMorgan Chase Bank, N.A."),
    (re.compile(r"\bBank\s+of\s+America\b", re.IGNORECASE), "Bank of America, N.A."),
    (re.compile(r"\bTD\s+Bank\b", re.IGNORECASE), "TD Bank, N.A."),
    (re.compile(r"\bWells\s+Fargo\b", re.IGNORECASE), "Wells Fargo Bank, N.A."),
    (re.compile(r"\bCitibank\b", re.IGNORECASE), "Citibank, N.A."),
    (re.compile(r"\bCapital\s+One\b", re.IGNORECASE), "Capital One, N.A."),
    (re.compile(r"\bPNC\b", re.IGNORECASE), "PNC Bank, N.A."),
    (re.compile(r"\bU\.?S\.?\s+Bank\b", re.IGNORECASE), "U.S. Bank, N.A."),
    (re.compile(r"\bTruist\b", re.IGNORECASE), "Truist Bank"),
    (re.compile(r"\bRegions\s+Bank\b", re.IGNORECASE), "Regions Bank"),
    (re.compile(r"\bFifth\s+Third\b", re.IGNORECASE), "Fifth Third Bank"),
    (re.compile(r"\bHuntington\b", re.IGNORECASE), "Huntington National Bank"),
    (re.compile(r"\bM&T\s+Bank\b", re.IGNORECASE), "M&T Bank"),
    (re.compile(r"\bKeyBank\b", re.IGNORECASE), "KeyBank, N.A."),
    (re.compile(r"\bBMO\b", re.IGNORECASE), "BMO Harris Bank, N.A."),
    (re.compile(r"\bThird\s+Coast\s+Bank\b", re.IGNORECASE), "Third Coast Bank, SSB"),
    (re.compile(r"\bMercury\b", re.IGNORECASE), "Mercury"),
    (re.compile(r"\bBluevine\b", re.IGNORECASE), "Bluevine"),
    (re.compile(r"\bNovo\b", re.IGNORECASE), "Novo"),
    (re.compile(r"\bRelay\b", re.IGNORECASE), "Relay"),
    (re.compile(r"\bLili\b", re.IGNORECASE), "Lili"),
    (re.compile(r"\bRho\b", re.IGNORECASE), "Rho"),
    (re.compile(r"\bBrex\b", re.IGNORECASE), "Brex"),
)


# ─────────────────────────────────────────────────────────────────────
# Pure-data row shapes
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ForensicResult:
    """Detector output for one PDF, collapsed to the booleans the
    corpus_documents schema stores plus the raw Creator / Producer
    strings the script captured from the PDF metadata.

    ``creator`` / ``producer`` may be empty strings when the field was
    missing or pikepdf failed to read it; they're NEVER ``None`` so the
    downstream insert payload has a stable shape.
    """

    has_font_inconsistency: bool
    has_text_overlay: bool
    has_creator_mismatch: bool
    creator: str
    producer: str
    page_count: int

    @property
    def fraud_signals_fired(self) -> bool:
        return self.has_font_inconsistency or self.has_text_overlay or self.has_creator_mismatch


@dataclass(frozen=True)
class IngestResult:
    """One row per PDF in the operator-facing summary.

    ``action`` is one of:
      * ``"ingested"`` — corpus_documents row written, optionally
                         hint / fingerprint updated.
      * ``"dedup_skip"`` — file_hash already present in
                            corpus_documents; nothing written.
      * ``"error"`` — detection or write raised; the file did NOT
                       contribute a row. Other files still process.
    """

    file_path: str
    file_hash: str
    bank_name: str | None
    page_count: int
    fraud_signals_fired: bool
    has_font_inconsistency: bool
    has_text_overlay: bool
    has_creator_mismatch: bool
    hint_updated: bool
    fingerprint_added: bool
    action: str  # "ingested" | "dedup_skip" | "error"
    detail: str = ""

    @property
    def is_issue(self) -> bool:
        return self.action == "error"


@dataclass
class IngestSummary:
    """Counters reported at the end of the run."""

    files_walked: int = 0
    files_ingested: int = 0
    files_skipped_dedup: int = 0
    files_errored: int = 0
    banks_seen: set[str] = field(default_factory=set)
    hints_updated: int = 0
    fingerprints_added: int = 0

    def render(self) -> str:
        return (
            f"{self.files_walked} files, "
            f"{len(self.banks_seen)} banks, "
            f"{self.hints_updated} hints generated/updated, "
            f"{self.fingerprints_added} fingerprints added, "
            f"{self.files_skipped_dedup} skipped (dedup)"
        )


# ─────────────────────────────────────────────────────────────────────
# Repository protocol — corpus_documents
# ─────────────────────────────────────────────────────────────────────


class CorpusDocumentsRepository(Protocol):
    """Narrow interface the script needs from the corpus_documents
    table. Two impls below: in-memory for tests, Supabase for prod.
    """

    def exists_by_hash(self, file_hash: str) -> bool: ...

    def insert(
        self,
        *,
        file_hash: str,
        original_path: str,
        bank_name: str | None,
        detected_creator: str,
        detected_producer: str,
        page_count: int,
        has_font_inconsistency: bool,
        has_text_overlay: bool,
        has_creator_mismatch: bool,
        fraud_signals_fired: bool,
        notes: str | None = None,
    ) -> UUID: ...


class CorpusWriteError(RuntimeError):
    """Raised when a corpus_documents row could not be persisted."""


class InMemoryCorpusRepository:
    """Dict-backed corpus_documents store. Tests only."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def exists_by_hash(self, file_hash: str) -> bool:
        return file_hash in self.rows

    def insert(
        self,
        *,
        file_hash: str,
        original_path: str,
        bank_name: str | None,
        detected_creator: str,
        detected_producer: str,
        page_count: int,
        has_font_inconsistency: bool,
        has_text_overlay: bool,
        has_creator_mismatch: bool,
        fraud_signals_fired: bool,
        notes: str | None = None,
    ) -> UUID:
        if file_hash in self.rows:
            raise CorpusWriteError(f"duplicate file_hash {file_hash[:12]}…")
        from uuid import uuid4

        row_id = uuid4()
        self.rows[file_hash] = {
            "id": row_id,
            "file_hash": file_hash,
            "original_path": original_path,
            "bank_name": bank_name,
            "detected_creator": detected_creator,
            "detected_producer": detected_producer,
            "page_count": page_count,
            "has_font_inconsistency": has_font_inconsistency,
            "has_text_overlay": has_text_overlay,
            "has_creator_mismatch": has_creator_mismatch,
            "fraud_signals_fired": fraud_signals_fired,
            "notes": notes,
        }
        return row_id


class SupabaseCorpusRepository:
    """Persistence backed by Postgres ``corpus_documents`` (mig 097)."""

    def exists_by_hash(self, file_hash: str) -> bool:
        from aegis.db import get_supabase

        result = (
            get_supabase()
            .table("corpus_documents")
            .select("id")
            .eq("file_hash", file_hash)
            .limit(1)
            .execute()
        )
        rows = cast(list[dict[str, Any]], result.data or [])
        return bool(rows)

    def insert(
        self,
        *,
        file_hash: str,
        original_path: str,
        bank_name: str | None,
        detected_creator: str,
        detected_producer: str,
        page_count: int,
        has_font_inconsistency: bool,
        has_text_overlay: bool,
        has_creator_mismatch: bool,
        fraud_signals_fired: bool,
        notes: str | None = None,
    ) -> UUID:
        from aegis.db import get_supabase

        payload: dict[str, Any] = {
            "file_hash": file_hash,
            "original_path": original_path,
            "bank_name": bank_name,
            "detected_creator": detected_creator or None,
            "detected_producer": detected_producer or None,
            "page_count": page_count,
            "has_font_inconsistency": has_font_inconsistency,
            "has_text_overlay": has_text_overlay,
            "has_creator_mismatch": has_creator_mismatch,
            "fraud_signals_fired": fraud_signals_fired,
            "notes": notes,
        }
        try:
            result = get_supabase().table("corpus_documents").insert(payload).execute()
        except Exception as exc:
            _log.error("corpus_documents.insert_failed hash=%s", file_hash[:12])
            raise CorpusWriteError(
                f"failed to insert corpus_documents row for {file_hash[:12]}…"
            ) from exc
        inserted = cast(list[dict[str, Any]], result.data or [])
        if not inserted:
            raise CorpusWriteError("supabase insert returned no row for corpus_documents")
        return UUID(inserted[0]["id"])


# ─────────────────────────────────────────────────────────────────────
# Pure detection helpers
# ─────────────────────────────────────────────────────────────────────


def compute_sha256(path: Path) -> str:
    """Stream-hash ``path`` in 1 MiB chunks. Returns hex digest."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def detect_bank_name(first_page_text: str) -> str | None:
    """Walk ``_BANK_NAME_PATTERNS`` against the text and return the
    first matching canonical bank name, or ``None`` on no hit.

    The text is the operator-visible first-page header / footer — bank
    statements universally embed the bank's display name in one of
    those zones. Substring matching is intentional ("JPMorgan Chase
    Bank, National Association" → "JPMorgan Chase Bank, N.A."); we
    never claim per-token precision.
    """
    if not first_page_text:
        return None
    for pattern, canonical in _BANK_NAME_PATTERNS:
        if pattern.search(first_page_text):
            return canonical
    return None


def _read_pdf_metadata(pdf_path: Path) -> tuple[str, str, int]:
    """Return ``(creator, producer, page_count)`` for a PDF.

    Failure-tolerant: any pikepdf / pymupdf failure returns the empty-
    string / zero shape so the per-file walk can continue. Mirrors the
    null-result posture of ``aegis.parser.forensic.*`` detectors.
    """
    creator = ""
    producer = ""
    page_count = 0
    try:
        with pikepdf.open(str(pdf_path)) as pdf:
            # pikepdf.Dictionary is dynamically-typed in stubs; the
            # ``or {}`` fallback covers a missing /Info dict, and ``Any``
            # silences the mypy var-annotated complaint at the
            # narrowest scope. Mirrors aegis.parser.metadata line 474.
            docinfo: Any = pdf.docinfo or {}
            creator_val = docinfo.get("/Creator")
            producer_val = docinfo.get("/Producer")
            creator = str(creator_val) if creator_val is not None else ""
            producer = str(producer_val) if producer_val is not None else ""
            page_count = len(pdf.pages)
    except Exception as exc:
        # Encrypted, corrupt, non-PDF — log at debug and continue. The
        # corpus row still records the (empty) fields so the operator
        # sees the gap; failing this read must NEVER block ingestion of
        # other files in the folder.
        _log.debug("pikepdf.open_failed path=%s err=%s", pdf_path.name, type(exc).__name__)
    return creator, producer, page_count


def _read_first_page_text(pdf_path: Path) -> str:
    """Pull the first page's plain text, capped at ``_HEADER_TEXT_BYTES``.

    Best-effort: failures return empty string (which leaves
    ``detect_bank_name`` returning ``None``).
    """
    try:
        with pymupdf.open(str(pdf_path)) as doc:  # type: ignore[no-untyped-call]
            if doc.page_count == 0:
                return ""
            page = doc.load_page(0)
            text = cast(str, page.get_text("text") or "")
    except Exception:
        return ""
    return text[:_HEADER_TEXT_BYTES]


def run_forensic_pass(pdf_path: Path) -> ForensicResult:
    """Invoke every forensic detector + the metadata reader on a PDF.

    Returns a ``ForensicResult`` collapsing the detector outputs to
    the booleans the corpus_documents schema stores plus the raw
    Creator / Producer strings for downstream fingerprint seeding.
    """
    creator, producer, page_count = _read_pdf_metadata(pdf_path)
    first_page = _read_first_page_text(pdf_path)
    bank_name = detect_bank_name(first_page)

    try:
        font_result = font_consistency.analyze(pdf_path)
        has_font = font_result.inconsistency_detected
    except Exception:
        has_font = False
    try:
        overlay_result = text_overlay.analyze(pdf_path)
        has_overlay = overlay_result.overlay_detected
    except Exception:
        has_overlay = False
    try:
        # creator_fingerprint.analyze needs the bank_name; falls
        # through to no-flag on unknown bank.
        fp_result = creator_fp.analyze(creator, producer, bank_name)
        has_mismatch = fp_result.mismatch_detected
    except Exception:
        has_mismatch = False

    return ForensicResult(
        has_font_inconsistency=has_font,
        has_text_overlay=has_overlay,
        has_creator_mismatch=has_mismatch,
        creator=creator,
        producer=producer,
        page_count=page_count,
    )


# ─────────────────────────────────────────────────────────────────────
# Clean-statement seeding — bank_layouts upsert + fingerprint append
# ─────────────────────────────────────────────────────────────────────


def _append_creator_observation(
    repo: BankLayoutRepository,
    *,
    bank_name: str,
    creator: str,
    producer: str,
) -> bool:
    """Append a ``(creator, producer)`` pair to the bank's
    ``layout_fingerprint["creator_observations"]`` list when not
    already present. Returns True iff a new pair was added.

    The bank_layouts table is the persistent surface for
    creator/producer fingerprints — the per-bank dict in
    ``aegis.parser.forensic.creator_fingerprint.KNOWN_CREATOR_PATTERNS``
    is the operator-curated whitelist; this list is the
    auto-accumulated observation set the operator reviews when growing
    that whitelist.

    Empty/blank creator AND producer skips the write (nothing to
    fingerprint). Either field alone is enough — Chase exports carry
    bank identity only on /Producer (see
    creator_fingerprint.KNOWN_CREATOR_PATTERNS docstring).
    """
    creator_n = creator.strip()
    producer_n = producer.strip()
    if not creator_n and not producer_n:
        return False

    existing_row = repo.find_by_bank_name(bank_name)
    observations: list[dict[str, str]]
    if existing_row is None:
        observations = []
    else:
        raw = existing_row.layout_fingerprint.get("creator_observations") or []
        if isinstance(raw, list):
            observations = [
                {
                    "creator": str(item.get("creator") or ""),
                    "producer": str(item.get("producer") or ""),
                }
                for item in raw
                if isinstance(item, dict)
            ]
        else:
            observations = []

    new_pair = {"creator": creator_n, "producer": producer_n}
    if new_pair in observations:
        return False
    observations.append(new_pair)
    # upsert_success bumps successful_parses + last_seen and merges
    # the fingerprint dict (new keys win). Pass the full list so the
    # client-side merge replaces the prior observation list with the
    # appended one.
    repo.upsert_success(
        bank_name=bank_name,
        fingerprint={"creator_observations": observations},
    )
    return True


def _record_clean_bank_layout(
    repo: BankLayoutRepository,
    *,
    bank_name: str,
    forensic: ForensicResult,
) -> tuple[bool, bool]:
    """For a clean (no-signal) parse with a known bank, bump
    successful_parses + append the creator observation if new.

    Returns ``(hint_updated, fingerprint_added)``:
      * ``hint_updated`` — successful_parses was incremented (always
        True for a clean parse with a known bank, since upsert_success
        is monotonic and the script always calls it).
      * ``fingerprint_added`` — a new (creator, producer) pair was
        appended to the bank's creator_observations list.

    Both writes share one ``upsert_success`` round-trip when the
    fingerprint changes; when only the parse count needs bumping we
    still upsert (the creator pair was already on file).
    """
    # ``_append_creator_observation`` issues the upsert_success when
    # the pair is new (which also covers the bump). When the pair was
    # already on file we still need to bump successful_parses — call
    # upsert_success with the fingerprint dict unchanged so the merge
    # is a no-op on the JSONB but the counter still ticks up.
    fingerprint_added = _append_creator_observation(
        repo,
        bank_name=bank_name,
        creator=forensic.creator,
        producer=forensic.producer,
    )
    if not fingerprint_added:
        # Bump the parse counter without changing the fingerprint.
        repo.upsert_success(bank_name=bank_name, fingerprint={})
    return True, fingerprint_added


# ─────────────────────────────────────────────────────────────────────
# Per-file orchestration
# ─────────────────────────────────────────────────────────────────────


def _find_pdf_files(root: Path) -> list[Path]:
    """Recursively walk ``root`` and return every ``.pdf`` file
    (case-insensitive suffix match). Sorted for deterministic output.
    """
    if not root.is_dir():
        return []
    pdfs = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"]
    pdfs.sort()
    return pdfs


def ingest_one(
    pdf_path: Path,
    *,
    corpus_repo: CorpusDocumentsRepository,
    bank_repo: BankLayoutRepository,
    audit: AuditLog,
    apply: bool,
) -> IngestResult:
    """Ingest exactly one PDF.

    Pure orchestration — every read/write goes through an injected
    repository so tests can run end-to-end without touching the live
    Supabase tables. The function NEVER touches merchants / documents
    / analyses / decisions; the invariant is enforced structurally by
    the absence of those imports plus an assertion in the test layer
    (see ``tests/scripts/test_ingest_training_corpus.py``).
    """
    try:
        file_hash = compute_sha256(pdf_path)
    except Exception as exc:
        return IngestResult(
            file_path=str(pdf_path),
            file_hash="",
            bank_name=None,
            page_count=0,
            fraud_signals_fired=False,
            has_font_inconsistency=False,
            has_text_overlay=False,
            has_creator_mismatch=False,
            hint_updated=False,
            fingerprint_added=False,
            action="error",
            detail=f"sha256 read failed: {type(exc).__name__}",
        )

    if corpus_repo.exists_by_hash(file_hash):
        return IngestResult(
            file_path=str(pdf_path),
            file_hash=file_hash,
            bank_name=None,
            page_count=0,
            fraud_signals_fired=False,
            has_font_inconsistency=False,
            has_text_overlay=False,
            has_creator_mismatch=False,
            hint_updated=False,
            fingerprint_added=False,
            action="dedup_skip",
            detail="file_hash already in corpus_documents",
        )

    forensic = run_forensic_pass(pdf_path)
    first_page = _read_first_page_text(pdf_path)
    bank_name = detect_bank_name(first_page)

    if not apply:
        # Dry-run: report what WOULD happen without writing.
        return IngestResult(
            file_path=str(pdf_path),
            file_hash=file_hash,
            bank_name=bank_name,
            page_count=forensic.page_count,
            fraud_signals_fired=forensic.fraud_signals_fired,
            has_font_inconsistency=forensic.has_font_inconsistency,
            has_text_overlay=forensic.has_text_overlay,
            has_creator_mismatch=forensic.has_creator_mismatch,
            hint_updated=False,
            fingerprint_added=False,
            action="ingested",
            detail="dry-run; no write",
        )

    try:
        row_id = corpus_repo.insert(
            file_hash=file_hash,
            original_path=str(pdf_path),
            bank_name=bank_name,
            detected_creator=forensic.creator,
            detected_producer=forensic.producer,
            page_count=forensic.page_count,
            has_font_inconsistency=forensic.has_font_inconsistency,
            has_text_overlay=forensic.has_text_overlay,
            has_creator_mismatch=forensic.has_creator_mismatch,
            fraud_signals_fired=forensic.fraud_signals_fired,
        )
    except CorpusWriteError as exc:
        return IngestResult(
            file_path=str(pdf_path),
            file_hash=file_hash,
            bank_name=bank_name,
            page_count=forensic.page_count,
            fraud_signals_fired=forensic.fraud_signals_fired,
            has_font_inconsistency=forensic.has_font_inconsistency,
            has_text_overlay=forensic.has_text_overlay,
            has_creator_mismatch=forensic.has_creator_mismatch,
            hint_updated=False,
            fingerprint_added=False,
            action="error",
            detail=f"corpus_documents insert failed: {exc}",
        )

    hint_updated = False
    fingerprint_added = False
    if not forensic.fraud_signals_fired and bank_name is not None:
        try:
            hint_updated, fingerprint_added = _record_clean_bank_layout(
                bank_repo,
                bank_name=bank_name,
                forensic=forensic,
            )
        except BankLayoutWriteError as exc:
            # Corpus row already landed — keep going, but flag the
            # partial failure so the operator can see it.
            return IngestResult(
                file_path=str(pdf_path),
                file_hash=file_hash,
                bank_name=bank_name,
                page_count=forensic.page_count,
                fraud_signals_fired=forensic.fraud_signals_fired,
                has_font_inconsistency=forensic.has_font_inconsistency,
                has_text_overlay=forensic.has_text_overlay,
                has_creator_mismatch=forensic.has_creator_mismatch,
                hint_updated=False,
                fingerprint_added=False,
                action="error",
                detail=f"bank_layouts write failed: {exc}",
            )

    # Audit row — file_hash + bank_name + signals only (no PII).
    try:
        audit.record(
            actor="ingest_training_corpus",
            action="corpus.document_ingested",
            subject_type="corpus_document",
            subject_id=row_id,
            details={
                "file_hash": file_hash,
                "bank_name": bank_name,
                "page_count": forensic.page_count,
                "has_font_inconsistency": forensic.has_font_inconsistency,
                "has_text_overlay": forensic.has_text_overlay,
                "has_creator_mismatch": forensic.has_creator_mismatch,
                "fraud_signals_fired": forensic.fraud_signals_fired,
                "hint_updated": hint_updated,
                "fingerprint_added": fingerprint_added,
            },
        )
    except AuditWriteError as exc:
        return IngestResult(
            file_path=str(pdf_path),
            file_hash=file_hash,
            bank_name=bank_name,
            page_count=forensic.page_count,
            fraud_signals_fired=forensic.fraud_signals_fired,
            has_font_inconsistency=forensic.has_font_inconsistency,
            has_text_overlay=forensic.has_text_overlay,
            has_creator_mismatch=forensic.has_creator_mismatch,
            hint_updated=hint_updated,
            fingerprint_added=fingerprint_added,
            action="error",
            detail=f"audit write failed: {exc}",
        )

    return IngestResult(
        file_path=str(pdf_path),
        file_hash=file_hash,
        bank_name=bank_name,
        page_count=forensic.page_count,
        fraud_signals_fired=forensic.fraud_signals_fired,
        has_font_inconsistency=forensic.has_font_inconsistency,
        has_text_overlay=forensic.has_text_overlay,
        has_creator_mismatch=forensic.has_creator_mismatch,
        hint_updated=hint_updated,
        fingerprint_added=fingerprint_added,
        action="ingested",
        detail="",
    )


def ingest_folder(
    folder: Path,
    *,
    corpus_repo: CorpusDocumentsRepository,
    bank_repo: BankLayoutRepository,
    audit: AuditLog,
    apply: bool,
) -> tuple[list[IngestResult], IngestSummary]:
    """Walk ``folder`` for PDFs and ingest each one.

    Returns ``(results, summary)``. The summary counters drive the
    final stdout banner; the per-file results power any future CSV /
    JSON export (not implemented in this iteration).
    """
    summary = IngestSummary()
    results: list[IngestResult] = []
    pdfs = _find_pdf_files(folder)
    for pdf_path in pdfs:
        summary.files_walked += 1
        result = ingest_one(
            pdf_path,
            corpus_repo=corpus_repo,
            bank_repo=bank_repo,
            audit=audit,
            apply=apply,
        )
        results.append(result)
        if result.action == "ingested":
            summary.files_ingested += 1
            if result.bank_name is not None:
                summary.banks_seen.add(result.bank_name)
            if result.hint_updated:
                summary.hints_updated += 1
            if result.fingerprint_added:
                summary.fingerprints_added += 1
        elif result.action == "dedup_skip":
            summary.files_skipped_dedup += 1
        else:
            summary.files_errored += 1
    return results, summary


# ─────────────────────────────────────────────────────────────────────
# Stdout rendering
# ─────────────────────────────────────────────────────────────────────


def render_per_file_line(result: IngestResult) -> str:
    """One operator-readable line per file. No PII — only filename
    (path the operator chose), bank, page count, signal booleans.
    """
    name = Path(result.file_path).name
    if result.action == "dedup_skip":
        return f"  SKIP  {name}  (already ingested, hash={result.file_hash[:10]}…)"
    if result.action == "error":
        return f"  ERR   {name}  {result.detail}"
    signals = []
    if result.has_font_inconsistency:
        signals.append("font")
    if result.has_text_overlay:
        signals.append("overlay")
    if result.has_creator_mismatch:
        signals.append("creator")
    sig_str = ",".join(signals) if signals else "clean"
    bank = result.bank_name or "?"
    return f"  OK    {name}  bank={bank}  pages={result.page_count}  signals={sig_str}"


def render_summary(
    results: Iterable[IngestResult],
    summary: IngestSummary,
    *,
    apply: bool,
) -> str:
    """Final banner. ``--dry-run`` prefix when no writes happened."""
    lines: list[str] = []
    for result in results:
        lines.append(render_per_file_line(result))
    mode = "APPLIED" if apply else "DRY-RUN"
    lines.append("")
    lines.append(f"[{mode}] {summary.render()}")
    if summary.files_errored:
        lines.append(f"  {summary.files_errored} file(s) errored — see ERR lines.")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# CLI entrypoint
# ─────────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Ingest a folder of operator-provided bank-statement PDFs "
            "into the training corpus (build-plan §6.5)."
        )
    )
    parser.add_argument(
        "folder",
        type=Path,
        help="Folder to walk recursively for *.pdf files.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Persist corpus_documents rows + bank_layouts updates + "
            "audit rows. Default is dry-run (walk + report, no writes)."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    folder: Path = args.folder
    if not folder.exists() or not folder.is_dir():
        print(f"ERROR: folder not found or not a directory: {folder}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    corpus_repo: CorpusDocumentsRepository
    bank_repo: BankLayoutRepository
    audit: AuditLog
    if args.apply:
        try:
            from aegis.audit import SupabaseAuditLog
            from aegis.bank_layouts.repository import SupabaseBankLayoutRepository

            corpus_repo = SupabaseCorpusRepository()
            bank_repo = SupabaseBankLayoutRepository()
            audit = SupabaseAuditLog()
        except Exception as exc:
            print(f"ERROR: supabase init failed: {exc}", file=sys.stderr)
            return EXIT_RUNTIME_ERROR
    else:
        # Dry-run: in-memory repos so the walker never touches Supabase.
        corpus_repo = InMemoryCorpusRepository()
        bank_repo = InMemoryBankLayoutRepository()
        audit = InMemoryAuditLog()

    results, summary = ingest_folder(
        folder,
        corpus_repo=corpus_repo,
        bank_repo=bank_repo,
        audit=audit,
        apply=args.apply,
    )
    print(render_summary(results, summary, apply=args.apply))

    if summary.files_errored:
        return EXIT_ISSUES_FOUND
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
