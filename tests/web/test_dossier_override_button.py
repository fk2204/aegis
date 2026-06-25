"""Dossier override button + modal visibility.

Validates the Phase 10 dossier override surface (migration 072):

  * The "Override recommendation" button is visible only when
    ``show_override_button`` is True (latest doc parse_status in
    {proceed, decline}).
  * The HTMX modal renders one checkbox per code in
    ``override_pattern_codes`` so the operator can mark per-pattern
    false positives.
  * The decision_id hidden field appears when one is supplied; the
    form still renders (and posts) when it's absent.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from aegis.merchants.models import MerchantRow
from aegis.web._templates import templates


def _merchant() -> MerchantRow:
    return MerchantRow(
        business_name="Acme Logistics LLC",
        owner_name="Jane Doe",
        state="NY",
        industry_naics="484110",
        time_in_business_months=24,
        credit_score=720,
        intake_date=date(2026, 5, 1),
    )


class _Doc:
    """Lightweight stand-in for DocumentRow — only the fields the
    override block reads."""

    def __init__(self, *, parse_status: str) -> None:
        self.id = uuid4()
        self.parse_status = parse_status


def _render(
    *,
    show: bool,
    parse_status: str,
    pattern_codes: list[str],
    decision_id: str | None = None,
) -> str:
    """Render the dossier template with the override-modal context.

    Other context dicts default to empty so unrelated sections render
    without raising; the test asserts only on override-button strings.
    """
    template = templates.get_template("merchant_detail_dossier.html.j2")
    return template.render(
        request=None,
        merchant=_merchant(),
        documents=[],
        document=_Doc(parse_status=parse_status),
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
        show_override_button=show,
        override_pattern_codes=pattern_codes,
        override_latest_decision_id=decision_id,
    )


def test_override_button_visible_when_proceed() -> None:
    html = _render(show=True, parse_status="proceed", pattern_codes=["mca_stacking"])
    assert 'data-test-id="override-recommendation-btn"' in html
    assert 'data-test-id="override-modal"' in html
    assert 'data-test-id="override-submit-btn"' in html


def test_override_button_absent_when_manual_review() -> None:
    html = _render(show=False, parse_status="manual_review", pattern_codes=[])
    assert 'data-test-id="override-recommendation-btn"' not in html
    assert 'data-test-id="override-modal"' not in html


def test_override_modal_renders_one_checkbox_per_pattern_code() -> None:
    codes = ["mca_stacking", "wash_deposit_suspected", "duplicate_deposits_detected"]
    html = _render(show=True, parse_status="proceed", pattern_codes=codes)
    assert 'data-test-id="override-pattern-fp-list"' in html
    for code in codes:
        # Both the code-text AND the form input value land in the modal.
        assert f'value="{code}"' in html
        assert f"<code>{code}</code>" in html


def test_override_modal_emits_decision_id_only_when_present() -> None:
    html_with = _render(
        show=True,
        parse_status="proceed",
        pattern_codes=[],
        decision_id="00000000-0000-0000-0000-000000000001",
    )
    assert 'name="decision_id"' in html_with
    assert "00000000-0000-0000-0000-000000000001" in html_with

    html_without = _render(show=True, parse_status="proceed", pattern_codes=[], decision_id=None)
    assert 'name="decision_id"' not in html_without
