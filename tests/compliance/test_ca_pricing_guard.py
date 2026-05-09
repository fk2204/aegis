"""SB 362 APR re-disclosure guard tests.

Per docs/compliance/01_california.md, section "SB 362 — what changed
effective 2026-01-01" → "Architectural impact for AEGIS." Cite:
Cal. Fin. Code § 22806 (added by SB 362).
"""

from __future__ import annotations

import pytest

from aegis.compliance.ca_pricing_guard import (
    CaPricingComplianceError,
    validate_communication,
)


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
def test_each_dossier_pricing_trigger_requires_apr(trigger: str) -> None:
    with pytest.raises(CaPricingComplianceError):
        validate_communication(
            merchant_state="CA",
            body=f"Your {trigger} is competitive. Sign attached.",
        )


def test_non_ca_merchant_with_pricing_no_apr_passes() -> None:
    """SB 362 is California-only; non-CA states are out of scope."""
    validate_communication(
        merchant_state="NY",
        body="Your offer: factor rate 1.30 over 120 days. No APR mentioned.",
    )
    validate_communication(
        merchant_state="FL",
        body="Your daily payment is $487 over 120 days. No APR mentioned.",
    )


def test_state_lookup_is_case_insensitive() -> None:
    with pytest.raises(CaPricingComplianceError):
        validate_communication(
            merchant_state="ca",
            body="Your offer: factor rate 1.30. Sign attached.",
        )


def test_apr_match_is_word_boundary_not_substring() -> None:
    """'TAPR' or 'APRIL' should not satisfy the APR mention requirement."""
    with pytest.raises(CaPricingComplianceError):
        validate_communication(
            merchant_state="CA",
            body=(
                "Your factor rate is 1.30. The TAPR program is closed; "
                "we'll review again in April."
            ),
        )


def test_error_carries_citation_attribute() -> None:
    """Callers can branch on the citation without parsing the message."""
    assert CaPricingComplianceError.citation == "Cal. Fin. Code § 22806"
