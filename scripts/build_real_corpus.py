"""Build the real-statement test corpus from prod ``pdf_store``.

Pulls every document with ``parse_status='proceed'`` AND a sealed PDF
in ``pdf_store`` (the encrypted blob — operator-real statements that
already parsed clean), decrypts each via ``PdfStoreRepository.
fetch_plaintext``, redacts PII on the PDF text layer via pymupdf
(``page.add_redact_annot`` + ``apply_redactions``), runs a literal-
substring canary against the original merchant PII, and writes:

* ``tests/corpus/real/{bank_slug}_{doc_id_first8}.pdf`` — sanitized PDF
* ``tests/corpus/real/{bank_slug}_{doc_id_first8}.expected.json`` —
  expected parser outputs (true_revenue ± 20%, exact nsf_count,
  confirmed-MCA-positions count, period_months, bank_name).

Canary failures (any original PII string still present in the
sanitized PDF text) are written to ``tests/corpus/real/skipped.log``
with a reason — the PDF is NOT written, the operator iterates.

Per CLAUDE.md, ``tests/corpus/real/`` is .gitignored; the build
script is committed but its output never enters git history (real
bank statement layouts even after redaction are too sensitive).

Usage::

    # Inventory only — no writes; shows include/skip plan.
    python scripts/build_real_corpus.py --dry-run

    # Build the corpus.
    python scripts/build_real_corpus.py --apply

    # Cap to N documents (useful for first runs).
    python scripts/build_real_corpus.py --apply --limit 5

    # Re-process one specific document by ID prefix.
    python scripts/build_real_corpus.py --apply --doc-id abc12345

Requires the operator's PDF encryption key in env — set
``PDF_ENCRYPTION_KEY_V{n}`` per the standard ``aegis.env`` shape.
Without it the fetch_plaintext call surfaces a clear error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, cast
from uuid import UUID

# pymupdf import: project deps already include ``pymupdf>=1.24``.
import fitz

# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------


REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
CORPUS_DIR: Final[Path] = REPO_ROOT / "tests" / "corpus" / "real"
SKIP_LOG: Final[Path] = CORPUS_DIR / "skipped.log"


# Account-number patterns. The 8-17 digit run catches DDA / savings;
# the 16-digit grouped form catches card numbers. Both bucket to the
# same placeholder so the parser still sees a numeric-looking token
# of similar shape.
_ACCT_GROUPED_RE: Final[re.Pattern[str]] = re.compile(r"\b\d{4}[\s-]\d{4}[\s-]\d{4}[\s-]\d{4}\b")
_ACCT_LONG_RE: Final[re.Pattern[str]] = re.compile(r"\b\d{8,17}\b")
# 9-digit routing numbers start with 0/1/2/3 (Fed ABA). We narrow to
# the leading-zero form per the operator's spec; widening later if
# needed is harmless because the placeholder shape is the same.
_ROUTING_RE: Final[re.Pattern[str]] = re.compile(r"\b0[0-9]{8}\b")
# Phone — North American shapes. The PDF text layer often carries
# spaces or punctuation between the area code and the line; the
# pattern accepts the common variants.
_PHONE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b"
)
# Email — RFC-loose; deliberately lenient.
_EMAIL_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)
# Street-address heuristic — "<num> <street word>". Catches most bank-
# statement address lines without false-matching account history that
# happens to lead with a number; the ``CR|CT|RD|...`` suffix set is
# the disambiguator.
_ADDRESS_RE: Final[re.Pattern[str]] = re.compile(
    r"\b\d{1,6}\s+[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?\s+"
    r"(?:STREET|ST|AVENUE|AVE|ROAD|RD|BOULEVARD|BLVD|DRIVE|DR|LANE|LN|"
    r"COURT|CT|CIRCLE|CIR|PLACE|PL|WAY|HIGHWAY|HWY|TERRACE|TER|PARKWAY|PKWY)"
    r"(?:\s+(?:NORTH|SOUTH|EAST|WEST|N|S|E|W|NE|NW|SE|SW))?\b",
    re.IGNORECASE,
)


# Replacement placeholders. Kept structurally similar to the redacted
# value so the parser's downstream tokenizers don't choke on a wildly
# different shape (e.g. an address line still occupies a line; an
# account number still looks like a numeric token).
_ACCT_PLACEHOLDER: Final[str] = "XXXX-XXXX"
_ROUTING_PLACEHOLDER: Final[str] = "XXXXXXXXX"
_OWNER_PLACEHOLDER: Final[str] = "[OWNER NAME]"
_MERCHANT_PLACEHOLDER: Final[str] = "[MERCHANT NAME]"
_PHONE_PLACEHOLDER: Final[str] = "[PHONE]"
_EMAIL_PLACEHOLDER: Final[str] = "[EMAIL]"
_ADDRESS_PLACEHOLDER: Final[str] = "[ADDRESS]"


# ----------------------------------------------------------------------
# Candidate inventory
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One document AEGIS knows parsed clean and stored sealed."""

    document_id: UUID
    merchant_id: UUID
    bank_name: str
    parse_status: str
    original_filename: str | None
    storage_path: str | None  # truthy = pdf_store seal exists


def _slug(value: str) -> str:
    """Filesystem-safe bank slug — lowercase + underscores."""
    s = re.sub(r"[^A-Za-z0-9]+", "_", value.lower()).strip("_")
    return s or "unknown_bank"


def _doc_id_prefix(doc_id: UUID) -> str:
    return doc_id.hex[:8]


def discover_candidates() -> list[Candidate]:
    """Return every document that's eligible for the real corpus."""
    # Local import keeps boot-time deps (boto3 / supabase env) deferred.
    from aegis.db import get_supabase

    sb = get_supabase()
    docs_resp = (
        sb.table("documents")
        .select("id,parse_status,storage_path,merchant_id,original_filename")
        .execute()
    )
    analyses_resp = sb.table("analyses").select("document_id,bank_name").execute()
    docs_rows = cast(list[dict[str, Any]], docs_resp.data or [])
    analyses_rows = cast(list[dict[str, Any]], analyses_resp.data or [])
    bank_by_doc: dict[str, str] = {
        a["document_id"]: (a.get("bank_name") or "unknown") for a in analyses_rows
    }

    out: list[Candidate] = []
    for d in docs_rows:
        if d.get("parse_status") != "proceed":
            continue
        storage_path = d.get("storage_path")
        if not storage_path:
            continue
        merchant_id = d.get("merchant_id")
        if merchant_id is None:
            # No merchant link → can't pull PII strings to redact.
            continue
        original_filename = d.get("original_filename")
        out.append(
            Candidate(
                document_id=UUID(d["id"]),
                merchant_id=UUID(merchant_id),
                bank_name=bank_by_doc.get(d["id"], "unknown"),
                parse_status="proceed",
                original_filename=(
                    original_filename if isinstance(original_filename, str) else None
                ),
                storage_path=storage_path if isinstance(storage_path, str) else None,
            )
        )
    return out


# ----------------------------------------------------------------------
# PII discovery + sanitization
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class MerchantPII:
    """Literal strings the canary uses + the sanitizer searches for."""

    business_name: str | None
    owner_name: str | None
    email: str | None
    phone: str | None
    address_lines: tuple[str, ...]  # MerchantRow has no explicit address; kept empty for now


def merchant_pii(merchant_id: UUID) -> MerchantPII:
    from aegis.api.deps import get_merchant_repository

    repo = get_merchant_repository()
    merchant = repo.get(merchant_id)
    return MerchantPII(
        business_name=merchant.business_name,
        owner_name=merchant.owner_name,
        email=merchant.email,
        phone=merchant.phone,
        address_lines=(),
    )


def _pii_literal_targets(pii: MerchantPII) -> list[str]:
    """The exact strings to search-and-redact (case-insensitive)."""
    out: list[str] = []
    for s in (pii.business_name, pii.owner_name, pii.email, pii.phone):
        if s and s.strip():
            out.append(s.strip())
    return out


def _redact_by_search(page: Any, needle: str, placeholder: str) -> int:  # noqa: ANN401 — pymupdf Page is untyped
    """Add a redaction over every occurrence of ``needle`` on the page.

    Returns the number of bboxes covered.
    """
    if not needle:
        return 0
    rects = page.search_for(needle, quads=False)
    for rect in rects:
        page.add_redact_annot(rect, text=placeholder, fill=(1, 1, 1))
    return len(rects)


def _redact_by_regex(page: Any, pattern: re.Pattern[str], placeholder: str) -> int:  # noqa: ANN401 — pymupdf Page is untyped
    """Find regex matches in the page text, then search_for each literal hit."""
    txt = page.get_text("text") or ""
    seen: set[str] = set()
    count = 0
    for m in pattern.finditer(txt):
        hit = m.group(0)
        if hit in seen:
            continue
        seen.add(hit)
        count += _redact_by_search(page, hit, placeholder)
    return count


def sanitize_pdf(plaintext: bytes, pii: MerchantPII) -> bytes:
    """Return a redacted copy of the PDF as bytes.

    Order matters: account/routing patterns first (longest tokens win
    so a 16-digit card number isn't half-redacted by the 8-17 long
    pattern), then phone/email/address, then named PII (business +
    owner — case-insensitive search_for handles capitalization drift).
    """
    doc = fitz.open(stream=plaintext, filetype="pdf")
    try:
        for page in doc:
            _redact_by_regex(page, _ACCT_GROUPED_RE, _ACCT_PLACEHOLDER)
            _redact_by_regex(page, _ACCT_LONG_RE, _ACCT_PLACEHOLDER)
            _redact_by_regex(page, _ROUTING_RE, _ROUTING_PLACEHOLDER)
            _redact_by_regex(page, _PHONE_RE, _PHONE_PLACEHOLDER)
            _redact_by_regex(page, _EMAIL_RE, _EMAIL_PLACEHOLDER)
            _redact_by_regex(page, _ADDRESS_RE, _ADDRESS_PLACEHOLDER)

            if pii.business_name:
                _redact_by_search(page, pii.business_name, _MERCHANT_PLACEHOLDER)
                # Also upper / title variants — banks normalize differently.
                _redact_by_search(page, pii.business_name.upper(), _MERCHANT_PLACEHOLDER)
            if pii.owner_name:
                _redact_by_search(page, pii.owner_name, _OWNER_PLACEHOLDER)
                _redact_by_search(page, pii.owner_name.upper(), _OWNER_PLACEHOLDER)
            if pii.email:
                _redact_by_search(page, pii.email, _EMAIL_PLACEHOLDER)
            if pii.phone:
                _redact_by_search(page, pii.phone, _PHONE_PLACEHOLDER)

            # ``images=fitz.PDF_REDACT_IMAGE_NONE`` keeps raster images
            # intact — most bank statements have a logo we want to
            # preserve so the vision parser still routes correctly.
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
        out = doc.tobytes(garbage=4, deflate=True)
        return bytes(out)
    finally:
        doc.close()


# ----------------------------------------------------------------------
# Canary
# ----------------------------------------------------------------------


def pdf_text(pdf_bytes: bytes) -> str:
    """Concatenate the text layer across all pages."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return "\n".join((page.get_text("text") or "") for page in doc)
    finally:
        doc.close()


def canary_violations(sanitized_text: str, pii: MerchantPII) -> list[str]:
    """Return a list of original-PII substrings still present.

    Empty list = canary passed. Any entry = leak.
    """
    hits: list[str] = []
    haystack = sanitized_text.lower()
    for needle in _pii_literal_targets(pii):
        if needle.lower() in haystack:
            hits.append(needle)
    return hits


# ----------------------------------------------------------------------
# Expected outputs
# ----------------------------------------------------------------------


def _confirmed_mca_positions(document_id: UUID) -> int:
    """Read pattern_analysis.mca_positions and count confirmed bucket.

    Pre-2026-06-26 persisted rows default ``match_source`` to
    ``known_funder`` (conservative) so they count as confirmed; new
    parses populate the field accurately.
    """
    from aegis.api.deps import get_repository

    repo = get_repository()
    analysis = repo.get_analysis(document_id)
    if analysis is None or analysis.pattern_analysis is None:
        return 0
    return sum(
        1
        for p in analysis.pattern_analysis.mca_positions
        if getattr(p, "match_source", "known_funder") == "known_funder"
    )


def _period_months(start: date | None, end: date | None) -> int:
    """Rough month-count for the statement period."""
    if start is None or end is None:
        return 0
    days = (end - start).days + 1
    return max(1, round(days / 30))


def expected_outputs(candidate: Candidate) -> dict[str, Any] | None:
    """Build the expected.json payload from the prod analysis row."""
    from aegis.api.deps import get_repository

    repo = get_repository()
    analysis = repo.get_analysis(candidate.document_id)
    if analysis is None:
        return None

    # ±20% range on monthly revenue, integer-rounded to the dollar.
    monthly = Decimal(str(analysis.monthly_revenue))
    low = int((monthly * Decimal("0.80")).quantize(Decimal("1")))
    high = int((monthly * Decimal("1.20")).quantize(Decimal("1")))

    return {
        "bank_name": candidate.bank_name,
        "expected_parse_status": "proceed",
        "expected_revenue_min": low,
        "expected_revenue_max": high,
        "expected_nsf_count": int(analysis.num_nsf),
        "expected_mca_positions_confirmed": _confirmed_mca_positions(candidate.document_id),
        "period_months": _period_months(
            analysis.statement_period_start, analysis.statement_period_end
        ),
        "doc_id_prefix": _doc_id_prefix(candidate.document_id),
    }


# ----------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------


@dataclass
class BuildResult:
    included: list[str]
    skipped_pii: list[tuple[str, list[str]]]
    skipped_other: list[tuple[str, str]]


def _iter_filtered(
    candidates: list[Candidate],
    *,
    limit: int | None,
    doc_id_filter: str | None,
) -> Iterator[Candidate]:
    seen = 0
    for c in candidates:
        if doc_id_filter and not c.document_id.hex.startswith(doc_id_filter.lower()):
            continue
        yield c
        seen += 1
        if limit is not None and seen >= limit:
            return


def run(
    *,
    dry_run: bool,
    limit: int | None,
    doc_id_filter: str | None,
) -> BuildResult:
    from aegis.api.deps import get_pdf_store_repository

    candidates = discover_candidates()
    pdf_store = get_pdf_store_repository()

    if not dry_run:
        CORPUS_DIR.mkdir(parents=True, exist_ok=True)
        SKIP_LOG.unlink(missing_ok=True)

    included: list[str] = []
    skipped_pii: list[tuple[str, list[str]]] = []
    skipped_other: list[tuple[str, str]] = []

    for c in _iter_filtered(candidates, limit=limit, doc_id_filter=doc_id_filter):
        slug = _slug(c.bank_name)
        prefix = _doc_id_prefix(c.document_id)
        stem = f"{slug}_{prefix}"
        try:
            pii = merchant_pii(c.merchant_id)
        except Exception as exc:
            skipped_other.append((stem, f"merchant_pii_lookup_failed: {exc}"))
            continue

        if dry_run:
            included.append(
                f"{stem}  bank={c.bank_name!r}  "
                f"business={pii.business_name!r}  owner={pii.owner_name!r}"
            )
            continue

        try:
            plaintext = pdf_store.fetch_plaintext(c.document_id)
        except Exception as exc:
            skipped_other.append((stem, f"pdf_store_fetch_failed: {exc}"))
            continue

        try:
            sanitized = sanitize_pdf(plaintext, pii)
        except Exception as exc:
            skipped_other.append((stem, f"sanitize_failed: {exc}"))
            continue

        # Canary: re-extract text, search for original PII strings.
        try:
            sanitized_text = pdf_text(sanitized)
        except Exception as exc:
            skipped_other.append((stem, f"text_extract_failed: {exc}"))
            continue
        violations = canary_violations(sanitized_text, pii)
        if violations:
            skipped_pii.append((stem, violations))
            continue

        expected = expected_outputs(c)
        if expected is None:
            skipped_other.append((stem, "analysis_row_missing"))
            continue

        pdf_path = CORPUS_DIR / f"{stem}.pdf"
        json_path = CORPUS_DIR / f"{stem}.expected.json"
        pdf_path.write_bytes(sanitized)
        json_path.write_text(json.dumps(expected, indent=2, sort_keys=True), encoding="utf-8")
        included.append(stem)

    if not dry_run and (skipped_pii or skipped_other):
        with SKIP_LOG.open("w", encoding="utf-8") as fh:
            for stem, hits in skipped_pii:
                fh.write(f"{stem}\tPII_CANARY_FAILED\t{hits!r}\n")
            for stem, reason in skipped_other:
                fh.write(f"{stem}\tOTHER\t{reason}\n")

    return BuildResult(
        included=included,
        skipped_pii=skipped_pii,
        skipped_other=skipped_other,
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Inventory only — show include/skip plan; do not write files.",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Write the sanitized PDFs and expected.json files.",
    )
    p.add_argument("--limit", type=int, default=None)
    p.add_argument(
        "--doc-id",
        type=str,
        default=None,
        help="Filter to documents whose UUID hex starts with this prefix.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv or sys.argv[1:]))
    if not (args.dry_run or args.apply):
        print("error: pass --dry-run or --apply", file=sys.stderr)
        return 2

    result = run(dry_run=args.dry_run, limit=args.limit, doc_id_filter=args.doc_id)

    print(f"included: {len(result.included)}")
    for line in result.included:
        print(f"  + {line}")
    if result.skipped_pii:
        print(f"skipped (PII canary failed): {len(result.skipped_pii)}")
        for stem, hits in result.skipped_pii:
            print(f"  ! {stem}  hits={hits!r}")
    if result.skipped_other:
        print(f"skipped (other): {len(result.skipped_other)}")
        for stem, reason in result.skipped_other:
            print(f"  - {stem}  reason={reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
