"""Detect whether a PDF is a Stripe or Square processor statement.

The detector is **deterministic and pure**: it inspects the first few
pages of extractable text for processor-specific signatures (brand
names, distinctive heading phrases). It does NOT call the LLM —
deciding which extractor to invoke shouldn't cost a Bedrock token,
and a wrong call here would route the document down a parser that
returns nothing useful.

Three outcomes:
    - "stripe"    — Stripe signatures present, no Square signatures.
    - "square"    — Square signatures present, no Stripe signatures.
    - "bank"      — neither set of signatures found (default; the
                    upload route falls back to the bank pipeline).
    - "ambiguous" — BOTH Stripe and Square signatures present in the
                    same document. The upload route fails closed here
                    rather than guessing.

The hit list lives in this module so signature drift is one place to
update, not scattered across the codebase.

Filename-only routing
---------------------
``detect_processor_from_filename`` complements the content detector
for the CSV path. Stripe Dashboard exports the balance-transactions
CSV with a predictable filename shape (``balance_transactions_2026-03.csv``,
``stripe-payouts-Q1.csv`` etc.); the upload route uses the filename
hit to skip the content sniff entirely for CSVs (where pymupdf
wouldn't work anyway). The token list is intentionally small and
case-insensitive — broad enough to catch the common shapes, narrow
enough that a random bank statement named "Stripe Mall - Mar.pdf"
doesn't accidentally route to the processor pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import pymupdf

from aegis.logger import get_logger

_log = get_logger(__name__)


ProcessorBrand = Literal["stripe", "square", "clover", "bank", "ambiguous"]


# Stripe statement signatures. The "stripe.com" + "Activity summary"
# pair is the load-bearing combination — random PDFs that mention
# Stripe in passing don't have both. Brand name on its own is too
# loose; the "Activity summary" / "Gross volume" / "Net volume"
# vocabulary is what tips it from "mentions Stripe" to "this IS a
# Stripe statement."
_STRIPE_SIGNATURES: Final[tuple[str, ...]] = (
    "stripe.com",
    "Stripe, Inc.",
    "Activity summary",
    "Gross volume",
    "Net volume",
)

# Square statement signatures. squareup.com URL + "Sales summary"
# heading + the brand block at top of statement.
_SQUARE_SIGNATURES: Final[tuple[str, ...]] = (
    "squareup.com",
    "Square, Inc.",
    "Sales summary",
    "Block, Inc.",  # Square's parent company name on newer statements
    "Net total",
)

# How many pages to scan for signatures. Processor statements print
# the brand and activity summary on page 1; we probe up to two pages
# to allow for cover-page variants without paying the full document
# scan cost.
_DETECTION_PROBE_PAGES: Final[int] = 2

# Per-brand hit threshold. Below this, the brand is not considered
# confidently present even if one signature matched (e.g. a generic
# "Stripe" mention in a non-Stripe document).
_MIN_BRAND_HITS: Final[int] = 2


@dataclass(frozen=True)
class ProcessorDetection:
    """Outcome of detection + reasoning for the structured log.

    Frozen so the result is safe to pass around and compare in tests.
    ``stripe_hits`` / ``square_hits`` are the count of signature
    matches inside the probe window.
    """

    brand: ProcessorBrand
    stripe_hits: int
    square_hits: int


def detect_processor(pdf_path: str | Path) -> ProcessorDetection:
    """Inspect the first few pages and decide processor brand.

    Defaults to ``bank`` on any pymupdf failure — the bank pipeline is
    the safer fallback (it'll either parse successfully or surface the
    document for manual review, but it won't silently mishandle a
    Stripe/Square doc the way a misrouted processor pipeline could).
    """
    sample = _extract_probe_text(pdf_path)
    stripe_hits = sum(1 for sig in _STRIPE_SIGNATURES if sig in sample)
    square_hits = sum(1 for sig in _SQUARE_SIGNATURES if sig in sample)

    stripe_present = stripe_hits >= _MIN_BRAND_HITS
    square_present = square_hits >= _MIN_BRAND_HITS

    brand: ProcessorBrand
    if stripe_present and square_present:
        brand = "ambiguous"
    elif stripe_present:
        brand = "stripe"
    elif square_present:
        brand = "square"
    else:
        brand = "bank"

    return ProcessorDetection(
        brand=brand,
        stripe_hits=stripe_hits,
        square_hits=square_hits,
    )


# Filename tokens that mark a Stripe export. Case-insensitive substring
# match against the basename. Conservative on purpose — single-word
# matches against common nouns like "stripe" alone would false-positive
# on a merchant called "Stripe Mall Inc". The two-token requirement
# (``stripe`` plus one of the structural words ``balance``, ``payout``,
# ``transactions``) is the load-bearing combination.
_STRIPE_FILENAME_TOKENS: Final[tuple[str, ...]] = (
    "stripe",
    "balance_transaction",
    "balance-transaction",
    "payouts_",
    "payouts-",
    "stripe_payout",
    "stripe-payout",
)

# Filename tokens that explicitly carry Square. Wired for the Square
# CSV path. ``square-transactions`` is the dash-separated form some
# operators rename Dashboard exports to; ``square_transactions`` is the
# underscore variant. The brand token ``square`` covers the common case.
#
# Note: Square's Dashboard default export is named ``transactions_<date>.csv``
# WITHOUT a brand prefix — that's deliberately NOT in this list because it
# would collide with Stripe's ``balance_transactions_<date>.csv`` (the
# substring match in detect_processor_from_filename would mark both Stripe
# and Square as hit and the function would return ``ambiguous``, breaking
# the Stripe routing path). The CSV header sniff in
# ``detect_processor_from_csv_header`` is the correct discriminator for
# generically-named Square exports.
_SQUARE_FILENAME_TOKENS: Final[tuple[str, ...]] = (
    "square",
    "squareup",
    "square-transactions",
    "square_transactions",
)

# Clover filename tokens. Clover Dashboard exports default to
# ``clover_transactions_<date>.csv`` / ``clover-transactions-<date>.csv``;
# operators sometimes rename to ``clover_export.csv``. The brand token
# ``clover`` is the load-bearing common-case match. ``clover-transactions``
# / ``clover_transactions`` cover the dash-vs-underscore variants
# Dashboard ships with.
_CLOVER_FILENAME_TOKENS: Final[tuple[str, ...]] = (
    "clover",
    "clover-transactions",
    "clover_transactions",
    "clover_export",
    "clover-export",
)


# Square CSV header signature. The first 8 columns identify a Square
# transactions export deterministically:
#   Date,Time,Time Zone,Description,Amount,Fee,Net,Transaction ID
# Documented at the Square Help Center reference linked in
# ``csv_square.py``. Matched case-sensitive — Square dashboards
# preserve the header verbatim.
_SQUARE_CSV_HEADER_SIGNATURE: Final[str] = (
    "Date,Time,Time Zone,Description,Amount,Fee,Net,Transaction ID"
)

# Stripe balance-transactions CSV header. The first three columns
# (``id,Type,Source``) are the structural signature; Stripe adds
# optional trailing columns over time but the leading three are stable.
_STRIPE_CSV_HEADER_PREFIX: Final[str] = "id,Type,Source"

# Clover CSV header discriminator. ``Auth Code`` + ``Device ID`` is the
# Clover-unique combination — neither Stripe (``id,Type,Source,...``)
# nor Square (``Date,Time,Time Zone,...,Device Name,...``) carry an
# ``Auth Code`` column at all, and ``Device ID`` (terminal identifier)
# differs from Square's ``Device Name``. The combined presence is the
# load-bearing signal.
_CLOVER_CSV_REQUIRED_COLUMNS: Final[frozenset[str]] = frozenset({"Auth Code", "Device ID"})


def detect_processor_from_csv_header(header_line: str) -> ProcessorBrand:
    """Return the processor brand suggested by a CSV header line.

    Complements ``detect_processor_from_filename`` for the CSV upload
    path: a Square or Clover export whose filename was renamed to a
    generic ``export.csv`` still gets routed correctly when the header
    is inspected. Pure-string check; no I/O.

    ``"bank"`` means "no processor signature in the header"; the
    caller should refuse the upload (we don't know how to parse an
    untagged CSV).
    """
    if not header_line:
        return "bank"
    stripped = header_line.strip()
    # Strip UTF-8 BOM if the caller passed a raw bytes-decoded line.
    if stripped.startswith("﻿"):
        stripped = stripped[1:]
    square_hit = stripped.startswith(_SQUARE_CSV_HEADER_SIGNATURE)
    stripe_hit = stripped.startswith(_STRIPE_CSV_HEADER_PREFIX)
    # Clover header lives anywhere in the header line (Clover doesn't
    # mandate column order strictly), so we look for the discriminating
    # combination of ``Auth Code`` AND ``Device ID`` as substring hits.
    clover_columns = {c.strip() for c in stripped.split(",")}
    clover_hit = _CLOVER_CSV_REQUIRED_COLUMNS.issubset(clover_columns)
    hits = sum([square_hit, stripe_hit, clover_hit])
    if hits > 1:
        return "ambiguous"
    if clover_hit:
        return "clover"
    if square_hit:
        return "square"
    if stripe_hit:
        return "stripe"
    return "bank"


def detect_processor_from_filename(filename: str | Path) -> ProcessorBrand:
    """Return the processor brand suggested by a filename, or ``"bank"``.

    Pure-string check; no I/O. Matches case-insensitive substrings
    against the basename only (path components ignored). The CSV
    upload route uses this for the filename-first routing path the
    operator spec calls for; the content sniff (``detect_processor``)
    still runs on PDFs as a second confirmation.

    "bank" means "the filename does NOT carry a processor signature";
    the caller should fall back to ``detect_processor`` (PDF) or fail
    closed (CSV without a processor signature in the filename — we
    don't know what to do with a CSV that isn't tagged).
    """
    basename = Path(str(filename)).name.lower()
    stripe_hit = any(token in basename for token in _STRIPE_FILENAME_TOKENS)
    square_hit = any(token in basename for token in _SQUARE_FILENAME_TOKENS)
    clover_hit = any(token in basename for token in _CLOVER_FILENAME_TOKENS)
    hits = sum([stripe_hit, square_hit, clover_hit])
    if hits > 1:
        return "ambiguous"
    if stripe_hit:
        return "stripe"
    if square_hit:
        return "square"
    if clover_hit:
        return "clover"
    return "bank"


def _extract_probe_text(pdf_path: str | Path) -> str:
    """Concatenate the first ``_DETECTION_PROBE_PAGES`` pages' text.

    Returns an empty string on pymupdf failure so the caller treats
    the document as ``bank`` (the safe fallback).
    """
    try:
        with pymupdf.open(pdf_path) as doc:  # type: ignore[no-untyped-call]
            pages_to_probe = min(_DETECTION_PROBE_PAGES, doc.page_count)
            chunks: list[str] = []
            for i in range(pages_to_probe):
                chunks.append(doc.load_page(i).get_text("text") or "")
            return "\n".join(chunks)
    except Exception:
        _log.warning("processor.detect.read_failed", extra={"pdf_path": str(pdf_path)})
        return ""


__all__ = [
    "ProcessorBrand",
    "ProcessorDetection",
    "detect_processor",
    "detect_processor_from_csv_header",
    "detect_processor_from_filename",
]
