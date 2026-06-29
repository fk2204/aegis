"""Dossier "Record outcome" prominent-CTA placement (Phase 2 / item 9.1).

The prominent "Record outcome" button replaces the old § 5¾ section
on the dossier — it now lives at the top of § 5 Disposition, above
the disposition copy, as the primary post-fund call to action.

These tests pin the placement contract:

  * The CTA appears only when ``override_latest_decision_id`` is set
    (without a recorded decision the outcome FK would violate
    ``deal_outcomes.decision_id NOT NULL`` from migration 074).
  * The CTA carries the ``btn btn-primary`` class so it picks up the
    dossier's ink-fill primary styling (aegis-system.css ~L520).
  * The HTMX wiring targets the in-section ``#deal-outcome-modal-host``
    swap target.
  * The CTA renders BEFORE the disposition copy in source order — the
    underwriter's eye lands on it before the recommendation prose.
  * The deprecated § 5¾ ``post-fund-outcome-section`` block is gone.
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


def _render(*, decision_id: str | None) -> str:
    """Render the dossier with the minimum context the CTA block reads."""
    template = templates.get_template("merchant_detail_dossier.html.j2")
    return template.render(
        request=None,
        merchant=_merchant(),
        documents=[],
        document=None,
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
        show_override_button=False,
        override_pattern_codes=[],
        override_latest_decision_id=decision_id,
    )


def test_cta_visible_when_decision_recorded() -> None:
    decision_id = str(uuid4())
    html = _render(decision_id=decision_id)
    assert 'data-test-id="record-outcome-cta"' in html
    assert 'data-test-id="record-deal-outcome-btn"' in html
    # btn-primary class on the prominent CTA (aegis-system.css ~L520).
    assert 'class="btn btn-primary record-deal-outcome-btn"' in html
    # HTMX wiring points at the in-section swap host.
    assert 'hx-target="#deal-outcome-modal-host"' in html
    assert f"/decisions/{decision_id}/outcome-modal" in html


def test_cta_absent_without_decision() -> None:
    """No decision_id → no FK target → no CTA (would 400 on submit)."""
    html = _render(decision_id=None)
    assert 'data-test-id="record-outcome-cta"' not in html
    assert 'data-test-id="record-deal-outcome-btn"' not in html


def test_cta_renders_before_disposition_copy() -> None:
    """Prominence contract: CTA must appear BEFORE the disposition <h3>
    in source order so the operator's eye lands on it first."""
    decision_id = str(uuid4())
    html = _render(decision_id=decision_id)
    cta_pos = html.find('data-test-id="record-outcome-cta"')
    awaiting_pos = html.find("Awaiting analysis.")
    assert cta_pos != -1, "CTA missing from render"
    assert awaiting_pos != -1, "disposition copy missing from render"
    assert cta_pos < awaiting_pos, (
        f"CTA must appear before disposition copy (cta@{cta_pos}, copy@{awaiting_pos})"
    )


def test_deprecated_post_fund_section_removed() -> None:
    """The old § 5¾ ``post-fund-outcome-section`` block is gone — the
    prominent CTA at the top of § 5 replaces it."""
    decision_id = str(uuid4())
    html = _render(decision_id=decision_id)
    assert 'data-test-id="post-fund-outcome-section"' not in html
    assert "§ 5¾" not in html
