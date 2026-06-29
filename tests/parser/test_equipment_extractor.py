"""Tests for ``aegis.parser.equipment`` — extraction + routing + VIN gate.

Three coverage areas:

* ``extract_equipment_details`` against a stub Bedrock client that
  captures the call shape and returns a canned tool payload.
* ``detect_equipment_document`` precedence vs. Agent 2's A/R aging
  detector — the equipment rule fires on "invoice" only when no aging
  / receivable token is also present.
* ``_coerce_vin`` directly — strict 17-char alphanumeric, no I/O/Q,
  drop on any mismatch so a malformed VIN never lands on the dossier.

The Bedrock client is a Protocol stub; no network round-trips, no
boto3 credentials needed. Mirrors the ``_StubNarratorClient`` shape in
``tests/scoring_v2/test_narrator.py``.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from aegis.parser.equipment import (
    EquipmentInvoiceResult,
    detect_equipment_document,
    extract_equipment_details,
)
from aegis.parser.equipment.extract import _coerce_vin
from aegis.parser.pipeline import (
    detect_equipment_document as detect_equipment_via_pipeline,
)

_STUB_MODEL_ID = "us.anthropic.claude-sonnet-4-6"


class _StubEquipmentClient:
    """Bedrock stub — captures the request, returns a fixed tool payload."""

    def __init__(self, tool_input: dict[str, Any]) -> None:
        self.tool_input = tool_input
        self.calls: int = 0
        self.last_system_prompt: str | None = None
        self.last_user_prompt: str | None = None
        self.last_temperature: float | None = None
        self.last_pdf_bytes: bytes | None = None

    def invoke_tool_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tool_name: str,
        tool_schema: dict[str, Any],
        max_tokens: int,
        temperature: float,
        pdf_bytes: bytes | None = None,
    ) -> tuple[dict[str, Any], str]:
        self.calls += 1
        self.last_system_prompt = system_prompt
        self.last_user_prompt = user_prompt
        self.last_temperature = temperature
        self.last_pdf_bytes = pdf_bytes
        return self.tool_input, _STUB_MODEL_ID


def _write_pdf(tmp_path: Path, body: bytes = b"%PDF-1.4 stub\n") -> str:
    """Write a tiny placeholder PDF on disk; the stub client ignores
    the bytes but ``extract_equipment_details`` still reads them so the
    file must exist and be non-empty."""
    pdf_path = tmp_path / "quote.pdf"
    pdf_path.write_bytes(body)
    return str(pdf_path)


# ---------------------------------------------------------------------------
# (a) Extraction with mocked Bedrock
# ---------------------------------------------------------------------------


def test_extract_returns_validated_result_on_complete_payload(tmp_path: Path) -> None:
    """A full Bedrock tool payload yields an ``EquipmentInvoiceResult``
    with Decimal money + all optional fields preserved."""
    client = _StubEquipmentClient(
        {
            "description": "2023 Kenworth T880 Day Cab",
            "make": "Kenworth",
            "model": "T880",
            "year": 2023,
            "condition": "new",
            "vin": "1XKAD49X7PJ123456",  # 17 chars, no I/O/Q
            "vendor_name": "Highway Truck Sales LLC",
            "total_cost": "152500.00",
        }
    )
    pdf_path = _write_pdf(tmp_path)
    result = extract_equipment_details(pdf_path, llm_client=client)
    assert result is not None
    assert isinstance(result, EquipmentInvoiceResult)
    assert result.description == "2023 Kenworth T880 Day Cab"
    assert result.make == "Kenworth"
    assert result.model == "T880"
    assert result.year == 2023
    assert result.condition == "new"
    assert result.vin == "1XKAD49X7PJ123456"
    assert result.vendor_name == "Highway Truck Sales LLC"
    assert result.total_cost == Decimal("152500.00")
    assert client.calls == 1
    # Temperature MUST be 0 (deterministic extraction). The client stub
    # captures it so a regression that raises temperature fails here.
    assert client.last_temperature == 0.0
    assert client.last_pdf_bytes == b"%PDF-1.4 stub\n"


def test_extract_drops_garbage_total_cost(tmp_path: Path) -> None:
    """A non-numeric total_cost is non-recoverable → ``None`` result.

    Mirrors the ``aegis.close.description_extractor._coerce_field``
    discipline: garbage money fails the parse, never silently
    degrades the dossier."""
    client = _StubEquipmentClient(
        {
            "description": "Bobcat S650 Skid Steer",
            "total_cost": "garbage",
        }
    )
    result = extract_equipment_details(_write_pdf(tmp_path), llm_client=client)
    assert result is None


def test_extract_drops_negative_total_cost(tmp_path: Path) -> None:
    """Negative cost is non-recoverable → ``None``."""
    client = _StubEquipmentClient(
        {
            "description": "Generator",
            "total_cost": "-100.00",
        }
    )
    assert extract_equipment_details(_write_pdf(tmp_path), llm_client=client) is None


def test_extract_strips_money_formatting(tmp_path: Path) -> None:
    """``$`` and commas are stripped before Decimal parse — the
    extractor accepts both ``"52500.00"`` and a defensive ``"$52,500"``
    even though the schema says Decimal-safe-string. Bedrock has been
    seen ignoring the schema constraint occasionally."""
    client = _StubEquipmentClient({"description": "Forklift", "total_cost": "$52,500"})
    result = extract_equipment_details(_write_pdf(tmp_path), llm_client=client)
    assert result is not None
    assert result.total_cost == Decimal("52500.00")


def test_extract_skips_not_an_equipment_quote_sentinel(tmp_path: Path) -> None:
    """When the model signals the PDF isn't actually an equipment
    quote, the sanitiser drops the whole result. Guards against the
    routing being too aggressive on filename-only signals."""
    client = _StubEquipmentClient(
        {
            "description": "(not an equipment quote)",
            "total_cost": "0.00",
        }
    )
    assert extract_equipment_details(_write_pdf(tmp_path), llm_client=client) is None


def test_extract_returns_none_for_missing_file(tmp_path: Path) -> None:
    """Missing file → ``None`` and no Bedrock call."""
    client = _StubEquipmentClient({"description": "x", "total_cost": "1"})
    missing = str(tmp_path / "does_not_exist.pdf")
    assert extract_equipment_details(missing, llm_client=client) is None
    assert client.calls == 0


# ---------------------------------------------------------------------------
# (b) Detection precedence vs. A/R aging
# ---------------------------------------------------------------------------


_DETECTION_CASES: list[tuple[str, bool]] = [
    # Direct equipment tokens — fire.
    ("Quote_2024_05.pdf", True),
    ("equipment-bobcat-s650.pdf", True),
    ("vehicle_bill_of_sale.pdf", True),
    ("machinery_lease.pdf", True),
    # "invoice" alone fires.
    ("Invoice_42.pdf", True),
    ("ACME_invoice_2024.pdf", True),
    # "invoice" + an A/R aging signal → A/R aging wins.
    ("AR_Aging_Invoice_Detail.pdf", False),
    ("Receivables Invoice Aging.pdf", False),
    ("ar-aging-invoice-export.pdf", False),
    # Pure A/R aging — never equipment.
    ("AR_Aging_Report.xlsx", False),
    ("receivable_aging.csv", False),
    # Neither — fall through.
    ("BankStatement_April_2024.pdf", False),
    ("Stripe_balance_transactions.csv", False),
    ("", False),
]


@pytest.mark.parametrize(("filename", "expected"), _DETECTION_CASES)
def test_detect_equipment_document_precedence(filename: str, expected: bool) -> None:
    """Filename → equipment route bool. The A/R aging path wins when
    any aging / receivable token is present, even if "invoice" is also
    in the filename. This is the load-bearing precedence rule with
    Agent 2's parser."""
    assert detect_equipment_document(filename) is expected


def test_pipeline_reexport_matches_module_detector() -> None:
    """``aegis.parser.pipeline.detect_equipment_document`` is the
    public callable the worker / upload route spells. It must call
    through to the same logic as the module-local detector — a
    regression where the two diverge would silently drop documents
    into the bank pipeline."""
    for filename, expected in _DETECTION_CASES:
        assert detect_equipment_via_pipeline(filename) is expected


# ---------------------------------------------------------------------------
# (c) VIN validation rejection
# ---------------------------------------------------------------------------


def test_coerce_vin_accepts_canonical_17_char() -> None:
    """A clean 17-char VIN with no I/O/Q is preserved."""
    assert _coerce_vin("1XKAD49X7PJ123456") == "1XKAD49X7PJ123456"


def test_coerce_vin_uppercases_and_strips_spaces_hyphens() -> None:
    """Operators / OCR commonly emit a VIN with stray spacing or
    dashes; the gate normalises before validating length. Same digit
    set, just formatting noise."""
    assert _coerce_vin("1xk ad49x-7pj123456") == "1XKAD49X7PJ123456"


@pytest.mark.parametrize(
    "bad_vin",
    [
        "TOO_SHORT",  # < 17 chars
        "ABCDEFGHJKLMNPRSTUVW",  # > 17 chars
        "1XKAD49X7PI123456",  # contains I (position 11)
        "1XKAD49X7PO123456",  # contains O
        "1XKAD49X7PQ123456",  # contains Q
        "1XKAD49X7P*123456",  # non-alphanumeric
        "",  # empty
    ],
)
def test_coerce_vin_drops_malformed(bad_vin: str) -> None:
    """Any VIN that fails the 17-char alphanumeric no-I/O/Q gate is
    dropped to ``None`` — shipping a malformed VIN onto the dossier
    creates downstream confusion when the funder cross-checks the
    VIN. Strict-or-omit, never best-effort."""
    assert _coerce_vin(bad_vin) is None


def test_coerce_vin_rejects_non_string() -> None:
    """A non-string VIN (model accident — integer or list) is dropped
    rather than coerced."""
    assert _coerce_vin(123) is None
    assert _coerce_vin(None) is None
    assert _coerce_vin(["1XKAD49X7PJ123456"]) is None


def test_extract_drops_vin_field_when_malformed(tmp_path: Path) -> None:
    """Full-stack: a malformed VIN inside an otherwise valid payload
    is dropped from the result, but the rest of the extraction
    survives. This is the contract that protects the dossier from
    a bad VIN landing on a row that's otherwise fine."""
    client = _StubEquipmentClient(
        {
            "description": "2022 Freightliner Cascadia",
            "make": "Freightliner",
            "model": "Cascadia",
            "year": 2022,
            "vin": "TOO_SHORT",  # malformed — gate drops it
            "total_cost": "85000.00",
        }
    )
    result = extract_equipment_details(_write_pdf(tmp_path), llm_client=client)
    assert result is not None
    assert result.vin is None
    assert result.make == "Freightliner"
    assert result.total_cost == Decimal("85000.00")
