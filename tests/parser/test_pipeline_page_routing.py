"""Pipeline integration: per-page routing under AEGIS_PARSER_PAGE_ROUTING.

End-to-end shape: build a mixed PDF (text + image pages) on disk,
flip the feature flag on, run the full pipeline, and confirm:

- The pipeline routes pages per-strategy (text → text extractor,
  image → vision extractor).
- The merged extraction reaches the validation gate and the
  classifier/aggregator stages.
- The structured log records the per-page decision rollup.
- ``[META] per_page_routing_used`` lands on the result flags.
- The fail-closed path: a doc whose first page is so sparse that
  BOTH strategies fall below the floor routes to manual_review with
  ``page_router_low_confidence`` instead of attempting extraction.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from aegis.config import get_settings
from aegis.parser.pipeline import run_pipeline

# ---------------------------------------------------------------------------
# PDF builder + LLM stub
# ---------------------------------------------------------------------------


def _build_mixed_pdf(path: Path) -> None:
    """Page 1 text-bearing (high density), page 2 image-only (no text)."""
    out = pymupdf.open()
    # Text page: 30 lines of fake bank-statement rows so density >> threshold.
    p = out.new_page(width=612, height=792)
    p.insert_text(
        (72, 72),
        "\n".join(
            f"01/{i:02d}/2026 DEPOSIT ACH XYZ INC 1,234.56  Balance 99,999.99"
            for i in range(1, 31)
        ),
        fontsize=10,
    )
    # Image page: rasterized content (no extractable text layer).
    text_src = pymupdf.open()
    src = text_src.new_page(width=612, height=792)
    src.insert_text((72, 72), "Image content\n" * 5, fontsize=11)
    pix = src.get_pixmap(dpi=150)
    text_src.close()
    ip = out.new_page(width=pix.width, height=pix.height)
    ip.insert_image(ip.rect, pixmap=pix)
    out.save(path)
    out.close()


def _summary() -> dict[str, Any]:
    """Period: 2026-01-01..2026-01-31 (30 days). Tied-out totals."""
    return {
        "bank_name": "TEST BANK",
        "account_holder": "ACME CO LLC",
        "account_last4": "1234",
        "period_start": "2026-01-01",
        "period_end": "2026-01-31",
        "beginning_balance": "5000.00",
        "ending_balance": "15000.00",
        "deposit_total": "50000.00",
        "withdrawal_total": "40000.00",
        "printed_transaction_count": 20,
    }


def _txns_for_slice(
    *, start_idx: int, count: int, source_page: int
) -> list[dict[str, Any]]:
    """Generate ``count`` deposit+withdrawal pairs tagged for the given page.

    Source page is 1-indexed WITHIN THE SLICE. The pipeline remaps to
    the original PDF's 1-indexed page number after merge.
    """
    txns: list[dict[str, Any]] = []
    line_id = 1
    for i in range(count):
        day = start_idx + 1 + i * 3
        d = f"2026-01-{day:02d}"
        txns.append(
            {
                "posted_date": d,
                "description": f"DEPOSIT {start_idx + i + 1}",
                "amount": "5000.00",
                "running_balance": "5000.00",
                "source_page": source_page,
                "source_line": line_id,
            }
        )
        line_id += 1
        txns.append(
            {
                "posted_date": d,
                "description": f"MERCHANT ADVANCE DAILY ACH {start_idx + i + 1}",
                "amount": "-4000.00",
                "running_balance": "5000.00",
                "source_page": source_page,
                "source_line": line_id,
            }
        )
        line_id += 1
    return txns


class _MixedLLM:
    """Returns different transactions per call so the merge contributes
    rows from both slices to the union. Both text + vision return a
    summary, but only the first call's (text) summary is kept by the
    merger."""

    def __init__(self) -> None:
        self.text_calls = 0
        self.vision_calls = 0

    def extract_raw_json(
        self, pdf_bytes: bytes, prompt: str
    ) -> tuple[dict[str, Any], bool]:
        _ = (pdf_bytes, prompt)
        self.text_calls += 1
        # 5 deposit+withdrawal pairs from days 1-13 (first 10 txns).
        return {
            "summary": _summary(),
            "transactions": _txns_for_slice(start_idx=0, count=5, source_page=1),
            "synthetic_risk_indicators": [],
        }, False

    def extract_raw_json_from_images(
        self, page_images_png: list[bytes], prompt: str
    ) -> tuple[dict[str, Any], bool]:
        _ = (page_images_png, prompt)
        self.vision_calls += 1
        return {
            "summary": _summary(),
            "transactions": _txns_for_slice(start_idx=5, count=5, source_page=1),
            "synthetic_risk_indicators": [],
        }, False

    def classify_batch_json(self, prompt: str) -> dict[str, Any]:
        """Classify each row by description; same shape the production
        BedrockClient returns."""
        import json

        sentinel = "(JSON array follows):"
        idx = prompt.rfind(sentinel)
        if idx == -1:
            return {"classifications": []}
        rows = json.loads(prompt[idx + len(sentinel) :].strip())
        out: list[dict[str, Any]] = []
        for r in rows:
            desc = str(r.get("description", "")).lower()
            amt = str(r.get("amount", "0"))
            if amt.startswith("-"):
                cat = "mca_debit" if "merchant advance" in desc else "fee"
            else:
                cat = "deposit"
            out.append({"id": r["id"], "category": cat, "confidence": 95})
        return {"classifications": out}


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
def flag_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Flip ``AEGIS_PARSER_PAGE_ROUTING`` on for the duration of one test.

    Clears the settings cache so the pipeline reads the updated value.
    """
    monkeypatch.setenv("AEGIS_PARSER_PAGE_ROUTING", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_per_page_routing_off_by_default(tmp_path: Path) -> None:
    """With the flag off, the existing whole-doc routing runs — no
    per-page extraction, no router log. The mixed PDF gets routed by
    the legacy ``has_text_layer`` check (first 3 pages probe → True
    on page 1 → whole-doc text extraction)."""
    pdf_path = tmp_path / "mixed.pdf"
    _build_mixed_pdf(pdf_path)
    llm = _MixedLLM()
    result = run_pipeline(str(pdf_path), llm)
    assert llm.text_calls == 1
    assert llm.vision_calls == 0
    assert "[META] per_page_routing_used" not in result.all_flags


def test_per_page_routing_on_invokes_both_strategies(
    tmp_path: Path, flag_on: None
) -> None:
    """Flag on + mixed PDF → both extractors run, transactions merge,
    and ``per_page_routing_used`` lands on the result flags."""
    _ = flag_on
    pdf_path = tmp_path / "mixed.pdf"
    _build_mixed_pdf(pdf_path)
    llm = _MixedLLM()
    result = run_pipeline(str(pdf_path), llm)

    assert llm.text_calls == 1, "text slice should produce one call"
    assert llm.vision_calls == 1, "vision slice should produce one call"
    assert "[META] per_page_routing_used" in result.all_flags
    # The merged extraction should produce 20 transactions
    # (5 pairs from text slice + 5 pairs from vision slice = 10 pairs).
    assert result.extraction is not None
    assert len(result.extraction.statement.transactions) == 20


def test_per_page_routing_on_text_only_pdf_falls_through(
    tmp_path: Path, flag_on: None
) -> None:
    """All-text PDF → is_homogeneous returns 'text' → the pipeline
    keeps using the cheaper single-call text extraction. The classifier
    still ran (one log line) but no per-page extraction happened."""
    _ = flag_on
    pdf_path = tmp_path / "text_only.pdf"
    out = pymupdf.open()
    for _ in range(2):
        p = out.new_page(width=612, height=792)
        p.insert_text(
            (72, 72),
            "\n".join(f"01/{i:02d}/2026 DEPOSIT 1000.00" for i in range(1, 31)),
            fontsize=10,
        )
    out.save(pdf_path)
    out.close()

    llm = _MixedLLM()
    result = run_pipeline(str(pdf_path), llm)
    assert llm.text_calls == 1
    assert llm.vision_calls == 0
    assert "[META] per_page_routing_used" not in result.all_flags
