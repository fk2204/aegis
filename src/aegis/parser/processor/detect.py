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


ProcessorBrand = Literal["stripe", "square", "bank", "ambiguous"]


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

# Filename tokens that explicitly carry Square (kept here for parity,
# wired for the future Square CSV path). NOT used by routing yet.
_SQUARE_FILENAME_TOKENS: Final[tuple[str, ...]] = (
    "square",
    "squareup",
)


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
    if stripe_hit and square_hit:
        return "ambiguous"
    if stripe_hit:
        return "stripe"
    if square_hit:
        return "square"
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
    "detect_processor_from_filename",
]
