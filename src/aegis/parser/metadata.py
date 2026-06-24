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
from typing import Any, Final

import pikepdf
import pymupdf

# Editors / toolchains that show up in tampered PDFs more often than not.
# `reportlab` is INTENTIONALLY excluded: AEGIS's synthetic corpus is built
# with reportlab and the library leaks "ReportLab PDF Library - (opensource)"
# in /Producer even when invariant=True is set, which would flag every
# corpus PDF as a hard editor signal. If a real merchant statement ever
# arrives with reportlab in /Producer, the medium-editor heuristic plus
# stripped-metadata / personal-author signals still fire.
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
    "itext",
    "pdflib",
    "pypdf2",
    "pypdf",
    "ghostscript",
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

# Per the PDF spec, every trailer ends with `%%EOF` followed by EOL or
# end-of-file. A naive `b"%%EOF"` substring count was over-reporting because
# the byte sequence can appear inside content streams or inline binary
# (font programs, embedded images). Anchoring the match on whitespace +
# (newline | end-of-file) restricts the count to genuine trailer ends.
_EOF_PATTERN = re.compile(rb"%%EOF\s*(?:\r\n|\r|\n|\Z)")


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
    # True when the first few pages yield enough extractable text to suggest
    # the document has a real text layer; False when the PDF is image-only
    # (scanned, photo-of-screen, password-stripped export that lost its text
    # layer). The parser pipeline reads this BEFORE pass 1 so it can route
    # image-only PDFs through a vision-based extraction instead of wasting
    # a text-extraction call. Default True so any detection failure falls
    # back to the existing text path (conservative — never hides a real
    # text PDF behind OCR by accident).
    has_text_layer: bool = True
    # Forensic layer (2026-06-24, master plan §6.4 follow-up). True when
    # the row-level font-consistency detector found a page whose
    # transaction spans don't match the page's modal font / size. This
    # is distinct from the existing page-level ``font_inconsistency``
    # flag, which compares one page's font set to OTHER pages' fonts.
    # See ``aegis.parser.forensic.font_consistency`` for the algorithm.
    font_inconsistency_detected: bool = False
    # Forensic layer #2 (2026-06-24). True when the PDF's /Creator
    # string matches a known editing-tool family AND does NOT match the
    # identified bank's known-good creator patterns (per
    # ``forensic.creator_fingerprint.KNOWN_CREATOR_PATTERNS``). The
    # check runs AFTER extraction (because it needs ``bank_name``),
    # so this field is populated by ``parser.pipeline.run_pipeline``
    # rather than ``analyze_metadata``.
    creator_mismatch_detected: bool = False
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
    """%%EOF markers > 1 imply incremental saves (tampering signal).

    Uses a regex anchored on EOL / end-of-file so we only count the marker
    when it appears as a real trailer terminator, not as an incidental byte
    sequence inside a content stream or font binary.
    """
    return len(_EOF_PATTERN.findall(raw))


def _has_pdf_signature(pdf: pikepdf.Pdf) -> bool:
    """Return True iff the PDF contains a digital-signature field.

    Walks AcroForm.Fields (and any nested Kids) looking for /FT == /Sig.
    Detection is presence-based — we do NOT validate the signature
    cryptographically. The EOF false-positive fix accepts this tradeoff:
    every digitally-signed PDF carries ≥2 EOF markers by design (each
    signature appends a %%EOF trailer as an incremental update), and major
    bank exports / KYC issuers commonly sign their outputs. Treating
    "signed + multi-EOF" as legitimate and "unsigned + multi-EOF" as
    suspicious matches the realistic threat model. Cryptographic
    validation is left for v2 (requires a signing library / new
    dependency); presence detection cuts the false-positive class on
    legitimate signed exports.
    """
    try:
        root: Any = pdf.Root
    except (pikepdf.PdfError, AttributeError, KeyError):
        return False
    if root is None or not hasattr(root, "__contains__"):
        return False
    try:
        if "/AcroForm" not in root:
            return False
        acroform: Any = root["/AcroForm"]
    except (pikepdf.PdfError, KeyError, ValueError, TypeError):
        return False
    if acroform is None or not hasattr(acroform, "get"):
        return False
    try:
        fields: Any = acroform.get("/Fields")
    except (pikepdf.PdfError, KeyError, ValueError):
        return False
    if not fields:
        return False

    # BFS over fields (with /Kids descent) looking for /FT == /Sig.
    try:
        queue: list[Any] = list(fields)
    except (pikepdf.PdfError, TypeError):
        return False
    while queue:
        field = queue.pop()
        try:
            ft = field.get("/FT")
        except (pikepdf.PdfError, KeyError, ValueError, AttributeError):
            ft = None
        if ft is not None and str(ft) == "/Sig":
            return True
        try:
            kids = field.get("/Kids")
        except (pikepdf.PdfError, KeyError, ValueError, AttributeError):
            kids = None
        if kids:
            try:
                queue.extend(list(kids))
            except (pikepdf.PdfError, TypeError):
                continue
    return False


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


# Minimum non-whitespace characters across the first probe-window pages
# required to count as "has a text layer." Real bank statements yield
# hundreds-to-thousands of characters per page; the floor of 50 protects
# against a single watermark glyph or page-number text in an otherwise
# image-only PDF flipping us back to the text path.
_TEXT_LAYER_MIN_CHARS: Final[int] = 50
_TEXT_LAYER_PROBE_PAGES: Final[int] = 3


def _detect_text_layer(pdf_path: str | Path) -> bool:
    """Return True iff the first few pages contain extractable text.

    Defaults to True on any exception so an unexpected pymupdf failure
    never silently hides a legitimate text-bearing PDF behind the OCR
    fallback (which is slower and costs more tokens).
    """
    try:
        with pymupdf.open(pdf_path) as doc:  # type: ignore[no-untyped-call]
            pages_to_probe = min(_TEXT_LAYER_PROBE_PAGES, doc.page_count)
            for i in range(pages_to_probe):
                text = doc.load_page(i).get_text("text") or ""
                # Strip whitespace — a page of blank lines is not a text layer.
                if sum(1 for ch in text if not ch.isspace()) >= _TEXT_LAYER_MIN_CHARS:
                    return True
            return False
    except Exception:
        return True


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
    if eof_markers > 1 and not _has_pdf_signature(pdf):
        # Digitally-signed PDFs use incremental updates by design — every
        # signature appends a %%EOF trailer. Suppressing the flag when a
        # signature is present cuts the false-positive class on legitimate
        # signed bank exports and KYC documents. Unsigned multi-EOF PDFs
        # still flag at the original severity.
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

    # Phase 9 forensics (master plan §6.4 forensic layer).
    font_flag, font_score = _font_inconsistency(pdf)
    if font_flag:
        flags.append(font_flag)
        score += font_score
    layer_flag, layer_score = _page_layer_anomaly(pdf)
    if layer_flag:
        flags.append(layer_flag)
        score += layer_score

    pdf.close()

    has_text_layer = _detect_text_layer(path)

    # Row-level font consistency (2026-06-24 forensic layer extension).
    # Runs after the pikepdf-based metadata checks because it uses
    # pymupdf (separate document open) and reads per-text-run font/size,
    # which pikepdf doesn't surface cleanly. Conservative fallback to
    # no-flag on any failure — never fail a parse on a forensic
    # detector error.
    from aegis.parser.forensic.font_consistency import (
        analyze as _font_consistency_analyze,
    )

    font_consistency = _font_consistency_analyze(path)
    if font_consistency.inconsistency_detected:
        flags.append(
            f"font_inconsistency_detected: {font_consistency.affected_page_count} "
            f"page(s); modal={font_consistency.modal_font or '?'}"
        )
        # Contribution mirrors ``_page_layer_anomaly`` (+15) — these are
        # the same band of forensic signal strength. The contribution
        # adds to metadata_score, which then gets weighted by
        # FRAUD_WEIGHTS["metadata"] in pipeline.py — no separate
        # FRAUD_WEIGHTS key needed, see the comment near FRAUD_WEIGHTS
        # for the wiring rationale.
        score += 15

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
        has_text_layer=has_text_layer,
        font_inconsistency_detected=font_consistency.inconsistency_detected,
        flags=flags,
        fraud_score=min(100, score),
    )


def _extract_page_fonts(page: pikepdf.Page) -> set[str]:
    """Return the set of /BaseFont names referenced by ``page``.

    Empty set when the page has no font resources (image-only, watermark
    only, or any pikepdf accessor failure). pikepdf raises a mix of
    PdfError, KeyError, ValueError on malformed object graphs; we treat
    any of them as "no fonts extracted" — the calling detector counts
    pages by font-overlap, so a silent miss simply lowers sensitivity
    rather than skewing the result.
    """
    names: set[str] = set()
    # pikepdf objects are weakly typed in stubs; Any avoids fighting
    # Page.get's overload with default args. Narrower except clauses
    # to keep ruff S/BLE rules satisfied.
    try:
        resources: Any = page.get("/Resources")
    except (pikepdf.PdfError, KeyError, ValueError):
        return names
    if resources is None or not hasattr(resources, "get"):
        return names
    try:
        fonts: Any = resources.get("/Font")
    except (pikepdf.PdfError, KeyError, ValueError):
        return names
    if not fonts:
        return names
    for _, font_obj in fonts.items():
        try:
            base = font_obj.get("/BaseFont")
        except (pikepdf.PdfError, KeyError, ValueError):
            continue
        if base is not None:
            names.add(str(base))
    return names


def _font_inconsistency(pdf: pikepdf.Pdf) -> tuple[str | None, int]:
    """Flag pages whose font set differs significantly from the document baseline.

    Bank statements typically use one consistent font family across every
    page. A page with a markedly different font subset is a signal that
    content was pasted in from an external source (text-modification
    tampering). We count distinct embedded-font base names per page; a
    page whose font set has zero overlap with the union of fonts seen on
    other pages flags as inconsistent.

    Single-page PDFs return None — no inter-page comparison possible.
    Returns ``(flag, score_delta)`` or ``(None, 0)`` when no anomaly.
    """
    try:
        pages = list(pdf.pages)
    except (pikepdf.PdfError, RuntimeError, ValueError, KeyError):
        return None, 0
    if len(pages) < 2:
        return None, 0

    per_page_fonts: list[set[str]] = []
    for p in pages:
        names = _extract_page_fonts(p)
        per_page_fonts.append(names)

    # Skip when no fonts are extractable at all (image-only PDFs).
    if not any(per_page_fonts):
        return None, 0

    inconsistent_pages = 0
    for i, this_page in enumerate(per_page_fonts):
        if not this_page:
            continue
        other_union: set[str] = set().union(*(s for j, s in enumerate(per_page_fonts) if j != i))
        if other_union and not (this_page & other_union):
            inconsistent_pages += 1

    if inconsistent_pages == 0:
        return None, 0
    return (
        f"font_inconsistency: {inconsistent_pages} page(s) have no font overlap",
        20,
    )


def _page_layer_anomaly(pdf: pikepdf.Pdf) -> tuple[str | None, int]:
    """Detect pages whose content-stream count differs from siblings.

    Pages assembled by overlaying one PDF onto another often carry
    multiple ``/Contents`` streams while the rest of the document uses
    a single stream per page. The variance is not conclusive (some
    legit exports also split streams) but per master plan §6.4 it
    contributes to the multi-layer fraud composite, not as a hard
    decline.
    """
    try:
        pages = list(pdf.pages)
    except (pikepdf.PdfError, RuntimeError, ValueError, KeyError):
        return None, 0
    if len(pages) < 2:
        return None, 0

    stream_counts: list[int] = []
    for p in pages:
        try:
            contents: Any = p.get("/Contents")
        except (pikepdf.PdfError, KeyError, ValueError):
            stream_counts.append(-1)
            continue
        if contents is None:
            stream_counts.append(0)
        elif isinstance(contents, pikepdf.Array):
            stream_counts.append(len(contents))
        else:
            stream_counts.append(1)

    # Anomaly: any page has a stream count that disagrees with the mode.
    valid_counts = [c for c in stream_counts if c >= 0]
    if len(set(valid_counts)) <= 1:
        return None, 0
    # Identify minority value(s); flag if at least one but not all pages
    # disagree.
    counter: dict[int, int] = {}
    for c in valid_counts:
        counter[c] = counter.get(c, 0) + 1
    if len(counter) < 2:
        return None, 0
    mode_count = max(counter.values())
    odd = sum(v for c, v in counter.items() if v != mode_count)
    if odd == 0:
        return None, 0
    return (
        f"page_layer_anomaly: {odd} page(s) have an off-mode /Contents stream count",
        15,
    )


__all__ = ["MetadataAnalysis", "PdfEncryptedError", "analyze_metadata"]
