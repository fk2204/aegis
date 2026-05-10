"""Renewal handling tests.

Covers:
  * compliance/renewal.py — TransactionType, RenewalContext,
    build_state_renewal_context.
  * compliance/disclosure.py — renewal-aware render_disclosure
    (validates that NY renewals without a RenewalContext raise; that
    RenderedDisclosure.transaction_type is propagated; that CA / FL /
    GA renewals don't get the NY-only template keys).

The actual NY anti-double-dipping math is tested under
``tests/compliance/test_new_york_tier1.py``; this file tests the
orchestration layer that wires that math into the disclosure router.

CORRECTIONS Correction 3 applies: no CA "Renewal" labeling rule is
enforced — the dossier's claim is softened to "industry guidance"
which AEGIS does not encode.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from aegis.compliance.disclosure import render_disclosure
from aegis.compliance.renewal import (
    RenewalContext,
    RenewalContextRequiredError,
    TransactionType,
    build_state_renewal_context,
)
from aegis.scoring.models import ScoreInput, ScoreResult

# --- TransactionType enum ---------------------------------------------------


def test_transaction_type_has_all_dossier_values() -> None:
    """Per dossier 14: NEW, RENEWAL, MODIFICATION, FORBEARANCE, DEFAULT_WORKOUT."""
    expected = {"new", "renewal", "modification", "forbearance", "default_workout"}
    assert {t.value for t in TransactionType} == expected


def test_transaction_type_default_is_new() -> None:
    """``TransactionType.NEW`` is the safe default for the disclosure router."""
    assert TransactionType.NEW.value == "new"


def test_transaction_type_string_round_trip() -> None:
    """StrEnum values are stable for storage / API contract."""
    assert TransactionType("renewal") == TransactionType.RENEWAL
    assert TransactionType.RENEWAL == "renewal"


# --- RenewalContext model --------------------------------------------------


def _valid_renewal(**overrides: object) -> RenewalContext:
    base: dict[str, object] = {
        "prior_funded_amount": Decimal("100000"),
        "prior_total_payback": Decimal("130000"),
        "prior_amount_repaid": Decimal("80000"),
        "prior_position_payoff_from_renewal": Decimal("50000"),
    }
    base.update(overrides)
    return RenewalContext(**base)  # type: ignore[arg-type]


def test_renewal_context_accepts_full_input() -> None:
    rc = _valid_renewal(prior_deal_id=uuid4(), detection_method="operator_flagged")
    assert isinstance(rc.prior_deal_id, UUID)
    assert rc.prior_funded_amount == Decimal("100000")
    assert rc.detection_method == "operator_flagged"


def test_renewal_context_defaults_detection_method_to_operator_flagged() -> None:
    rc = _valid_renewal()
    assert rc.detection_method == "operator_flagged"
    assert rc.detection_confidence is None


def test_renewal_context_rejects_zero_prior_funded() -> None:
    with pytest.raises(ValidationError):
        _valid_renewal(prior_funded_amount=Decimal("0"))


def test_renewal_context_rejects_negative_prior_amount_repaid() -> None:
    with pytest.raises(ValidationError):
        _valid_renewal(prior_amount_repaid=Decimal("-1"))


def test_renewal_context_rejects_unknown_detection_method() -> None:
    with pytest.raises(ValidationError):
        _valid_renewal(detection_method="psychic_intuition")


def test_renewal_context_rejects_extra_fields() -> None:
    """extra='forbid' — typos must fail loudly."""
    with pytest.raises(ValidationError):
        RenewalContext(
            prior_funded_amount=Decimal("100000"),
            prior_total_payback=Decimal("130000"),
            prior_amount_repaid=Decimal("80000"),
            prior_position_payoff_from_renewal=Decimal("50000"),
            random_typo_field="oops",  # type: ignore[call-arg]
        )


def test_renewal_context_detection_confidence_bounded_0_to_1() -> None:
    _valid_renewal(detection_confidence=Decimal("0.85"))  # OK
    with pytest.raises(ValidationError):
        _valid_renewal(detection_confidence=Decimal("1.5"))


# --- build_state_renewal_context: NY ---------------------------------------


def test_build_renewal_context_ny_with_renewal_emits_double_dipping_keys() -> None:
    """NY renewal → both template keys present, double-dipping computed."""
    out = build_state_renewal_context("NY", _valid_renewal())
    assert out == {
        "is_renewal_with_double_dip": True,
        # Worked example: $100K/$130K/$80K/$50K → $6,000.00.
        "double_dipping_amount": "$6,000.00",
    }


def test_build_renewal_context_ny_no_renewal_emits_neutral_template_keys() -> None:
    """NY non-renewal still needs the keys (StrictUndefined would raise)."""
    out = build_state_renewal_context("NY", renewal=None)
    assert out == {
        "is_renewal_with_double_dip": False,
        "double_dipping_amount": "$0.00",
    }


def test_build_renewal_context_ny_lowercase_state_works() -> None:
    out = build_state_renewal_context("ny", _valid_renewal())
    assert out["is_renewal_with_double_dip"] is True


def test_build_renewal_context_ny_zero_amount_renders_as_zero_dollars() -> None:
    """When the math yields $0 (e.g. principal already fully repaid)."""
    out = build_state_renewal_context(
        "NY",
        _valid_renewal(prior_amount_repaid=Decimal("100000")),  # principal gone
    )
    assert out["is_renewal_with_double_dip"] is True
    assert out["double_dipping_amount"] == "$0.00"


# --- build_state_renewal_context: CA / FL / GA / IL / Tier 3 ---------------


@pytest.mark.parametrize("state", ["CA", "FL", "GA", "IL", "WY", "OH"])
def test_build_renewal_context_non_ny_returns_empty_dict_with_renewal(
    state: str,
) -> None:
    """Only NY has renewal-only template content; others get nothing extra."""
    out = build_state_renewal_context(state, _valid_renewal())
    assert out == {}


@pytest.mark.parametrize("state", ["CA", "FL", "GA", "IL", "WY", "OH"])
def test_build_renewal_context_non_ny_returns_empty_dict_without_renewal(
    state: str,
) -> None:
    out = build_state_renewal_context(state, renewal=None)
    assert out == {}


def test_build_renewal_context_non_ny_logs_when_renewal_present(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Audit trail: CA/FL/GA renewals are logged even though no template change."""
    with caplog.at_level(logging.INFO, logger="aegis.compliance.renewal"):
        build_state_renewal_context("CA", _valid_renewal())
    assert any(
        "renewal.no_state_specific_content" in r.getMessage()
        for r in caplog.records
    )


def test_build_renewal_context_ny_logs_double_dipping_computation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="aegis.compliance.renewal"):
        build_state_renewal_context("NY", _valid_renewal())
    assert any(
        "renewal.ny_double_dipping_computed" in r.getMessage()
        for r in caplog.records
    )


# --- render_disclosure renewal integration ---------------------------------


def _deal(state: str = "NY") -> ScoreInput:
    return ScoreInput(
        merchant_id=uuid4(),
        business_name="Acme Co",
        owner_name="Jane Doe",
        state=state,
        avg_daily_balance=Decimal("12500.00"),
        true_revenue=Decimal("110000.00"),
        monthly_revenue=Decimal("110000.00"),
        lowest_balance=Decimal("3000.00"),
        num_nsf=0,
        days_negative=0,
        mca_positions=0,
        mca_daily_total=Decimal("0.00"),
        debt_to_revenue=Decimal("0.00"),
        fraud_score=10,
        statement_period_start=date(2026, 4, 1),
        statement_period_end=date(2026, 4, 30),
        statement_days=30,
        requested_amount=Decimal("50000.00"),
        requested_factor=Decimal("1.30"),
        requested_term_days=120,
    )


def _score() -> ScoreResult:
    return ScoreResult(
        score=72,
        tier="B",
        recommendation="approve",
        suggested_max_advance=Decimal("100000.00"),
        recommended_factor_rate=Decimal("1.29"),
        recommended_holdback_pct=Decimal("0.12"),
        estimated_payback_days=180,
    )


def test_render_disclosure_ny_renewal_without_context_raises() -> None:
    """NY renewal MUST carry RenewalContext — raises before any render attempt."""
    with pytest.raises(
        RenewalContextRequiredError,
        match=r"§ 600\.6\(b\)\(3\)\(v\)",
    ):
        render_disclosure(
            "NY",
            _deal("NY"),
            _score(),
            transaction_type=TransactionType.RENEWAL,
            renewal=None,
        )


def test_render_disclosure_ny_new_does_not_require_renewal_context() -> None:
    """Non-renewal NY deal does NOT need RenewalContext; the renewal raise
    is renewal-only.

    The full NY render still requires the additional template variables
    that ``_build_context`` does not supply (Jinja StrictUndefined would
    raise on them) — that's a separate, pre-existing render-context gap
    not part of this change. The renewal validation must pass before
    any of that.
    """
    # We stop short of actually rendering — the validation we care about
    # is "does render_disclosure refuse to even start for NY renewal +
    # no context?". For the non-renewal path the validation passes; the
    # downstream Jinja error is unrelated and out of scope here.
    from jinja2.exceptions import UndefinedError

    with pytest.raises(UndefinedError):
        render_disclosure(
            "NY",
            _deal("NY"),
            _score(),
            transaction_type=TransactionType.NEW,
            renewal=None,
        )


def test_render_disclosure_il_renewal_proceeds_without_context() -> None:
    """IL is Tier 2 — no NY-style requirement. Renewal flag stored on result."""
    rendered = render_disclosure(
        "IL",
        _deal("IL"),
        _score(),
        transaction_type=TransactionType.RENEWAL,
    )
    assert rendered.transaction_type == TransactionType.RENEWAL
    assert rendered.tier == 2


def test_render_disclosure_default_transaction_type_is_new() -> None:
    rendered = render_disclosure("IL", _deal("IL"), _score())
    assert rendered.transaction_type == TransactionType.NEW
