"""Vision-first routing tests — 2026-06-24 wire.

Covers the four contract points the operator's spec calls out:

  1. A PDF with a real text layer (≥ ``VISION_ROUTE_THRESHOLD`` chars
     total across the first probed pages) takes the text path — no
     ``[META] vision_routed`` flag fires.
  2. A PDF with effectively no text layer (image-only / <
     ``VISION_ROUTE_THRESHOLD`` chars) routes straight to vision —
     ``[META] vision_routed: chars=N`` fires, ``[META] ocr_fallback_used``
     does NOT (the latter is reserved for the text→error→vision-fallback
     path).
  3. A vision-routed doc does NOT receive the bank-layout-hints block
     in its prompt suffix. Hints describe text-layer structure (column
     labels, section delimiters) that the vision model interprets
     differently; injecting them confuses the prompt without helping.
  4. The existing ``ocr_fallback_used`` flag continues to fire on the
     text→error→vision-fallback path — the wire only adds a sibling
     flag, it does not repurpose the existing one.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pikepdf
import pymupdf
import pytest

from aegis.bank_layouts import InMemoryBankLayoutRepository
from aegis.parser.extract import ExtractionError
from aegis.parser.pipeline import run_pipeline


class _PromptCapturingLLM:
    """LLMClient stub that records the last prompt + raises ExtractionError
    on demand, so the text→error→vision-fallback path can be exercised."""

    def __init__(
        self,
        extraction_payload: dict[str, Any],
        *,
        raise_text_extraction: bool = False,
    ) -> None:
        self._extraction = extraction_payload
        self._raise_text_extraction = raise_text_extraction
        self.last_text_prompt: str | None = None
        self.last_vision_prompt: str | None = None

    def extract_raw_json(self, pdf_bytes: bytes, prompt: str) -> tuple[dict[str, Any], bool]:
        _ = pdf_bytes
        self.last_text_prompt = prompt
        if self._raise_text_extraction:
            raise ExtractionError("synthetic text-pass failure")
        return self._extraction, False

    def extract_raw_json_from_images(
        self, page_images_png: list[bytes], prompt: str
    ) -> tuple[dict[str, Any], bool]:
        _ = page_images_png
        self.last_vision_prompt = prompt
        return self._extraction, False

    def classify_batch_json(self, prompt: str) -> dict[str, Any]:
        sentinel = "(JSON array follows):"
        idx = prompt.rfind(sentinel)
        if idx == -1:
            return {"classifications": []}
        tail = prompt[idx + len(sentinel) :].strip()
        rows = json.loads(tail)
        out: list[dict[str, Any]] = []
        for r in rows:
            desc = str(r.get("description", "")).lower()
            amt_str = str(r.get("amount", "0"))
            if amt_str.startswith("-"):
                category = "mca_debit" if "merchant advance" in desc else "fee"
            else:
                category = "deposit"
            out.append({"id": r["id"], "category": category, "confidence": 95})
        return {"classifications": out}


def _clean_payload() -> dict[str, Any]:
    """Identical to ``tests/parser/conftest.py::_clean_extraction_payload``
    but duplicated locally so this file is self-contained."""
    from decimal import Decimal

    deposit_amounts = [
        "4673.21",
        "5108.42",
        "4892.15",
        "5421.07",
        "4760.55",
        "5290.18",
        "4843.62",
        "5176.33",
        "4915.88",
        "4918.59",
    ]
    transactions: list[dict[str, Any]] = []
    running = Decimal("5000.00")
    line_id = 1
    for i, amount in enumerate(deposit_amounts):
        day = 1 + i * 3
        d = f"2026-01-{day:02d}"
        running += Decimal(amount)
        transactions.append(
            {
                "posted_date": d,
                "description": f"DEPOSIT {i + 1}",
                "amount": amount,
                "running_balance": str(running),
                "source_page": 1 if day <= 14 else 2,
                "source_line": line_id,
            }
        )
        line_id += 1
        running -= Decimal("4000.00")
        transactions.append(
            {
                "posted_date": d,
                "description": f"MERCHANT ADVANCE DAILY ACH {i + 1}",
                "amount": "-4000.00",
                "running_balance": str(running),
                "source_page": 1 if day <= 14 else 2,
                "source_line": line_id,
            }
        )
        line_id += 1
    return {
        "summary": {
            "bank_name": "Chase",
            "account_holder": "ACME CO LLC",
            "account_last4": "1234",
            "period_start": "2026-01-01",
            "period_end": "2026-01-31",
            "beginning_balance": "5000.00",
            "ending_balance": "15000.00",
            "deposit_total": "50000.00",
            "withdrawal_total": "40000.00",
            "printed_transaction_count": 20,
        },
        "transactions": transactions,
        "synthetic_risk_indicators": [],
    }


@pytest.fixture
def text_pdf_path(tmp_path: Path) -> Path:
    """Build a 2-page PDF with a real text layer well above the 50-char
    aggregate floor — routes through the text path, not vision."""
    p = tmp_path / "text.pdf"
    doc = pymupdf.open()  # type: ignore[no-untyped-call]
    body = "Chase Business Complete Checking statement page\n" * 5
    for _ in range(2):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 72), body, fontsize=10)
    doc.save(p)  # type: ignore[no-untyped-call]
    doc.close()  # type: ignore[no-untyped-call]
    return p


@pytest.fixture
def image_only_pdf_path(tmp_path: Path) -> Path:
    """Build a 2-page blank PDF with no text layer at all — pikepdf
    blank pages have zero extractable text, which puts the aggregate
    char count at 0 (below the 50-char floor) and triggers the
    vision-first route."""
    p = tmp_path / "image_only.pdf"
    pdf = pikepdf.Pdf.new()
    for _ in range(2):
        pdf.add_blank_page(page_size=(612, 792))
    pdf.save(str(p))
    pdf.close()
    return p


@pytest.fixture
def capturing_llm() -> Iterator[_PromptCapturingLLM]:
    yield _PromptCapturingLLM(_clean_payload())


@pytest.fixture
def capturing_llm_text_fails() -> Iterator[_PromptCapturingLLM]:
    yield _PromptCapturingLLM(_clean_payload(), raise_text_extraction=True)


# ---------------------------------------------------------------------
# 1. Real text layer → text path
# ---------------------------------------------------------------------


def test_text_layer_pdf_routes_to_text_no_vision_routed_flag(
    text_pdf_path: Path,
    capturing_llm: _PromptCapturingLLM,
) -> None:
    """A PDF with extractable text well above the 50-char floor takes
    the text path — no ``[META] vision_routed`` flag, vision LLM
    endpoint never called."""
    result = run_pipeline(
        str(text_pdf_path),
        capturing_llm,
        today=date(2026, 2, 15),
    )
    assert any("vision_routed" not in f for f in result.all_flags)
    assert not any("vision_routed" in f for f in result.all_flags)
    assert capturing_llm.last_text_prompt is not None
    assert capturing_llm.last_vision_prompt is None


# ---------------------------------------------------------------------
# 2. Image-only PDF → vision-first route + new flag
# ---------------------------------------------------------------------


def test_image_only_pdf_routes_to_vision_with_vision_routed_flag(
    image_only_pdf_path: Path,
    capturing_llm: _PromptCapturingLLM,
) -> None:
    """An image-only PDF (chars < 50) takes the vision route, fires
    ``[META] vision_routed: chars=N``, and does NOT fire the
    ``[META] ocr_fallback_used`` flag (which is reserved for the
    text→error→vision-fallback path)."""
    result = run_pipeline(
        str(image_only_pdf_path),
        capturing_llm,
        today=date(2026, 2, 15),
    )
    flags = result.all_flags
    vision_flags = [f for f in flags if "vision_routed" in f]
    assert len(vision_flags) == 1, f"Expected one vision_routed flag, got: {flags}"
    assert vision_flags[0].startswith("[META] vision_routed: chars=")
    # The proactive route MUST NOT also fire ocr_fallback_used —
    # ocr_fallback_used is reserved for the text→error→vision-fallback
    # path so operators can distinguish the two failure modes on the
    # dossier.
    assert not any("ocr_fallback_used" in f for f in flags)
    # Vision LLM endpoint was called; text endpoint was NOT.
    assert capturing_llm.last_vision_prompt is not None
    assert capturing_llm.last_text_prompt is None


# ---------------------------------------------------------------------
# 3. Vision-routed doc → no layout-hints injection
# ---------------------------------------------------------------------


def test_vision_routed_skips_layout_hints_in_prompt_suffix(
    image_only_pdf_path: Path,
    capturing_llm: _PromptCapturingLLM,
) -> None:
    """Even when a bank_layouts repo is wired with hints, a vision-
    routed doc does NOT receive the hints block in its prompt suffix.
    Hints describe text-layer structure and confuse the vision
    extraction prompt."""
    repo = InMemoryBankLayoutRepository()
    repo.set_hints(
        bank_name="Chase",
        hints="Header layout uses two-line bank-name. Running balance is rightmost.",
    )
    # Cross the HINTS_AVAILABLE_THRESHOLD so the layout-hints block
    # would otherwise inject.
    for _ in range(5):
        repo.upsert_success(bank_name="Chase", fingerprint={"k": 1})

    result = run_pipeline(
        str(image_only_pdf_path),
        capturing_llm,
        today=date(2026, 2, 15),
        bank_layouts=repo,
        known_bank_name="Chase",
    )
    # Sanity — we did go through the vision branch.
    assert any("vision_routed" in f for f in result.all_flags)
    assert capturing_llm.last_vision_prompt is not None
    # The hints header MUST NOT appear in the prompt the vision call
    # received.
    assert (
        "Layout hints from prior successful parses of this bank:"
        not in capturing_llm.last_vision_prompt
    )
    assert "Running balance is rightmost." not in capturing_llm.last_vision_prompt


# ---------------------------------------------------------------------
# 4. Existing ocr_fallback_used preserved for text→error→vision path
# ---------------------------------------------------------------------


def test_text_error_vision_fallback_still_fires_ocr_fallback_used_flag(
    text_pdf_path: Path,
    capturing_llm_text_fails: _PromptCapturingLLM,
) -> None:
    """A PDF with a real text layer whose text-extraction call raises
    ExtractionError falls through to the vision fallback. The legacy
    ``[META] ocr_fallback_used`` flag fires; the new
    ``[META] vision_routed`` flag does NOT (vision_routed is reserved
    for the proactive image-only initial route).
    """
    result = run_pipeline(
        str(text_pdf_path),
        capturing_llm_text_fails,
        today=date(2026, 2, 15),
        vision_fallback_on_extraction_error=True,
    )
    flags = result.all_flags
    # Existing flag fires (the legacy contract).
    assert any("ocr_fallback_used" in f for f in flags), flags
    # New flag does NOT fire (this isn't the proactive route).
    assert not any("vision_routed" in f for f in flags), flags
    # Both LLM endpoints were called — text first (raised), then vision.
    assert capturing_llm_text_fails.last_text_prompt is not None
    assert capturing_llm_text_fails.last_vision_prompt is not None
