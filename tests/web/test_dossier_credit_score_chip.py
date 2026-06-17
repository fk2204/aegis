"""Dossier credit-score-missing chip.

When ``merchant.credit_score is None``, the dossier header renders a
warning chip below the meta-row nudging the operator that funder
matching may be incomplete. When ``credit_score`` is set, the chip
is absent.

These tests render the dossier template fragment directly through the
real Jinja environment — no FastAPI app needed — so the contract under
test is the template source, not the route plumbing around it.
"""

from __future__ import annotations

from datetime import date

from aegis.merchants.models import MerchantRow
from aegis.web._templates import templates

_CHIP_TEST_ID = "dossier-credit-missing-chip"


def _make_merchant(credit_score: int | None) -> MerchantRow:
    return MerchantRow(
        business_name="Acme Logistics LLC",
        owner_name="Jane Doe",
        state="NY",
        industry_naics="484110",
        time_in_business_months=24,
        credit_score=credit_score,
        intake_date=date(2026, 5, 1),
    )


def _render_dossier_header(merchant: MerchantRow) -> str:
    """Render just the dossier template with the bare minimum context.

    The header block we care about is at the top — most downstream
    sections render with empty defaults when documents/analysis are
    absent. Anything that throws (e.g. missing helper data) would
    indicate the chip change crashed unrelated rendering.
    """
    template = templates.get_template("merchant_detail_dossier.html.j2")
    return template.render(
        request=None,
        merchant=merchant,
        documents=[],
        analysis=None,
        history=[],
        ofac_status=None,
        ofac_last_checked_at=None,
        pattern_cards=[],
        score_result=None,
        decision=None,
        stips_result=None,
        soft_signals=[],
        funder_matches=[],
        renewal_attestation=None,
        state_tier="standard",
        toc_sections=[],
    )


def test_chip_renders_when_credit_score_is_none() -> None:
    merchant = _make_merchant(credit_score=None)
    html = _render_dossier_header(merchant)
    assert f'data-test-id="{_CHIP_TEST_ID}"' in html
    assert "Credit score not on file" in html


def test_chip_absent_when_credit_score_is_set() -> None:
    merchant = _make_merchant(credit_score=720)
    html = _render_dossier_header(merchant)
    assert f'data-test-id="{_CHIP_TEST_ID}"' not in html
    assert "Credit score not on file" not in html
