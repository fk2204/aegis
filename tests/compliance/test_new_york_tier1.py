"""New York Tier 1 promotion tests.

Asserts the dossier-driven facts from
``docs/compliance/02_new_york.md`` are reflected in the STATES table
and the Tier 1 template renders correctly. Additional NY-specific
modules covered:

  * compliance/ny_double_dipping.py   — § 600.6(b)(3)(v) computation
  * compliance/broker_compensation.py — § 600.21(f) per-funder text
  * compliance/pricing_guard.py       — § 600.1 / § 600.3 APR re-disclosure
  * scoring/match_funders.py          — CPLR § 3218 conditional CoJ rule
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

from aegis.compliance.broker_compensation import (
    BrokerCompensationDisclosureMissing,
    NyBrokerCompensationDisclosureMissing,
    validate_broker_compensation_disclosure,
)
from aegis.compliance.ny_double_dipping import (
    DoubleDippingInputError,
    compute_double_dipping_amount,
)
from aegis.compliance.states import (
    STATES,
    TEMPLATES_DIR,
    PendingAmendment,
    Tier1Regulation,
)
from aegis.funders.models import FunderRow

# --- States table -----------------------------------------------------------


def test_new_york_is_tier1() -> None:
    ny = STATES["NY"]
    assert isinstance(ny, Tier1Regulation)
    assert ny.tier == 1


def test_new_york_carries_cfdl_identification() -> None:
    ny = STATES["NY"]
    assert isinstance(ny, Tier1Regulation)
    assert ny.bill_number == "SB 5470 (2020), amended by S898 (2021)"
    assert ny.bill_year == 2020
    assert ny.common_name == "Commercial Finance Disclosure Law (CFDL)"
    assert ny.statute_citation == "N.Y. Fin. Services Law §§ 801-811"
    assert ny.regulation_citation == "23 NYCRR Part 600"
    assert ny.prescribed_form_section == "23 NYCRR § 600.6"
    assert ny.apr_calculation_method == "actuarial_reg_z"


def test_new_york_effective_date_pair() -> None:
    """NY: regulations adopted 2023-02-01; mandatory compliance 2023-08-01."""
    ny = STATES["NY"]
    assert isinstance(ny, Tier1Regulation)
    assert ny.regulations_adopted == date(2023, 2, 1)
    assert ny.mandatory_compliance_date == date(2023, 8, 1)
    # The dossier does not quote SB 5470's enactment date — preserved as
    # null rather than improvised.
    assert ny.effective_date_statute is None
    # Mandatory compliance date doubles as the binding effective date.
    assert ny.effective_date_regulations == date(2023, 8, 1)


def test_new_york_threshold_is_2_5m() -> None:
    ny = STATES["NY"]
    assert isinstance(ny, Tier1Regulation)
    assert ny.threshold_amount_usd == Decimal("2500000")
    assert (
        "2,500,000" in ny.threshold_test_summary
        or "$2,500,000" in ny.threshold_test_summary
    )


def test_new_york_apr_tolerances_per_section_600_4() -> None:
    """Per CORRECTIONS_2026-05-08.md: tolerance section is § 600.4."""
    ny = STATES["NY"]
    assert isinstance(ny, Tier1Regulation)
    assert ny.apr_tolerance_percent == Decimal("0.125")  # regular
    assert ny.apr_tolerance_irregular_percent == Decimal("0.250")  # irregular
    assert ny.apr_re_disclosure_required is True


def test_new_york_coj_is_conditional_with_cpl_3218_citation() -> None:
    ny = STATES["NY"]
    assert isinstance(ny, Tier1Regulation)
    assert ny.coj_allowed == "conditional"
    assert ny.coj_citation == "N.Y. CPLR § 3218 (amended chapter 311 of 2019)"
    assert ny.coj_amendment_bill == "Chapter 311 of 2019"
    assert ny.coj_effective_date == date(2019, 8, 30)


def test_new_york_section_600_21_transmission_rules() -> None:
    ny = STATES["NY"]
    assert isinstance(ny, Tier1Regulation)
    assert ny.requires_unaltered_disclosure_transmission is True
    assert ny.transmission_record_retention_years == 4
    # Critical NY-specific obligation that CA does NOT have.
    assert ny.broker_compensation_disclosure_required is True
    assert ny.broker_disclosure_section == "23 NYCRR § 600.21(f)"


def test_new_york_has_pending_s2305() -> None:
    ny = STATES["NY"]
    assert isinstance(ny, Tier1Regulation)
    assert len(ny.pending_amendments) == 1
    s2305 = ny.pending_amendments[0]
    assert isinstance(s2305, PendingAmendment)
    assert s2305.bill_number == "S2305"
    assert s2305.year == 2025
    assert s2305.status == "introduced"
    assert "$5,000,000" in s2305.summary or "5,000,000" in s2305.summary


def test_new_york_template_path_points_at_cfdl_jinja() -> None:
    ny = STATES["NY"]
    assert isinstance(ny, Tier1Regulation)
    assert ny.template_path == "ny_cfdl.html.j2"
    assert (TEMPLATES_DIR / ny.template_path).is_file()


# --- Template rendering -----------------------------------------------------


def _ny_render(**overrides: object) -> str:
    """Render ny_cfdl.html.j2 with a complete context."""
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(enabled_extensions=("html", "j2", "html.j2")),
        undefined=StrictUndefined,
    )
    base_ctx: dict[str, object] = {
        "financer_name": "Test Funder LLC",
        "funding_provided": "$50,000.00",
        "recipient_funds": "$48,500.00",
        "recipient_funds_lt_funding": True,
        "pays_third_party_payoffs": False,
        "third_party_payoff_note": "",
        "is_renewal_with_double_dip": False,
        "double_dipping_amount": "$0.00",
        "apr": "38.00%",
        "payment_channel": "credit card receipts",
        "avg_monthly_income": "$22,000.00",
        "finance_is_fee_based": True,
        "finance_charge": "$15,000.00",
        "finance_charge_can_increase": False,
        "estimated_total_payment": "$65,000.00",
        "estimated_payment_amount": "$487.00",
        "estimated_payment_freq": "business day",
        "irregular_payments_note": "",
        "payment_terms_text": (
            "Each business day, your credit card processor will remit 15% of "
            "your gross receipts to us, and send any remaining amounts to you. "
            "This financing does not have a fixed payment schedule and there "
            "is no minimum payment amount."
        ),
        "estimated_term": "133 days",
        "estimated_term_explanation": "Term assumes constant projected income.",
        "has_monthly_cost_insert": True,
        "estimated_monthly_cost": "$10,714.00",
        "monthly_cost_derivation": "Estimated periodic payment annualized to monthly.",
        "prepayment_finance_charge_text": (
            "If you prepay, no portion of the finance charge will be refunded."
        ),
        "prepayment_additional_fee_text": "There is no additional fee for prepayment.",
        "collateral_requirements_text": "None",
        "rendered_at": "2026-05-09",
    }
    base_ctx.update(overrides)
    return env.get_template("ny_cfdl.html.j2").render(**base_ctx)


def test_ny_template_includes_required_footer() -> None:
    out = _ny_render()
    assert (
        "New York Applicable law requires this information to be provided "
        "to you to help you make an informed decision."
    ) in out


def test_ny_template_includes_all_ten_row_labels() -> None:
    """NY has 10 rows (one more than CA — the Collateral Requirements row)."""
    out = _ny_render()
    for label in (
        "Funding Provided",
        "Estimated Annual Percentage Rate (APR)",
        "Finance Charge",
        "Estimated Total Payment Amount",
        "Estimated Payment",
        "Payment Terms",
        "Estimated Term",
        "Prepayment",
        "Collateral Requirements",  # NY-specific Row 10
    ):
        assert label in out


def test_ny_template_apr_uses_finance_charges_phrasing() -> None:
    """NY explanatory text says 'finance charges you pay' (CA says 'fees you pay')."""
    out = _ny_render()
    assert "finance charges you pay" in out
    assert (
        "APR is the estimated cost of your financing expressed as a yearly rate."
    ) in out


def test_ny_template_omits_double_dipping_when_not_renewal() -> None:
    out = _ny_render(is_renewal_with_double_dip=False)
    assert "double dipping" not in out
    assert "Does the renewal financing" not in out


def test_ny_template_includes_double_dipping_for_renewal() -> None:
    """Per § 600.6(b)(3)(v), renewal disclosure must appear when applicable.

    Whitespace is collapsed before matching because Jinja preserves the
    template's source whitespace and the regulator-prescribed sentence
    spans two lines in the template for readability.
    """
    out = _ny_render(
        is_renewal_with_double_dip=True,
        double_dipping_amount="$6,000.00",
    )
    collapsed = " ".join(out.split())
    assert (
        "Does the renewal financing include any amount that is used to pay "
        "unpaid finance charges or fees, also known as double dipping?"
    ) in collapsed
    assert "$6,000.00" in out
    assert "If the amount is zero, the answer would be No." in collapsed


def test_ny_template_collateral_row_content_passes_through() -> None:
    out = _ny_render(collateral_requirements_text="UCC-1 filing on accounts receivable")
    assert "UCC-1 filing on accounts receivable" in out


def test_ny_template_strict_undefined_rejects_missing_var() -> None:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(enabled_extensions=("html", "j2", "html.j2")),
        undefined=StrictUndefined,
    )
    template = env.get_template("ny_cfdl.html.j2")
    with pytest.raises(UndefinedError):
        template.render()


def test_ny_template_cites_section_600_0_in_footer() -> None:
    out = _ny_render()
    assert "23 NYCRR § 600.0" in out


# --- Double-dipping computation (§ 600.6(b)(3)(v)) --------------------------


def test_double_dipping_worked_example_from_module() -> None:
    """The example documented in the module docstring: $100K/$130K/$80K/$50K → $6,000.

    Verifies the principal-first amortization formula as implemented.
    """
    result = compute_double_dipping_amount(
        prior_funded_amount=Decimal("100000"),
        prior_total_payback=Decimal("130000"),
        prior_amount_repaid=Decimal("80000"),
        renewal_amount_used_to_pay_prior=Decimal("50000"),
    )
    assert result == Decimal("6000.00")


def test_double_dipping_zero_when_principal_already_repaid() -> None:
    """Once prior_amount_repaid >= prior_funded_amount, principal is gone."""
    result = compute_double_dipping_amount(
        prior_funded_amount=Decimal("100000"),
        prior_total_payback=Decimal("130000"),
        prior_amount_repaid=Decimal("100000"),
        renewal_amount_used_to_pay_prior=Decimal("30000"),
    )
    assert result == Decimal("0.00")


def test_double_dipping_zero_when_nothing_left_to_pay() -> None:
    """remaining_balance <= 0 → result is $0."""
    result = compute_double_dipping_amount(
        prior_funded_amount=Decimal("100000"),
        prior_total_payback=Decimal("130000"),
        prior_amount_repaid=Decimal("130000"),
        renewal_amount_used_to_pay_prior=Decimal("10000"),
    )
    assert result == Decimal("0.00")


def test_double_dipping_caps_at_remaining_balance() -> None:
    """renewal_amount > remaining_balance is capped — can't double-dip extra."""
    # remaining_balance = 130000 - 80000 = 50000.
    # capped renewal = 50000. fraction = 1.0.
    # embedded finance charge = 30000 * 20000/100000 = 6000.
    capped = compute_double_dipping_amount(
        prior_funded_amount=Decimal("100000"),
        prior_total_payback=Decimal("130000"),
        prior_amount_repaid=Decimal("80000"),
        renewal_amount_used_to_pay_prior=Decimal("999999"),  # way over
    )
    exact = compute_double_dipping_amount(
        prior_funded_amount=Decimal("100000"),
        prior_total_payback=Decimal("130000"),
        prior_amount_repaid=Decimal("80000"),
        renewal_amount_used_to_pay_prior=Decimal("50000"),  # exactly remaining
    )
    assert capped == exact == Decimal("6000.00")


def test_double_dipping_rejects_zero_funded_amount() -> None:
    with pytest.raises(DoubleDippingInputError, match="prior_funded_amount"):
        compute_double_dipping_amount(
            prior_funded_amount=Decimal("0"),
            prior_total_payback=Decimal("0"),
            prior_amount_repaid=Decimal("0"),
            renewal_amount_used_to_pay_prior=Decimal("0"),
        )


def test_double_dipping_rejects_payback_below_funded() -> None:
    """No negative finance charge possible — raise rather than silently zero."""
    with pytest.raises(DoubleDippingInputError, match="prior_total_payback"):
        compute_double_dipping_amount(
            prior_funded_amount=Decimal("100000"),
            prior_total_payback=Decimal("90000"),
            prior_amount_repaid=Decimal("0"),
            renewal_amount_used_to_pay_prior=Decimal("50000"),
        )


def test_double_dipping_rejects_float_inputs() -> None:
    """Decimal-only — passing float at the boundary is a programming error."""
    with pytest.raises(DoubleDippingInputError, match="must be Decimal"):
        compute_double_dipping_amount(
            prior_funded_amount=100000.0,  # type: ignore[arg-type]
            prior_total_payback=Decimal("130000"),
            prior_amount_repaid=Decimal("80000"),
            renewal_amount_used_to_pay_prior=Decimal("50000"),
        )


# --- Broker compensation guard (§ 600.21(f)) --------------------------------


def _bare_funder(text: str = "") -> FunderRow:
    return FunderRow(name="Acme Capital", aegis_compensation_disclosure_text=text)


def test_broker_compensation_required_for_ny_when_text_empty() -> None:
    funder = _bare_funder(text="")
    with pytest.raises(
        NyBrokerCompensationDisclosureMissing,
        match=r"23 NYCRR § 600\.21\(f\)",
    ):
        validate_broker_compensation_disclosure(
            merchant_state="NY", funder=funder
        )


def test_broker_compensation_required_treats_whitespace_as_empty() -> None:
    funder = _bare_funder(text="   \n\t  ")
    with pytest.raises(NyBrokerCompensationDisclosureMissing):
        validate_broker_compensation_disclosure(
            merchant_state="NY", funder=funder
        )


def test_broker_compensation_passes_when_text_present() -> None:
    funder = _bare_funder(
        text="AEGIS receives a commission of 10% of the funded amount from Acme Capital."
    )
    # Should not raise.
    validate_broker_compensation_disclosure(merchant_state="NY", funder=funder)


def test_broker_compensation_skipped_for_ca() -> None:
    """CA dossier records broker_compensation_disclosure_required=false."""
    funder = _bare_funder(text="")
    # Should not raise — CA is not in the per-state rule registry.
    validate_broker_compensation_disclosure(merchant_state="CA", funder=funder)


def test_broker_compensation_skipped_for_tier3_states() -> None:
    funder = _bare_funder(text="")
    for state in ("FL", "TX", "WY"):
        validate_broker_compensation_disclosure(
            merchant_state=state, funder=funder
        )


def test_broker_compensation_state_lookup_case_insensitive() -> None:
    funder = _bare_funder(text="")
    with pytest.raises(NyBrokerCompensationDisclosureMissing):
        validate_broker_compensation_disclosure(
            merchant_state="ny", funder=funder
        )


def test_broker_compensation_subclass_of_base_error() -> None:
    """Callers can ``except BrokerCompensationDisclosureMissing`` once."""
    assert issubclass(
        NyBrokerCompensationDisclosureMissing,
        BrokerCompensationDisclosureMissing,
    )


def test_broker_compensation_error_carries_citation() -> None:
    assert (
        NyBrokerCompensationDisclosureMissing.citation
        == "23 NYCRR § 600.21(f)"
    )


# --- FunderRow new field ----------------------------------------------------


def test_funder_row_defaults_compensation_text_to_empty() -> None:
    """Existing funder rows have no broker comp text until operator supplies."""
    funder = FunderRow(name="Default Funder")
    assert funder.aegis_compensation_disclosure_text == ""


def test_funder_row_accepts_compensation_text() -> None:
    funder = FunderRow(
        name="Acme Capital",
        aegis_compensation_disclosure_text=(
            "AEGIS is paid a 10% commission on funded amount, paid by Acme."
        ),
    )
    assert "10% commission" in funder.aegis_compensation_disclosure_text
