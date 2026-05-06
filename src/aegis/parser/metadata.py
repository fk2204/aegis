"""PDF tampering signal detection via pikepdf.

Inspects metadata (producer, creator, author, dates), counts %%EOF markers
(>1 means incremental save -> tampering signal), checks startxref offset
points at a valid xref or object header, and flags page-size inconsistency
between pages of the same document.

Returns a `MetadataAnalysis` with a 0..100 fraud subscore consumed by
`pipeline.py`.

Bug fix vs TS version
---------------------
The TS author-detection regex flagged any "Capitalized Capitalized" string
as a personal name, which incorrectly tags "Bank Of America" or "Wells
Fargo" as a personal author. Here we exclude common bank-name token sets
before flagging.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pikepdf

# Editors that show up in tampered PDFs more often than not.
_HARD_EDITORS = (
    "foxit phantompdf",
    "nitro pro",
    "pdfescape",
    "smallpdf",
    "ilovepdf",
    "sejda",
    "pdf-xchange editor",
    "cutepdf",
    "pdfill",
)

# Editors a merchant might legitimately use to view/save, but still a signal.
_MEDIUM_EDITORS = (
    "adobe acrobat",
    "preview",
    "microsoft word",
    "libreoffice",
    "openoffice",
    "google docs",
    "apple pages",
)

# Tokens that, when present, mean an "Author" string is almost certainly an
# institution name rather than a person. Used to suppress the personal-author
# flag (TS regex bug fix).
_INSTITUTION_TOKENS = (
    "bank",
    "credit union",
    "trust",
    "national",
    "savings",
    "financial",
    "wells fargo",
    "chase",
    "citi",
    "capital one",
    "pnc",
    "us bank",
    "regions",
    "truist",
    "bbva",
    "fifth third",
    "huntington",
    "td bank",
    "santander",
    "bmo",
    "ally",
)

_PERSONAL_NAME_PATTERNS = (
    re.compile(r"^[A-Z][a-z]+\s+[A-Z][a-z]+$"),  # First Last
    re.compile(r"^[A-Z][a-z]+\s+[A-Z]\.\s+[A-Z][a-z]+$"),  # First M. Last
    re.compile(r"^[A-Z][a-z]+\s+[A-Z][a-z]+\s+[A-Z][a-z]+$"),  # First Middle Last
)


class PdfEncryptedError(RuntimeError):
    """Raised when a PDF is encrypted and cannot be inspected."""


@dataclass
class MetadataAnalysis:
    pdf_creation_date: datetime | None
    pdf_modification_date: datetime | None
    pdf_producer: str | None
    pdf_creator: str | None
    pdf_author: str | None
    page_count: int
    file_size_bytes: int
    eof_markers: int
    page_sizes: list[str]
    flags: list[str] = field(default_factory=list)
    fraud_score: int = 0


def _looks_like_institution(author: str) -> bool:
    a = author.lower()
    return any(token in a for token in _INSTITUTION_TOKENS)


def _is_personal_author(author: str) -> bool:
    a = author.strip()
    if not a:
        return False
    if _looks_like_institution(a):
        return False
    if "@" in a:
        return True
    return any(p.match(a) for p in _PERSONAL_NAME_PATTERNS)


def _count_eof_markers(raw: bytes) -> int:
    """%%EOF markers > 1 imply incremental saves (tampering signal)."""
    return raw.count(b"%%EOF")


def _xref_offset_aligned(raw: bytes) -> bool:
    """The last `startxref N` should point at b"xref" or an object header.

    Hex-edited PDFs frequently have a stale offset that no longer aligns.
    """
    matches = list(re.finditer(rb"startxref\s+(\d+)", raw))
    if not matches:
        return True  # nothing to check
    last = matches[-1]
    declared = int(last.group(1))
    if declared <= 0 or declared >= len(raw):
        return True  # out-of-range offsets are caught elsewhere
    peek = raw[declared : declared + 16]
    if peek.startswith(b"xref"):
        return True
    # PDF object header pattern: e.g. b"42 0 obj"
    return bool(re.match(rb"^\d+\s+\d+\s+obj", peek))


def _coerce_dt(value: object) -> datetime | None:
    """pikepdf returns pdfdate strings; turn them into datetimes when possible."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value)
    # PDF date format: D:YYYYMMDDHHmmSS[+-]HH'mm'
    m = re.match(r"D:(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?", s)
    if not m:
        return None
    parts = [int(g) if g else 0 for g in m.groups()]
    try:
        return datetime(parts[0], parts[1], parts[2], parts[3], parts[4], parts[5])
    except ValueError:
        return None


def analyze_metadata(pdf_path: str | Path) -> MetadataAnalysis:
    """Inspect a PDF for tampering signals.

    Raises
    ------
    PdfEncryptedError
        If the PDF is encrypted (parser cannot proceed).
    """
    path = Path(pdf_path)
    raw = path.read_bytes()

    try:
        pdf = pikepdf.open(path)
    except pikepdf.PasswordError as exc:
        raise PdfEncryptedError(f"PDF is encrypted: {path}") from exc

    flags: list[str] = []
    score = 0

    # pikepdf's docinfo is a Dictionary[Name, Object] that supports `in`
    # and item access but doesn't expose a typed mapping protocol. Falling
    # back to Any here so we can mix `.get` and `[]` access without typing
    # the dozen pdf-object types we never need to reason about.
    docinfo: Any = pdf.docinfo or {}
    creation = _coerce_dt(docinfo.get("/CreationDate"))
    modification = _coerce_dt(docinfo.get("/ModDate"))
    producer = str(docinfo["/Producer"]) if "/Producer" in docinfo else None
    creator = str(docinfo["/Creator"]) if "/Creator" in docinfo else None
    author = str(docinfo["/Author"]) if "/Author" in docinfo else None

    eof_markers = _count_eof_markers(raw)
    if eof_markers > 1:
        flags.append(f"incremental_saves: {eof_markers} EOF markers")
        score += 40

    if creation and modification:
        gap_minutes = (modification - creation).total_seconds() / 60.0
        if gap_minutes > 120:
            flags.append(f"modified_{int(gap_minutes)}min_after_creation")
            score += 30
        elif gap_minutes > 5:
            flags.append(f"modified_{int(gap_minutes)}min_after_creation")
            score += 15

    if producer:
        prod_lower = producer.lower()
        if any(e in prod_lower for e in _HARD_EDITORS):
            flags.append(f"editor_detected: {producer}")
            score += 35
        elif any(e in prod_lower for e in _MEDIUM_EDITORS):
            flags.append(f"editor_detected: {producer}")
            score += 15

    if author and _is_personal_author(author):
        flags.append(f"personal_author: {author}")
        score += 20

    if not creation and not producer and not creator:
        flags.append("stripped_metadata")
        score += 28

    pages = list(pdf.pages)
    page_sizes: list[str] = []
    for p in pages:
        mediabox = p.mediabox
        # mediabox is [llx, lly, urx, ury]; size is (urx-llx, ury-lly)
        try:
            width = round(float(mediabox[2]) - float(mediabox[0]))
            height = round(float(mediabox[3]) - float(mediabox[1]))
            page_sizes.append(f"{width}x{height}")
        except (IndexError, TypeError, ValueError):
            page_sizes.append("unknown")
    if len(set(page_sizes)) > 1:
        flags.append(f"page_size_inconsistency: {', '.join(sorted(set(page_sizes)))}")
        score += 30

    if not _xref_offset_aligned(raw):
        flags.append("xref_offset_mismatch")
        score += 25

    pdf.close()

    return MetadataAnalysis(
        pdf_creation_date=creation,
        pdf_modification_date=modification,
        pdf_producer=producer,
        pdf_creator=creator,
        pdf_author=author,
        page_count=len(pages),
        file_size_bytes=len(raw),
        eof_markers=eof_markers,
        page_sizes=page_sizes,
        flags=flags,
        fraud_score=min(100, score),
    )


__all__ = ["MetadataAnalysis", "PdfEncryptedError", "analyze_metadata"]
