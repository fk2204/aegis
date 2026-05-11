"""Parser test fixtures.

Builds two minimal PDFs (valid for `pikepdf` / `analyze_metadata`) and a
canned-response `LLMClient` stub. Real Bedrock calls are not made — tests
demonstrate pipeline behavior, not LLM behavior.

Two scenarios:
- `clean_profitable` — printed totals tie out against line items.
- `math_broken`     — printed deposit_total is wrong by $5,000.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pikepdf
import pytest

# -- minimal PDFs ------------------------------------------------------------


def _build_blank_pdf(path: Path, *, pages: int = 2) -> None:
    pdf = pikepdf.Pdf.new()
    for _ in range(pages):
        pdf.add_blank_page(page_size=(612, 792))  # US Letter
    pdf.save(str(path))
    pdf.close()


@pytest.fixture(scope="session")
def clean_pdf_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    p = tmp_path_factory.mktemp("clean") / "clean_profitable.pdf"
    _build_blank_pdf(p, pages=2)
    return p


@pytest.fixture(scope="session")
def broken_pdf_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    p = tmp_path_factory.mktemp("broken") / "math_broken.pdf"
    _build_blank_pdf(p, pages=2)
    return p


# -- canned LLM payloads -----------------------------------------------------
# Beginning $5000 + deposits $50000 - withdrawals $40000 = $15000 ✓
# Period: 2026-01-01 .. 2026-01-31 (30 days, well inside 14-50)
# 10 deposits ($5000 each), 10 withdrawals ($4000 each) — paired.
# Daily running balance ties out: every transaction day has begin + sum(today) = end.

_PERIOD_START = date(2026, 1, 1)
_PERIOD_END = date(2026, 1, 31)


def _clean_extraction_payload() -> dict[str, Any]:
    """Build the canned pass-1 response.

    Spread 10 deposit+withdrawal pairs across the full 31-day period
    (every 3 days). Deposits vary realistically ($4673 - $5421) so the
    synthetic-low-variance + round-number detectors don't fire, and the
    velocity detector doesn't see a 7-day spike. Running balance is
    tracked precisely so the daily reconciliation check passes.

    Totals: 10 deposits summing to $50,000.00; 10 withdrawals of
    -$4,000.00 each summing to -$40,000.00. Beginning $5,000 +
    deposits $50,000 - withdrawals $40,000 = ending $15,000.
    """
    # Varied but tied-out deposit stream — sum is exactly $50,000.00.
    deposit_amounts = [
        "4673.21", "5108.42", "4892.15", "5421.07", "4760.55",
        "5290.18", "4843.62", "5176.33", "4915.88", "4918.59",
    ]
    assert sum(Decimal(a) for a in deposit_amounts) == Decimal("50000.00")

    transactions: list[dict[str, Any]] = []
    running = Decimal("5000.00")
    line_id = 1
    for i, amount in enumerate(deposit_amounts):
        day = 1 + i * 3  # days 1, 4, 7, 10, ..., 28
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
            "bank_name": "TEST BANK",
            "account_holder": "ACME CO LLC",
            "account_last4": "1234",
            "period_start": _PERIOD_START.isoformat(),
            "period_end": _PERIOD_END.isoformat(),
            "beginning_balance": "5000.00",
            "ending_balance": "15000.00",
            "deposit_total": "50000.00",
            "withdrawal_total": "40000.00",
            "printed_transaction_count": 20,
        },
        "transactions": transactions,
        "synthetic_risk_indicators": [],
    }


def _broken_extraction_payload() -> dict[str, Any]:
    """Same line items, but printed `deposit_total` is wrong by $5,000.

    The validator should fire `reconciliation_failed_deposit_total`.
    """
    payload = _clean_extraction_payload()
    payload["summary"]["deposit_total"] = "55000.00"  # wrong
    return payload


def _classification_payload(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a canned pass-2 response that classifies each row.

    Deposit rows -> "deposit", debit rows containing 'merchant advance' -> 'mca_debit'.
    """
    classifications: list[dict[str, Any]] = []
    for t in transactions:
        desc = t["description"].lower()
        if t["amount"].startswith("-"):
            cat = "mca_debit" if "merchant advance" in desc else "fee"
        else:
            cat = "deposit"
        classifications.append(
            {
                "id": "ECHO",  # patched per-call below; pipeline uses txn id
                "category": cat,
                "confidence": 95,
            }
        )
    return {"classifications": classifications}


# -- LLM stubs ---------------------------------------------------------------


class _StubLLM:
    """In-memory `LLMClient` stub.

    Holds a canned extraction payload and constructs a classification
    response from the request prompt (so each call's transaction ids
    are echoed back correctly).
    """

    def __init__(
        self,
        extraction_payload: dict[str, Any],
        *,
        truncated: bool = False,
    ) -> None:
        self._extraction = extraction_payload
        self._truncated = truncated

    def extract_raw_json(
        self, pdf_bytes: bytes, prompt: str
    ) -> tuple[dict[str, Any], bool]:
        _ = (pdf_bytes, prompt)
        return self._extraction, self._truncated

    def classify_batch_json(self, prompt: str) -> dict[str, Any]:
        # The header ends with "(JSON array follows):" then a newline + the
        # actual array. Locate by sentinel so schema-shape brackets in the
        # header don't confuse us.
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
def clean_llm() -> Iterator[_StubLLM]:
    yield _StubLLM(_clean_extraction_payload())


@pytest.fixture
def broken_llm() -> Iterator[_StubLLM]:
    yield _StubLLM(_broken_extraction_payload())
