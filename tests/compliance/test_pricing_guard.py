"""Per-state APR re-disclosure guard tests (CA + NY).

CA cite: Cal. Fin. Code § 22806 (added by SB 362).
  Source: ``docs/compliance/01_california.md``, "SB 362 — what changed
  effective 2026-01-01" → "Architectural impact for AEGIS".

NY cite: 23 NYCRR § 600.1 / § 600.3.
  Source: ``docs/compliance/02_new_york.md``, "What 23 NYCRR § 600.6
  requires for MCA disclosures" → "APR re-disclosure rule".
"""

from __future__ import annotations

import pytest

from aegis.compliance.pricing_guard import (
    CaPricingComplianceError,
    NyPricingComplianceError,
    PricingComplianceError,
    validate_communication,
)

# --- California -------------------------------------------------------------


def test_ca_merchant_with_pricing_keyword_no_apr_raises() -> None:
    with pytest.raises(CaPricingComplianceError, match=r"Cal\. Fin\. Code § 22806"):
        validate_communication(
            merchant_state="CA",
            body="Your offer: factor rate 1.30 over 120 days. Sign attached.",
        )


def test_ca_merchant_with_apr_uppercase_passes() -> None:
    validate_communication(
        merchant_state="CA",
        body="Your offer: factor rate 1.30, APR 36.50%. Sign attached.",
    )


def test_ca_merchant_with_annual_percentage_rate_phrase_passes() -> None:
    validate_communication(
        merchant_state="CA",
        body=(
            "Your offer: factor rate 1.30, "
            "annual percentage rate 36.50%. Sign attached."
        ),
    )


def test_ca_merchant_no_pricing_keyword_passes() -> None:
    """Status update with no pricing reference → no re-disclosure obligation."""
    validate_communication(
        merchant_state="CA",
        body="We've received your bank statement. Underwriting in progress.",
    )


@pytest.mark.parametrize(
    "trigger",
    ["factor rate", "daily payment", "financing amount"],
)
def test_each_dossier_pricing_trigger_requires_apr_ca(trigger: str) -> None:
    with pytest.raises(CaPricingComplianceError):
        validate_communication(
            merchant_state="CA",
            body=f"Your {trigger} is competitive. Sign attached.",
        )


def test_ca_state_lookup_is_case_insensitive() -> None:
    with pytest.raises(CaPricingComplianceError):
        validate_communication(
            merchant_state="ca",
            body="Your offer: factor rate 1.30. Sign attached.",
        )


def test_ca_apr_match_is_word_boundary_not_substring() -> None:
    """'TAPR' or 'APRIL' should not satisfy the APR mention requirement."""
    with pytest.raises(CaPricingComplianceError):
        validate_communication(
            merchant_state="CA",
            body=(
                "Your factor rate is 1.30. The TAPR program is closed; "
                "we'll review again in April."
            ),
        )


def test_ca_error_carries_citation_attribute() -> None:
    """Callers can branch on the citation without parsing the message."""
    assert CaPricingComplianceError.citation == "Cal. Fin. Code § 22806"


# --- New York ---------------------------------------------------------------


def test_ny_merchant_with_pricing_keyword_no_apr_raises() -> None:
    with pytest.raises(
        NyPricingComplianceError, match=r"23 NYCRR § 600\.1 / § 600\.3"
    ):
        validate_communication(
            merchant_state="NY",
            body="Your offer: factor rate 1.30 over 120 days. Sign attached.",
        )


def test_ny_merchant_with_apr_passes() -> None:
    validate_communication(
        merchant_state="NY",
        body="Your offer: factor rate 1.30, APR 38.00%. Sign attached.",
    )


def test_ny_merchant_with_annual_percentage_rate_phrase_passes() -> None:
    validate_communication(
        merchant_state="NY",
        body=(
            "Your offer: factor rate 1.30, "
            "annual percentage rate 38.00%. Sign attached."
        ),
    )


def test_ny_finance_charge_trigger_requires_apr() -> None:
    """NY dossier explicitly enumerates 'finance charge' as a trigger."""
    with pytest.raises(NyPricingComplianceError):
        validate_communication(
            merchant_state="NY",
            body="Your finance charge is $15,000. Sign attached.",
        )


@pytest.mark.parametrize(
    "trigger",
    ["factor rate", "daily payment", "financing amount", "finance charge"],
)
def test_each_dossier_pricing_trigger_requires_apr_ny(trigger: str) -> None:
    with pytest.raises(NyPricingComplianceError):
        validate_communication(
            merchant_state="NY",
            body=f"Your {trigger} is competitive. Sign attached.",
        )


def test_ny_state_lookup_is_case_insensitive() -> None:
    with pytest.raises(NyPricingComplianceError):
        validate_communication(
            merchant_state="ny",
            body="Your offer: factor rate 1.30. Sign attached.",
        )


def test_ny_no_pricing_keyword_passes() -> None:
    validate_communication(
        merchant_state="NY",
        body="We've received your bank statement. Underwriting in progress.",
    )


def test_ny_error_carries_citation_attribute() -> None:
    assert NyPricingComplianceError.citation == "23 NYCRR § 600.1 / § 600.3"


# --- Out-of-scope states ----------------------------------------------------


def test_non_covered_state_with_pricing_no_apr_passes() -> None:
    """States without APR re-disclosure rules are out of scope."""
    validate_communication(
        merchant_state="FL",
        body="Your daily payment is $487 over 120 days. No APR mentioned.",
    )
    validate_communication(
        merchant_state="TX",
        body="Your offer: factor rate 1.30 over 120 days. No APR mentioned.",
    )


# --- Polymorphism -----------------------------------------------------------


def test_state_specific_errors_share_base_class() -> None:
    """Callers can ``except PricingComplianceError`` once across states."""
    assert issubclass(CaPricingComplianceError, PricingComplianceError)
    assert issubclass(NyPricingComplianceError, PricingComplianceError)


@pytest.mark.parametrize(
    "state",
    ["CA", "NY"],
)
def test_base_error_class_catches_either_state(state: str) -> None:
    with pytest.raises(PricingComplianceError):
        validate_communication(
            merchant_state=state,
            body="Your offer: factor rate 1.30. Sign attached.",
        )
