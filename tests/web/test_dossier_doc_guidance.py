"""Phase 2 / item 3.6 — product-aware document upload guidance.

The intake-banner "Upload statements" button is followed by a muted
help-text line that tells the operator which documents to gather for
the specific Commera lending product on this merchant (migration 080
``product_type``). Six product types, one fallback string for legacy /
unknown rows.

Render-only test — no FastAPI surface. Follows the same shape as
``tests/web/test_dossier_product_chip.py``.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from aegis.merchants.models import MerchantRow
from aegis.web._templates import templates


class _StubScore:
    """Minimal duck-typed ScoreResult — recommendation drives the
    funder section's decline-state vs. inline-match branching."""

    def __init__(self, recommendation: str = "approve") -> None:
        self.recommendation = recommendation
        self.score = 70
        self.tier = "B"
        self.paper_grade = "B"
        self.suggested_max_advance = Decimal("50000")
        self.hard_decline_reasons: list[str] = []
        self.soft_concerns: list[str] = []
        self.decline_details: dict[str, Any] = {}


def _make_merchant_ns(product_type: str | None) -> SimpleNamespace:
    """Duck-typed merchant so the test doesn't need to construct a full
    MerchantRow (the dossier template touches dozens of attributes).
    """
    return SimpleNamespace(
        id=uuid4(),
        business_name="Acme Painting LLC",
        owner_name="Jane Owner",
        state="CA",
        industry_naics="722511",
        industry_choice=None,
        time_in_business_months=24,
        credit_score=720,
        close_lead_id="lead_abc",
        product_type=product_type,
        web_presence_flags=None,
        ucc_filings=None,
        ucc_default_indicators=None,
    )


def _render_dossier(*, merchant: MerchantRow | SimpleNamespace) -> str:
    """Render the dossier template with ``from_intake=True`` so the
    intake banner (which contains the upload button + the new
    doc-guidance line) is in the output."""
    template = templates.get_template("merchant_detail_dossier.html.j2")
    return template.render(
        request=None,
        merchant=merchant,
        documents=[],
        document=None,
        analysis=None,
        aggregate_labels={},
        aggregate_unit_kind={},
        pattern_cards=[],
        latest_transactions=[],
        soft_signals=None,
        has_concentration_pattern=False,
        from_intake=True,
        intake_docs_uploaded=0,
        intake_docs_failed=0,
        score_result=_StubScore(),
        score_window=None,
        statement_coverage=None,
        stacking=None,
        mca_stack=None,
        balance_health=None,
        offer=None,
        state_tier=None,
        ofac_status="pending",
        ofac_match=None,
        trend=None,
        history=[],
        close_last_orchestration_capped=False,
        unified_tracks=None,
        shadow_signals=[],
        merchant_shadow_signals=[],
        revenue_trends=None,
        funder_note_submissions=[],
        operator_notes=[],
        operator_note_max_chars=2000,
        deal_summary=None,
        funder_narrative="",
        doc_checklist={
            "voided_check_on_file": False,
            "drivers_license_on_file": False,
            "bank_statements_months": 0,
        },
        stips_result=None,
        top_matched_funder_name=None,
        matched_funders=[],
        matched_funder_responses={},
        submitted_funder_ids=set(),
    )


# (product_type, guidance substring expected in the rendered HTML)
_PRODUCT_GUIDANCE_CASES: list[tuple[str, str]] = [
    (
        "revenue_based",
        "Upload 3-6 months of business bank statements. Processor statements",
    ),
    (
        "business_loan",
        "2 years business tax returns (1120/1120-S/1065/Schedule C) + current P&L",
    ),
    (
        "line_of_credit",
        "3-6 months bank statements + 2 years business tax returns.",
    ),
    (
        "equipment",
        "equipment quote or invoice with make/model/serial/price",
    ),
    (
        "asset_based",
        "A/R aging report (Excel or PDF) + accounts receivable list",
    ),
    (
        "receivables",
        "invoice copies + A/R aging report showing customer payment history",
    ),
]


@pytest.mark.parametrize(("product_type", "expected_substring"), _PRODUCT_GUIDANCE_CASES)
def test_dossier_renders_product_specific_doc_guidance(
    product_type: str, expected_substring: str
) -> None:
    """Each of the six Commera product types maps to its own guidance
    string. The hint chip is present and the expected guidance text
    appears immediately below the upload button."""
    merchant = _make_merchant_ns(product_type)
    html = _render_dossier(merchant=cast(MerchantRow, merchant))
    assert 'data-test-id="doc-guidance"' in html
    assert expected_substring in html


def test_dossier_doc_guidance_falls_back_for_unknown_product_type() -> None:
    """A legacy / typo / pre-080 product string lands on the neutral
    "Upload bank statements to begin." fallback rather than rendering
    an empty hint. Mirrors the product-chip fallback discipline."""
    merchant = _make_merchant_ns("totally_made_up")
    html = _render_dossier(merchant=cast(MerchantRow, merchant))
    assert 'data-test-id="doc-guidance"' in html
    assert "Upload bank statements to begin." in html


def test_dossier_doc_guidance_falls_back_for_null_product_type() -> None:
    """A pre-080 row whose ``product_type`` is ``None`` lands on the
    neutral fallback string. The intake-banner doc-guidance line never
    silently disappears."""
    merchant = _make_merchant_ns(None)
    html = _render_dossier(merchant=cast(MerchantRow, merchant))
    assert 'data-test-id="doc-guidance"' in html
    assert "Upload bank statements to begin." in html
