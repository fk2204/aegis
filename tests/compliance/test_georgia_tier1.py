"""Georgia Tier 1 promotion tests.

Asserts the dossier-driven facts from
``docs/compliance/04_georgia.md`` (with the citation correction from
``docs/compliance/CORRECTIONS_2026-05-08.md``) are reflected in the
STATES table and the Tier 1 template renders correctly. Additional
GA-specific behavior covered:

  * compliance/states.py     — schema additions for GA (signed_by,
    signed_date, coj_venue_restriction, enforcement_framework,
    receivable_purchase_statutorily_protected)
  * ga_sb90.html.j2 template — content-based with APR row (7 items)

Citation precision per CORRECTIONS Correction 1: cite § 10-1-393.18
as a single section, NOT "et seq.".
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

# --- States table -----------------------------------------------------------


def test_georgia_is_tier1() -> None:
    ga = STATES["GA"]
    assert isinstance(ga, Tier1Regulation)
    assert ga.tier == 1


def test_georgia_carries_sb90_identification() -> None:
    ga = STATES["GA"]
    assert isinstance(ga, Tier1Regulation)
    assert ga.bill_number == "SB 90"
    assert ga.bill_year == 2023
    assert ga.chapter == "Act 217 of the 2023 Regular Session"
    assert ga.common_name == "Georgia Commercial Financing Disclosure Law"
    assert ga.signed_by == "Gov. Kemp"
    assert ga.signed_date == date(2023, 5, 1)


def test_georgia_statute_citation_per_corrections() -> None:
    """CORRECTIONS Correction 1: cite § 10-1-393.18 as single section.

    Not "et seq." — the dossier-internal references to § 10-1-393.19 in
    the dossier body are the typo the CORRECTIONS file flags. The
    authoritative cite is § 10-1-393.18.
    """
    ga = STATES["GA"]
    assert isinstance(ga, Tier1Regulation)
    assert "§ 10-1-393.18" in ga.statute_citation
    assert "et seq" not in ga.statute_citation.lower()
    # The dossier explicitly notes "single section, multiple subsections".
    assert "single section" in ga.statute_citation


def test_georgia_effective_date_and_no_separate_regulations() -> None:
    """Statute effective 2024-01-01; no separate body of regulations."""
    ga = STATES["GA"]
    assert isinstance(ga, Tier1Regulation)
    assert ga.effective_date_statute == date(2024, 1, 1)
    assert ga.effective_date_regulations == date(2024, 1, 1)
    assert ga.regulation_citation is None
    assert ga.citation_url_regulation is None


def test_georgia_threshold_is_500k() -> None:
    ga = STATES["GA"]
    assert isinstance(ga, Tier1Regulation)
    assert ga.threshold_amount_usd == Decimal("500000")
    assert ga.threshold_test_summary is not None
    assert (
        "500,000" in ga.threshold_test_summary
        or "$500,000" in ga.threshold_test_summary
    )
    # Same small-volume safe harbor as FL: > 5 transactions / year.
    assert "5" in ga.threshold_test_summary


def test_georgia_is_content_based_with_apr() -> None:
    """GA differs from FL: content-based BUT APR is required."""
    ga = STATES["GA"]
    assert isinstance(ga, Tier1Regulation)
    assert ga.prescribed_form_section is None
    assert ga.apr_required is True
    assert ga.apr_calculation_method == "actuarial_reg_z"
    # No SB 362-equivalent re-disclosure rule per dossier.
    assert ga.apr_re_disclosure_required is False


def test_georgia_coj_allowed_with_venue_restriction() -> None:
    """GA permits CoJ — first 'allowed' state in the table."""
    ga = STATES["GA"]
    assert isinstance(ga, Tier1Regulation)
    assert ga.coj_allowed == "allowed"
    assert ga.coj_citation == "O.C.G.A. § 9-12-18"
    # Venue restriction recorded for operator visibility.
    assert ga.coj_venue_restriction is not None
    assert "county" in ga.coj_venue_restriction.lower()
    assert "defendant" in ga.coj_venue_restriction.lower()


def test_georgia_broker_advance_fees_prohibited() -> None:
    """Same prohibition pattern as FL — § 10-1-393.18 broker rules."""
    ga = STATES["GA"]
    assert isinstance(ga, Tier1Regulation)
    assert ga.broker_advance_fees_prohibited is True
    # GA does NOT impose CA / NY-style broker disclosures.
    assert ga.broker_compensation_disclosure_required is False
    assert ga.broker_advertisement_address_disclosure_required is False


def test_georgia_no_transmission_duty() -> None:
    """No GA parallel to CA § 952 / NY § 600.21 transmission rules."""
    ga = STATES["GA"]
    assert isinstance(ga, Tier1Regulation)
    assert ga.requires_unaltered_disclosure_transmission is False
    assert ga.transmission_record_retention_years == 0


def test_georgia_enforcement_ag_only_under_fbpa() -> None:
    ga = STATES["GA"]
    assert isinstance(ga, Tier1Regulation)
    assert ga.private_right_of_action is False
    assert ga.enforcement_authority == "Georgia Attorney General (exclusive)"
    assert ga.enforcement_framework == "Fair Business Practices Act"


def test_georgia_penalties_per_corrections() -> None:
    """Penalty amounts verified verbatim per CORRECTIONS file.

    First-time: $500/violation, $20K aggregate. After notice: $1,000/
    violation, $50K aggregate. Recorded in the notes string.
    """
    ga = STATES["GA"]
    assert isinstance(ga, Tier1Regulation)
    notes = ga.notes
    assert "$500/violation" in notes
    assert "$20K aggregate" in notes
    assert "$1,000/violation" in notes
    assert "$50K aggregate" in notes


def test_georgia_receivable_purchase_statutorily_protected() -> None:
    """GA-specific defensive posture for MCA reclassification (dossier 12)."""
    ga = STATES["GA"]
    assert isinstance(ga, Tier1Regulation)
    assert ga.receivable_purchase_statutorily_protected is True


@pytest.mark.parametrize("abbr", ["CA", "NY", "FL"])
def test_other_tier1_states_default_receivable_purchase_field_to_false(
    abbr: str,
) -> None:
    """Default value: only GA has the statutory protection in the table today."""
    reg = STATES[abbr]
    assert isinstance(reg, Tier1Regulation)
    assert reg.receivable_purchase_statutorily_protected is False


def test_georgia_template_path_points_at_sb90_jinja() -> None:
    ga = STATES["GA"]
    assert isinstance(ga, Tier1Regulation)
    assert ga.template_path == "ga_sb90.html.j2"
    assert (TEMPLATES_DIR / ga.template_path).is_file()


# --- Template rendering -----------------------------------------------------


def _ga_render(**overrides: object) -> str:
    """Render ga_sb90.html.j2 with a complete context."""
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
        "total_amount_to_be_paid": "$65,000.00",
        "total_dollar_cost": "$15,000.00",
        "apr": "36.50%",
        "payment_amounts_may_vary": False,
        "payment_manner": "ACH debit",
        "payment_frequency": "each business day",
        "payment_amount_or_first_estimate": "$487.00",
        "has_prepayment_costs_or_discounts": False,
        "prepayment_terms_text": "",
        "rendered_at": "2026-05-09",
    }
    base_ctx.update(overrides)
    return env.get_template("ga_sb90.html.j2").render(**base_ctx)


def test_ga_template_includes_all_seven_required_content_items() -> None:
    """7 required content items per dossier, including APR (item 5)."""
    out = _ga_render()
    for label in (
        "Total amount of funds provided",
        "Total amount of funds disbursed to your business",
        "Total amount your business will pay",
        "Total dollar cost of this commercial financing",
        "Annual Percentage Rate (APR)",  # GA-specific vs FL
        "Payment schedule",
        "Prepayment terms",
    ):
        assert label in out, f"required content item missing: {label!r}"


def test_ga_template_renders_apr_value() -> None:
    """APR is the GA-specific addition vs FL — must surface the value."""
    out = _ga_render(apr="42.75%")
    assert "42.75%" in out


def test_ga_template_uses_definition_list_not_table() -> None:
    """Content-based, not form-prescribed — same shape as FL template."""
    out = _ga_render()
    assert "<dl>" in out
    assert "</dl>" in out
    assert "<table" not in out
    assert "<tr>" not in out


def test_ga_template_cites_section_10_1_393_18_in_footer_not_et_seq() -> None:
    """Per CORRECTIONS Correction 1: cite single section, not 'et seq.'"""
    out = _ga_render()
    assert "O.C.G.A. § 10-1-393.18" in out
    assert "et seq" not in out.lower()
    # And not the typo'd § 10-1-393.19 either.
    assert "393.19" not in out


def test_ga_template_payment_first_estimate_when_amounts_vary() -> None:
    out = _ga_render(
        payment_amounts_may_vary=True,
        payment_amount_or_first_estimate="$487.00",
    )
    assert "Payment amounts may vary" in out
    assert "estimated amount of the first payment" in out


def test_ga_template_prepayment_terms_only_when_costs_or_discounts() -> None:
    out_with = _ga_render(
        has_prepayment_costs_or_discounts=True,
        prepayment_terms_text="GA PREPAY MARKER 777",
    )
    assert "GA PREPAY MARKER 777" in out_with
    out_without = _ga_render(
        has_prepayment_costs_or_discounts=False,
        prepayment_terms_text="GA PREPAY MARKER 777",
    )
    assert "GA PREPAY MARKER 777" not in out_without
    assert "no costs or discounts associated with prepayment" in out_without


def test_ga_template_strict_undefined_rejects_missing_var() -> None:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(enabled_extensions=("html", "j2", "html.j2")),
        undefined=StrictUndefined,
    )
    template = env.get_template("ga_sb90.html.j2")
    with pytest.raises(UndefinedError):
        template.render()


