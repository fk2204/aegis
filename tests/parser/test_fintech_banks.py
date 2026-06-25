"""Unit + pipeline coverage for fintech bank-of-record detection.

Covers three surfaces:

  * ``detect_fintech_bank`` pure-function unit tests — case-insensitive
    substring match, hit / miss / missing-input cases, every seeded
    identifier round-trips.
  * Pipeline integration — a Mercury statement payload emits the
    ``[WARN] fintech_bank_detected:`` flag on ``all_flags``, and the
    detection does NOT change ``parse_status`` or ``fraud_score``
    relative to the same payload with a non-fintech bank name.
  * Negative case — a Chase / Bank of America payload does NOT emit
    the flag.

The fintech warning is decline-neutral. The discipline assertions
below pin that contract — any change that ties the WARN flag to a
score or status will fail these tests, which is the intended gate.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from aegis.parser.fintech_banks import (
    FINTECH_BANK_IDENTIFIERS,
    detect_fintech_bank,
)
from aegis.parser.pipeline import run_pipeline

# ---------------------------------------------------------------------------
# Unit tests — pure detector function
# ---------------------------------------------------------------------------


def test_detect_fintech_bank_positive_mercury() -> None:
    """A plain "Mercury" bank name resolves to the canonical entry."""
    result = detect_fintech_bank("Mercury")
    assert result is not None
    canonical, warning = result
    assert canonical == "Mercury"
    assert "Mercury" in warning
    assert "fintech" in warning.lower()


def test_detect_fintech_bank_case_insensitive_upper() -> None:
    """All-caps bank name still resolves."""
    result = detect_fintech_bank("MERCURY")
    assert result is not None
    assert result[0] == "Mercury"


def test_detect_fintech_bank_case_insensitive_lower() -> None:
    """All-lowercase bank name still resolves."""
    result = detect_fintech_bank("mercury")
    assert result is not None
    assert result[0] == "Mercury"


def test_detect_fintech_bank_substring_in_longer_name() -> None:
    """A longer bank-name string containing the identifier resolves."""
    result = detect_fintech_bank("Mercury Bank, N.A.")
    assert result is not None
    assert result[0] == "Mercury"


def test_detect_fintech_bank_negative_chase() -> None:
    """Chase is not a fintech — no detection."""
    assert detect_fintech_bank("Chase") is None


def test_detect_fintech_bank_negative_bank_of_america() -> None:
    """Bank of America is not a fintech — no detection."""
    assert detect_fintech_bank("Bank of America") is None


def test_detect_fintech_bank_negative_wells_fargo() -> None:
    """Wells Fargo is not a fintech — no detection."""
    assert detect_fintech_bank("Wells Fargo") is None


def test_detect_fintech_bank_handles_none() -> None:
    """A missing bank name (None) does not crash and returns None."""
    assert detect_fintech_bank(None) is None


def test_detect_fintech_bank_handles_empty_string() -> None:
    """An empty bank name returns None."""
    assert detect_fintech_bank("") is None


def test_detect_fintech_bank_handles_whitespace_only() -> None:
    """A whitespace-only bank name returns None."""
    assert detect_fintech_bank("   ") is None


@pytest.mark.parametrize("identifier", list(FINTECH_BANK_IDENTIFIERS.keys()))
def test_detect_fintech_bank_round_trips_every_seeded_entry(identifier: str) -> None:
    """Every seeded identifier resolves to itself when fed back in."""
    result = detect_fintech_bank(identifier)
    assert result is not None
    canonical, _warning = result
    assert canonical == FINTECH_BANK_IDENTIFIERS[identifier]


# ---------------------------------------------------------------------------
# Pipeline integration — reuse the clean payload + LLM stub from
# test_pipeline_bank_hints.py's pattern (we duplicate locally to keep
# this test self-contained per the conftest-private-helper convention).
# ---------------------------------------------------------------------------


def _clean_payload(bank_name: str) -> dict[str, Any]:
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
            "bank_name": bank_name,
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


class _StubLLM:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._extraction = payload

    def extract_raw_json(self, pdf_bytes: bytes, prompt: str) -> tuple[dict[str, Any], bool]:
        _ = (pdf_bytes, prompt)
        return self._extraction, False

    def extract_raw_json_from_images(
        self, page_images_png: list[bytes], prompt: str
    ) -> tuple[dict[str, Any], bool]:
        _ = (page_images_png, prompt)
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


@pytest.fixture
def text_pdf_with_layer(tmp_path: Path) -> Iterator[Path]:
    """Build a 2-page PDF with a real text layer so extraction routes
    through the text path. Mirrors the fixture pattern in
    ``test_pipeline_bank_hints.py``."""
    import pymupdf

    pdf = tmp_path / "text_with_layer.pdf"
    doc = pymupdf.open()  # type: ignore[no-untyped-call]
    body = "Bank statement page content for fintech detection test\n" * 5
    for _ in range(2):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 72), body, fontsize=10)
    doc.save(pdf)  # type: ignore[no-untyped-call]
    doc.close()  # type: ignore[no-untyped-call]
    yield pdf


def test_pipeline_emits_warn_flag_for_mercury(text_pdf_with_layer: Path) -> None:
    """A clean-payload parse with bank_name='Mercury' surfaces the
    ``[WARN] fintech_bank_detected:`` flag on ``all_flags``."""
    llm = _StubLLM(_clean_payload(bank_name="Mercury"))
    result = run_pipeline(str(text_pdf_with_layer), llm)

    fintech_flags = [f for f in result.all_flags if f.startswith("[WARN] fintech_bank_detected:")]
    assert len(fintech_flags) == 1
    assert "Mercury" in fintech_flags[0]


def test_pipeline_no_flag_for_chase(text_pdf_with_layer: Path) -> None:
    """A clean-payload parse with bank_name='Chase' does NOT emit the
    fintech-bank flag — Chase is a traditional bank."""
    llm = _StubLLM(_clean_payload(bank_name="Chase"))
    result = run_pipeline(str(text_pdf_with_layer), llm)

    assert not any(f.startswith("[WARN] fintech_bank_detected:") for f in result.all_flags)


def test_pipeline_no_flag_for_bank_of_america(text_pdf_with_layer: Path) -> None:
    """Bank of America is not a fintech — no flag emitted."""
    llm = _StubLLM(_clean_payload(bank_name="Bank of America"))
    result = run_pipeline(str(text_pdf_with_layer), llm)

    assert not any(f.startswith("[WARN] fintech_bank_detected:") for f in result.all_flags)


def test_pipeline_handles_missing_bank_name(text_pdf_with_layer: Path) -> None:
    """A parse where the extractor returns ``bank_name=None`` does NOT
    crash and does NOT emit the fintech flag."""
    payload = _clean_payload(bank_name="Chase")
    payload["summary"]["bank_name"] = None  # explicit no-bank-name
    llm = _StubLLM(payload)
    result = run_pipeline(str(text_pdf_with_layer), llm)

    assert not any(f.startswith("[WARN] fintech_bank_detected:") for f in result.all_flags)


def test_pipeline_fintech_flag_does_not_change_parse_status(
    text_pdf_with_layer: Path,
) -> None:
    """Decline-discipline assertion. The same clean payload, parsed
    once with bank_name='Mercury' and once with bank_name='Chase',
    lands on the same ``parse_status`` and the same ``fraud_score`` —
    the fintech detection is surface-only and MUST NOT influence the
    decline gate. Regression guard for the WARN-only contract."""
    mercury_result = run_pipeline(
        str(text_pdf_with_layer), _StubLLM(_clean_payload(bank_name="Mercury"))
    )
    chase_result = run_pipeline(
        str(text_pdf_with_layer), _StubLLM(_clean_payload(bank_name="Chase"))
    )

    assert mercury_result.parse_status == chase_result.parse_status
    assert mercury_result.fraud_score == chase_result.fraud_score
