"""Feature D — ``_build_extraction_prompt_suffix`` tests covering the
four shapes documented on the function:

* merchant-context only            (MERCHANT CONTEXT block, no layout block)
* layout-hints only                (legacy behavior preserved)
* both                             (merchant block, blank line, layout block)
* neither                          (returns None)

Pure unit tests over the prompt builder — no LLM, no PDF, no DB. The
end-to-end injection through ``run_pipeline`` is already covered by
``test_pipeline_bank_hints.py``; this file pins the formatting of the
new merchant-context block.
"""

from __future__ import annotations

import pytest

from aegis.bank_layouts import InMemoryBankLayoutRepository
from aegis.parser.pipeline import (
    MerchantContext,
    _build_extraction_prompt_suffix,
)

# ---------------------------------------------------------------------------
# MerchantContext sanity
# ---------------------------------------------------------------------------


def test_merchant_context_is_empty_when_all_none() -> None:
    ctx = MerchantContext()
    assert ctx.is_empty() is True


def test_merchant_context_is_empty_when_all_whitespace() -> None:
    ctx = MerchantContext(
        deal_context="   ",
        close_lead_description="\n\n",
        close_notes_summary="",
        close_call_transcripts=None,
    )
    assert ctx.is_empty() is True


def test_merchant_context_not_empty_when_any_field_set() -> None:
    ctx = MerchantContext(deal_context="x")
    assert ctx.is_empty() is False


# ---------------------------------------------------------------------------
# Prompt suffix — four shapes
# ---------------------------------------------------------------------------


def test_neither_returns_none() -> None:
    """No bank layouts wired + no merchant context = no suffix."""
    out = _build_extraction_prompt_suffix(
        bank_layouts=None,
        known_bank_name=None,
        merchant_context=None,
    )
    assert out is None


def test_merchant_context_only_renders_block_with_present_lines() -> None:
    """Lines with empty values are omitted; lines with content render."""
    ctx = MerchantContext(
        deal_context="Broker said merchant is renewal",
        close_lead_description=None,  # omitted
        close_notes_summary="Note A\n---\nNote B",
        close_call_transcripts="",  # omitted
    )
    out = _build_extraction_prompt_suffix(
        bank_layouts=None,
        known_bank_name=None,
        merchant_context=ctx,
    )
    assert out is not None
    assert "MERCHANT CONTEXT (use this to better understand the deal):" in out
    assert "Operator notes: Broker said merchant is renewal" in out
    assert "Recent Close notes: Note A\n---\nNote B" in out
    # Omitted lines: their labels must NOT appear.
    assert "Close lead description:" not in out
    assert "Recent call summaries:" not in out
    # No layout block appended.
    assert "Layout hints from prior successful parses" not in out


def test_merchant_context_only_with_all_four_fields() -> None:
    """Every field present → every label present in order."""
    ctx = MerchantContext(
        deal_context="Op note",
        close_lead_description="Lead desc",
        close_notes_summary="Notes summary",
        close_call_transcripts="Calls summary",
    )
    out = _build_extraction_prompt_suffix(
        bank_layouts=None,
        known_bank_name=None,
        merchant_context=ctx,
    )
    assert out == (
        "MERCHANT CONTEXT (use this to better understand the deal):\n"
        "Operator notes: Op note\n"
        "Close lead description: Lead desc\n"
        "Recent Close notes: Notes summary\n"
        "Recent call summaries: Calls summary"
    )


def test_layout_hints_only_preserves_legacy_behavior() -> None:
    """The original layout-hints block still renders when merchant_context
    is None — Feature D's change is purely additive."""
    repo = InMemoryBankLayoutRepository()
    repo.set_hints(bank_name="Chase", hints="rightmost column is balance")
    for _ in range(5):
        repo.upsert_success(bank_name="Chase", fingerprint={"k": 1})

    out = _build_extraction_prompt_suffix(
        bank_layouts=repo,
        known_bank_name="Chase",
        merchant_context=None,
    )
    assert out == (
        "Layout hints from prior successful parses of this bank:\nrightmost column is balance"
    )


def test_both_blocks_concatenate_with_blank_line() -> None:
    """When merchant context AND layout hints both produce a block,
    concatenate merchant-first then layout, joined by a single blank line.
    """
    repo = InMemoryBankLayoutRepository()
    repo.set_hints(bank_name="Chase", hints="rightmost column is balance")
    for _ in range(5):
        repo.upsert_success(bank_name="Chase", fingerprint={"k": 1})

    ctx = MerchantContext(deal_context="Broker said this is a renewal")
    out = _build_extraction_prompt_suffix(
        bank_layouts=repo,
        known_bank_name="Chase",
        merchant_context=ctx,
    )
    assert out == (
        "MERCHANT CONTEXT (use this to better understand the deal):\n"
        "Operator notes: Broker said this is a renewal"
        "\n\n"
        "Layout hints from prior successful parses of this bank:\n"
        "rightmost column is balance"
    )


def test_empty_merchant_context_is_treated_as_none() -> None:
    """An empty MerchantContext does not render a heading-only block.

    Equivalent to passing ``merchant_context=None`` — only the
    layout-hints block survives.
    """
    repo = InMemoryBankLayoutRepository()
    repo.set_hints(bank_name="Chase", hints="hint")
    for _ in range(5):
        repo.upsert_success(bank_name="Chase", fingerprint={"k": 1})

    out = _build_extraction_prompt_suffix(
        bank_layouts=repo,
        known_bank_name="Chase",
        merchant_context=MerchantContext(),
    )
    assert out is not None
    assert "MERCHANT CONTEXT" not in out
    assert "Layout hints" in out


@pytest.mark.parametrize(
    "field_name, value",
    [
        ("deal_context", "Op note"),
        ("close_lead_description", "Lead desc"),
        ("close_notes_summary", "Notes summary"),
        ("close_call_transcripts", "Calls summary"),
    ],
)
def test_single_field_only_renders_only_that_line(field_name: str, value: str) -> None:
    """Each of the four fields gates its own line independently."""
    ctx = MerchantContext(**{field_name: value})
    out = _build_extraction_prompt_suffix(
        bank_layouts=None,
        known_bank_name=None,
        merchant_context=ctx,
    )
    assert out is not None
    # Heading present.
    assert "MERCHANT CONTEXT" in out
    # Exactly two lines of content: heading + the one field's line.
    lines = out.split("\n")
    assert len(lines) == 2
    assert value in lines[1]
