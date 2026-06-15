"""Pipeline bank-layout-hints injection + success-upsert tests.

Covers the two parser-side touchpoints added by migration 059 / Track A
(bank-layout learning):

  * Prompt injection: when ``BankLayoutRepository.get_hints`` returns
    text, the system prompt sent to the LLM contains that text under a
    "Layout hints from prior successful parses of this bank:" header.
  * No injection: when ``get_hints`` returns ``None`` (no hints, or
    below the 3-parse threshold), the system prompt does NOT contain
    that header.
  * Upsert on success: a parse that lands on ``proceed`` or ``review``
    calls ``upsert_success`` exactly once with the expected bank_name
    + a PII-free fingerprint shape.
  * No upsert on failure: a parse that lands on ``manual_review`` does
    NOT call ``upsert_success``.

We don't mock Bedrock — the existing ``_StubLLM`` from conftest.py
captures the prompt string. We extend it locally for assertion access
to the last-seen prompt.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from aegis.bank_layouts import InMemoryBankLayoutRepository
from aegis.bank_layouts.repository import HINTS_AVAILABLE_THRESHOLD
from aegis.parser.pipeline import run_pipeline


class _PromptCapturingLLM:
    """LLMClient stub that records the last prompt it received.

    Mirrors ``tests/parser/conftest.py::_StubLLM`` closely so the
    deterministic pipeline runs end-to-end; the only addition is the
    public ``last_prompt`` attribute the tests assert against.
    """

    def __init__(self, extraction_payload: dict[str, Any]) -> None:
        self._extraction = extraction_payload
        self.last_prompt: str | None = None

    def extract_raw_json(self, pdf_bytes: bytes, prompt: str) -> tuple[dict[str, Any], bool]:
        _ = pdf_bytes
        self.last_prompt = prompt
        return self._extraction, False

    def extract_raw_json_from_images(
        self, page_images_png: list[bytes], prompt: str
    ) -> tuple[dict[str, Any], bool]:
        _ = page_images_png
        self.last_prompt = prompt
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


# Re-use the clean extraction payload shape from conftest.py — the
# clean_profitable scenario produces a parse_status in {'proceed',
# 'review'}, which is exactly what the upsert success path needs.


def _import_clean_payload() -> dict[str, Any]:
    """Build the same clean payload as conftest.py's _clean_extraction_payload.

    Duplicated locally rather than importing the private helper so this
    test file stays self-contained and doesn't depend on conftest
    internals that may move during refactors.
    """
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


def _broken_payload() -> dict[str, Any]:
    payload = _import_clean_payload()
    payload["summary"]["deposit_total"] = "55000.00"  # validation will fail
    return payload


@pytest.fixture
def capturing_llm_clean() -> Iterator[_PromptCapturingLLM]:
    yield _PromptCapturingLLM(_import_clean_payload())


@pytest.fixture
def capturing_llm_broken() -> Iterator[_PromptCapturingLLM]:
    yield _PromptCapturingLLM(_broken_payload())


def test_prompt_injection_when_hints_exist(
    clean_pdf_path: Path,
    capturing_llm_clean: _PromptCapturingLLM,
) -> None:
    """When the bank crosses the 3-parse threshold + has non-empty hints,
    the system prompt sent to Bedrock contains the hints header + body."""
    repo = InMemoryBankLayoutRepository()
    # Prime Chase with hints + 5 prior successful parses so the threshold
    # gate (>= 3) opens.
    repo.set_hints(
        bank_name="Chase",
        hints="Header layout uses two-line bank-name. Running balance is rightmost.",
    )
    for _ in range(5):
        repo.upsert_success(bank_name="Chase", fingerprint={"k": 1})

    assert repo.find_by_bank_name("Chase") is not None  # sanity
    assert repo.find_by_bank_name("Chase").successful_parses >= HINTS_AVAILABLE_THRESHOLD  # type: ignore[union-attr]

    result = run_pipeline(
        str(clean_pdf_path),
        capturing_llm_clean,
        today=date(2026, 2, 15),
        bank_layouts=repo,
        known_bank_name="Chase",
    )
    assert result.parse_status in {"proceed", "review"}
    assert capturing_llm_clean.last_prompt is not None
    assert (
        "Layout hints from prior successful parses of this bank:" in capturing_llm_clean.last_prompt
    )
    assert (
        "Header layout uses two-line bank-name. Running balance is rightmost."
        in capturing_llm_clean.last_prompt
    )


def test_no_injection_when_no_hints_available(
    clean_pdf_path: Path,
    capturing_llm_clean: _PromptCapturingLLM,
) -> None:
    """A bank with no prior parses (or below threshold) produces no
    hints header in the prompt."""
    repo = InMemoryBankLayoutRepository()
    # Below threshold — only 1 prior parse, no hints set.
    repo.upsert_success(bank_name="Chase", fingerprint={"k": 1})

    run_pipeline(
        str(clean_pdf_path),
        capturing_llm_clean,
        today=date(2026, 2, 15),
        bank_layouts=repo,
        known_bank_name="Chase",
    )
    assert capturing_llm_clean.last_prompt is not None
    assert (
        "Layout hints from prior successful parses of this bank:"
        not in capturing_llm_clean.last_prompt
    )


def test_no_injection_when_repo_not_wired(
    clean_pdf_path: Path,
    capturing_llm_clean: _PromptCapturingLLM,
) -> None:
    """Default behavior — no bank_layouts kwarg → base prompt unchanged."""
    run_pipeline(
        str(clean_pdf_path),
        capturing_llm_clean,
        today=date(2026, 2, 15),
    )
    assert capturing_llm_clean.last_prompt is not None
    assert (
        "Layout hints from prior successful parses of this bank:"
        not in capturing_llm_clean.last_prompt
    )


def test_upsert_called_on_successful_parse(
    clean_pdf_path: Path,
    capturing_llm_clean: _PromptCapturingLLM,
) -> None:
    """A parse landing in {'proceed', 'review'} records the learning."""
    repo = InMemoryBankLayoutRepository()
    result = run_pipeline(
        str(clean_pdf_path),
        capturing_llm_clean,
        today=date(2026, 2, 15),
        bank_layouts=repo,
        known_bank_name=None,
    )
    assert result.parse_status in {"proceed", "review"}
    chase = repo.find_by_bank_name("Chase")
    assert chase is not None
    assert chase.successful_parses == 1
    # Fingerprint contract: observable layout only, no PII.
    fp = chase.layout_fingerprint
    assert "transaction_count" in fp
    assert "has_running_balance" in fp
    assert "page_count" in fp
    assert "currency" in fp
    # And explicitly assert no PII keys.
    forbidden = {"account_holder", "business_name", "transactions", "descriptions"}
    assert forbidden.isdisjoint(fp.keys())


def test_no_upsert_on_validation_failure(
    broken_pdf_path: Path,
    capturing_llm_broken: _PromptCapturingLLM,
) -> None:
    """A math-broken statement (parse_status='manual_review') does NOT
    record the bank as a successful parse."""
    repo = InMemoryBankLayoutRepository()
    result = run_pipeline(
        str(broken_pdf_path),
        capturing_llm_broken,
        today=date(2026, 2, 15),
        bank_layouts=repo,
        known_bank_name=None,
    )
    assert result.parse_status == "manual_review"
    # No row should have been created.
    assert repo.list_all() == []
