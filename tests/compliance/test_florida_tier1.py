"""Florida Tier 1 promotion tests.

Asserts the dossier-driven facts from
``docs/compliance/03_florida.md`` are reflected in the STATES table
and the Tier 1 template renders correctly. Additional FL-specific
behavior covered:

  * compliance/states.py        — schema extensions for FL (apr_required,
    broker_advance_fees_prohibited, broker_advertisement_address_disclosure_required,
    private_right_of_action, enforcement_authority; optional regulation_*
    + coj_effective_date + prescribed_form_section)
  * funders/models.py           — charges_merchant_advance_fees field
  * fl_fcfdl.html.j2 template   — content-based, no APR, no prescribed
    table format, six required content items
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from jinja2 import (
    Environment,
    FileSystemLoader,
    StrictUndefined,
    UndefinedError,
    select_autoescape,
)

from aegis.compliance.states import STATES, TEMPLATES_DIR, Tier1Regulation
from aegis.funders.models import FunderRow

# --- States table -----------------------------------------------------------


def test_florida_is_tier1() -> None:
    fl = STATES["FL"]
    assert isinstance(fl, Tier1Regulation)
    assert fl.tier == 1


def test_florida_carries_fcfdl_identification() -> None:
    fl = STATES["FL"]
    assert isinstance(fl, Tier1Regulation)
    assert fl.bill_number == "HB 1353"
    assert fl.bill_year == 2023
    assert fl.chapter == "Chapter 2023-290, Laws of Florida"
    assert fl.common_name == "Florida Commercial Financing Disclosure Law (FCFDL)"
    assert fl.statute_citation == (
        "Fla. Stat. §§ 559.961 - 559.9615 (Part XIII of Chapter 559)"
    )


def test_florida_effective_dates_per_dossier() -> None:
    """Statute effective 2023-07-01; mandatory compliance 2024-01-01."""
    fl = STATES["FL"]
    assert isinstance(fl, Tier1Regulation)
    assert fl.effective_date_statute == date(2023, 7, 1)
    assert fl.mandatory_compliance_date == date(2024, 1, 1)
    assert fl.effective_date_regulations == date(2024, 1, 1)


def test_florida_threshold_is_500k() -> None:
    fl = STATES["FL"]
    assert isinstance(fl, Tier1Regulation)
    assert fl.threshold_amount_usd == Decimal("500000")
    assert fl.threshold_test_summary is not None
    assert (
        "500,000" in fl.threshold_test_summary
        or "$500,000" in fl.threshold_test_summary
    )
    # FL adds a small-volume safe harbor (>5 transactions/year).
    assert "5" in fl.threshold_test_summary


def test_florida_is_content_based_not_form_prescribed() -> None:
    """FL § 559.9613 lists six required items — does NOT prescribe a form."""
    fl = STATES["FL"]
    assert isinstance(fl, Tier1Regulation)
    assert fl.prescribed_form_section is None
    # No separate body of regulations either — statute self-executing.
    assert fl.regulation_citation is None
    assert fl.citation_url_regulation is None


def test_florida_does_not_require_apr() -> None:
    """FL is lighter than CA / NY / GA — no APR disclosure obligation."""
    fl = STATES["FL"]
    assert isinstance(fl, Tier1Regulation)
    assert fl.apr_required is False
    assert fl.apr_calculation_method == "not_required"
    # No re-disclosure rule either.
    assert fl.apr_re_disclosure_required is False


def test_florida_coj_banned_with_section_55_05_citation() -> None:
    fl = STATES["FL"]
    assert isinstance(fl, Tier1Regulation)
    assert fl.coj_allowed == "banned"
    assert fl.coj_citation == "Fla. Stat. § 55.05"
    # Historic statute — no specific amendment date in the dossier.
    assert fl.coj_amendment_bill is None
    assert fl.coj_effective_date is None


def test_florida_broker_advance_fees_prohibited() -> None:
    """Per § 559.9614(1)(a) — FL-specific obligation neither CA nor NY has."""
    fl = STATES["FL"]
    assert isinstance(fl, Tier1Regulation)
    assert fl.broker_advance_fees_prohibited is True


def test_florida_broker_advertisement_address_disclosure_required() -> None:
    """Per § 559.9614(3) — FL marketing copy must show real address + phone."""
    fl = STATES["FL"]
    assert isinstance(fl, Tier1Regulation)
    assert fl.broker_advertisement_address_disclosure_required is True


def test_florida_no_broker_compensation_disclosure() -> None:
    """FL is silent on broker compensation — that's NY-specific (§ 600.21(f))."""
    fl = STATES["FL"]
    assert isinstance(fl, Tier1Regulation)
    assert fl.broker_compensation_disclosure_required is False


def test_florida_no_transmission_duty() -> None:
    """FL has no parallel to CA § 952 / NY § 600.21 transmission rules."""
    fl = STATES["FL"]
    assert isinstance(fl, Tier1Regulation)
    assert fl.requires_unaltered_disclosure_transmission is False
    assert fl.transmission_record_retention_years == 0


def test_florida_enforcement_ag_only_no_pra() -> None:
    fl = STATES["FL"]
    assert isinstance(fl, Tier1Regulation)
    assert fl.private_right_of_action is False
    assert fl.enforcement_authority == "Florida Attorney General (exclusive)"


def test_florida_template_path_points_at_fcfdl_jinja() -> None:
    fl = STATES["FL"]
    assert isinstance(fl, Tier1Regulation)
    assert fl.template_path == "fl_fcfdl.html.j2"
    assert (TEMPLATES_DIR / fl.template_path).is_file()


# --- Template rendering -----------------------------------------------------


def _fl_render(**overrides: object) -> str:
    """Render fl_fcfdl.html.j2 with a complete context."""
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(enabled_extensions=("html", "j2", "html.j2")),
        undefined=StrictUndefined,
    )
    base_ctx: dict[str, object] = {
        "financer_name": "Test Funder LLC",
        "funds_provided": "$50,000.00",
        "funds_disbursed_to_business": "$48,500.00",
        "funds_disbursed_lt_provided": True,
        "deductions_explanation": (
            "$1,500.00 was withheld at disbursement to satisfy a prior "
            "advance from Test Funder LLC."
        ),
        "total_amount_business_must_pay": "$65,000.00",
        "total_dollar_cost": "$15,000.00",
        "payment_amounts_may_vary": False,
        "payment_manner": "ACH debit",
        "payment_frequency": "each business day",
        "payment_amount_or_first_estimate": "$487.00",
        "has_prepayment_costs_or_discounts": False,
        "prepayment_terms_text": "",
        "prepayment_contract_provision_ref": (
            "Section 7.3 of the Master Financing Agreement"
        ),
        "rendered_at": "2026-05-09",
    }
    base_ctx.update(overrides)
    return env.get_template("fl_fcfdl.html.j2").render(**base_ctx)


def test_fl_template_includes_all_six_required_content_items() -> None:
    """Per § 559.9613(2) — all six items must be present, regardless of layout."""
    out = _fl_render()
    for label in (
        "Total amount of funds provided",                       # (a)
        "Total amount of funds disbursed to your business",     # (b)
        "Total amount your business must pay",                  # (c)
        "Total dollar cost of this commercial financing",       # (d)
        "Manner, frequency, and amount of payments",            # (e)
        "Prepayment",                                           # (f)
    ):
        assert label in out, f"required content item missing: {label!r}"


def test_fl_template_omits_apr_section() -> None:
    """FL does NOT require APR disclosure; template must not include one."""
    out = _fl_render()
    assert "APR" not in out
    assert "annual percentage rate" not in out.lower()


def test_fl_template_uses_definition_list_not_table() -> None:
    """Content-based, not form-prescribed — no <table> for the six items."""
    out = _fl_render()
    assert "<dl>" in out
    assert "</dl>" in out
    # Sanity: the body uses <dt>/<dd>, not <table>/<tr>/<td>.
    assert "<table" not in out
    assert "<tr>" not in out


def test_fl_template_cites_section_559_9613_in_footer() -> None:
    out = _fl_render()
    assert "Fla. Stat. § 559.9613" in out


def test_fl_template_renders_total_dollar_cost_field() -> None:
    """Item (d) is the finance-charge equivalent — must surface the value."""
    out = _fl_render(total_dollar_cost="$15,000.00")
    assert "$15,000.00" in out


def test_fl_template_includes_deductions_explanation_when_funds_lt_provided() -> None:
    out = _fl_render(
        funds_disbursed_lt_provided=True,
        deductions_explanation="Test deduction explanation 12345",
    )
    assert "Test deduction explanation 12345" in out


def test_fl_template_omits_deductions_explanation_when_no_deductions() -> None:
    out = _fl_render(
        funds_disbursed_lt_provided=False,
        funds_disbursed_to_business="$50,000.00",
        deductions_explanation="",
    )
    # The explanation block is gated; absent when disbursed == provided.
    assert "withheld at disbursement" not in out


def test_fl_template_payment_first_estimate_when_amounts_vary() -> None:
    """Per § 559.9613(2)(e) second clause: variable-amount → first-payment estimate."""
    out = _fl_render(
        payment_amounts_may_vary=True,
        payment_amount_or_first_estimate="$487.00",
    )
    assert "Payment amounts may vary" in out
    assert "estimated amount of the first payment" in out
    assert "$487.00" in out


def test_fl_template_payment_fixed_when_amounts_dont_vary() -> None:
    out = _fl_render(payment_amounts_may_vary=False)
    assert "Payment amounts may vary" not in out
    assert "amount of each payment is" in out


def test_fl_template_prepayment_contract_ref_always_required() -> None:
    """Per § 559.9613(2)(f): contract reference is always required, even when
    there are no prepayment costs/discounts (merchant still has the rights)."""
    out_with = _fl_render(
        has_prepayment_costs_or_discounts=True,
        prepayment_terms_text="2% discount if paid within 30 days.",
    )
    out_without = _fl_render(
        has_prepayment_costs_or_discounts=False,
        prepayment_terms_text="",
    )
    for out in (out_with, out_without):
        assert "Section 7.3 of the Master Financing Agreement" in out


def test_fl_template_prepayment_text_only_when_costs_or_discounts() -> None:
    out_with = _fl_render(
        has_prepayment_costs_or_discounts=True,
        prepayment_terms_text="DISTINCTIVE PREPAY MARKER 999",
    )
    assert "DISTINCTIVE PREPAY MARKER 999" in out_with
    out_without = _fl_render(
        has_prepayment_costs_or_discounts=False,
        prepayment_terms_text="DISTINCTIVE PREPAY MARKER 999",
    )
    assert "DISTINCTIVE PREPAY MARKER 999" not in out_without
    assert "no costs or discounts associated with prepayment" in out_without


def test_fl_template_strict_undefined_rejects_missing_var() -> None:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(enabled_extensions=("html", "j2", "html.j2")),
        undefined=StrictUndefined,
    )
    template = env.get_template("fl_fcfdl.html.j2")
    with pytest.raises(UndefinedError):
        template.render()


# --- FunderRow new field ----------------------------------------------------


def test_funder_row_defaults_charges_merchant_advance_fees_false() -> None:
    """Existing funder rows are assumed to NOT charge advance fees."""
    funder = FunderRow(name="Default Funder")
    assert funder.charges_merchant_advance_fees is False


def test_funder_row_accepts_charges_merchant_advance_fees_true() -> None:
    funder = FunderRow(name="Aggressive Funder", charges_merchant_advance_fees=True)
    assert funder.charges_merchant_advance_fees is True


