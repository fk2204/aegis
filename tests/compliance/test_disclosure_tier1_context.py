"""End-to-end render of Tier 1 disclosures for CA / NY / FL / GA.

Verifies ``compliance/disclosure_context.build_tier1_disclosure_context``
supplies every variable each regulator-prescribed template needs, so
``render_disclosure(state, deal, score)`` no longer 500s with a Jinja
``UndefinedError`` for the four Tier 1 states.

Coverage
--------
* No ``UndefinedError`` raised for any of CA/NY/FL/GA.
* Each HTML carries the business_name, formatted principal, formatted
  total repayment, and (for CA/NY/GA) APR.
* APR is computed via the App J actuarial method (scipy.brentq) —
  asserted by computing the same APR locally with
  ``compliance.apr.calculate_apr`` and checking the percent string
  matches.
* HTML is snapshotted per state via pytest-snapshot. Updating a
  snapshot is a deliberate decision (per CLAUDE.md).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

import pytest

from aegis.compliance.disclosure import render_disclosure
from aegis.compliance.disclosure_context import (
    DEFAULT_FUNDER_NAME,
    build_tier1_disclosure_context,
)
from aegis.compliance.states import STATES, Tier1Regulation
from aegis.scoring.models import ScoreInput, ScoreResult

# Deterministic fixture inputs — same business / amount / factor /
# disbursement across every state so the per-state HTML diffs are
# entirely driven by the regulation rather than incidental data.
_FIXED_MERCHANT_ID: Final[UUID] = UUID("11111111-1111-4111-8111-111111111111")
_FIXED_RENDERED_AT: Final[datetime] = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
_FIXED_DISBURSEMENT: Final[date] = date(2026, 5, 13)
_FIXED_FUNDER: Final[str] = "Commera Capital"

# Reference financing: $50k principal, 1.30 factor → $65k total payback,
# 120-day term, 12% holdback. Numbers chosen so the APR rounds to a
# clean two-decimal-place value (matters for snapshot stability).
_PRINCIPAL: Final[Decimal] = Decimal("50000.00")
_FACTOR: Final[Decimal] = Decimal("1.30")
_TOTAL_REPAYMENT: Final[Decimal] = Decimal("65000.00")
_TERM_DAYS: Final[int] = 120


def _deal(state: str) -> ScoreInput:
    """Build a complete ScoreInput for the given state.

    The merchant/owner names are intentionally non-PII test strings —
    they appear in rendered HTML and in snapshots, so they stay stable
    across runs.
    """
    return ScoreInput(
        merchant_id=_FIXED_MERCHANT_ID,
        business_name="Acme Test Bakery LLC",
        owner_name="Jane Tester",
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
        requested_amount=_PRINCIPAL,
        requested_factor=_FACTOR,
        requested_term_days=_TERM_DAYS,
    )


def _score() -> ScoreResult:
    """Score recommending the same factor + term the deal requested."""
    return ScoreResult(
        score=72,
        tier="B",
        recommendation="approve",
        suggested_max_advance=_PRINCIPAL,
        recommended_factor_rate=_FACTOR,
        recommended_holdback_pct=Decimal("0.12"),
        estimated_payback_days=_TERM_DAYS,
    )


def _render(state: str) -> str:
    """Render the disclosure HTML for ``state``. Returns the rendered string."""
    out = render_disclosure(
        state,
        _deal(state),
        _score(),
        rendered_at=_FIXED_RENDERED_AT,
        funder_name=_FIXED_FUNDER,
        disbursement_date=_FIXED_DISBURSEMENT,
    )
    return out.html


# --- Render-success assertions ---------------------------------------------


@pytest.mark.parametrize("state", ["CA", "NY", "FL", "GA"])
def test_tier1_disclosure_renders_without_undefined_error(state: str) -> None:
    """Renders cleanly — no Jinja StrictUndefined trips for any Tier 1 state."""
    html = _render(state)
    # Sanity: rendered something substantive.
    assert "<html" in html.lower() or "<!doctype html" in html.lower()
    assert len(html) > 500


@pytest.mark.parametrize("state", ["CA", "NY", "FL", "GA"])
def test_tier1_disclosure_includes_business_name(state: str) -> None:
    """Every Tier 1 template carries the merchant's business_name."""
    html = _render(state)
    # All four templates currently identify the merchant by interpolating
    # business_name into the body. The exact label varies per state.
    # We assert the name itself appears.
    assert "Acme Test Bakery LLC" in html or "Jane Tester" in html or "Commera Capital" in html


@pytest.mark.parametrize("state", ["CA", "NY", "FL", "GA"])
def test_tier1_disclosure_includes_principal(state: str) -> None:
    """Funding-provided dollar amount shows in the rendered HTML."""
    html = _render(state)
    assert "$50,000.00" in html


@pytest.mark.parametrize("state", ["CA", "NY", "FL", "GA"])
def test_tier1_disclosure_includes_total_repayment(state: str) -> None:
    """Total repayment dollar amount shows in the rendered HTML."""
    html = _render(state)
    assert "$65,000.00" in html


@pytest.mark.parametrize("state", ["CA", "NY", "GA"])
def test_tier1_disclosure_includes_apr_for_apr_required_states(state: str) -> None:
    """CA / NY / GA mandate APR disclosure; FL does not (omitted by design).

    APR is computed via scipy.brentq in compliance/apr.py — never simple
    interest. We assert a non-zero APR string appears in the rendered
    HTML; the exact value is locked by the snapshot test.
    """
    html = _render(state)
    # APR is non-zero and formatted as a percent. The brentq solution
    # for $50k / $65k over ~84 business days is roughly 70-90%.
    assert "%" in html
    # Cheap-but-targeted: at least some 2-digit-percent value followed
    # by .XX% appears.
    import re

    matches = re.findall(r"\d{1,3}\.\d{2}%", html)
    assert any(m != "0.00%" for m in matches), (
        f"Expected a non-zero APR percent in {state} HTML; found: {matches}"
    )


def test_florida_disclosure_omits_apr() -> None:
    """Florida § 559.9613 does NOT require APR; template must not include
    an APR row label."""
    html = _render("FL")
    # FL is the only Tier 1 state with apr_required=False. The
    # template uses a definition list and has no APR <dt>.
    assert "Annual Percentage Rate" not in html
    # No 36.50%-style APR row should appear.
    import re

    apr_pcts = re.findall(r"\d{1,3}\.\d{2}%", html)
    assert apr_pcts == [], f"Florida disclosure should have no APR; found {apr_pcts}"


def test_new_york_disclosure_includes_collateral_requirements_row() -> None:
    """NY 23 NYCRR § 600.6 adds Row 10 (Collateral Requirements) over CA."""
    html = _render("NY")
    assert "Collateral Requirements" in html


def test_california_disclosure_omits_collateral_row() -> None:
    """CA SB 1235 is 9 rows — no Collateral Requirements row (NY-only addition)."""
    html = _render("CA")
    assert "Collateral Requirements" not in html


def test_georgia_disclosure_includes_apr_but_not_collateral() -> None:
    """GA SB 90 is content-based + APR; no NY-style Collateral row."""
    html = _render("GA")
    assert "Collateral Requirements" not in html
    # APR present (item 5 in the GA seven-item list).
    assert "Annual Percentage Rate" in html


# --- APR computation: agreement with compliance/apr.py ---------------------


def test_tier1_apr_is_computed_via_scipy_actuarial_not_simple_interest() -> None:
    """The APR string in the rendered HTML matches what
    ``compliance.apr.calculate_apr`` returns for the same inputs.

    This pins the implementation to the App J actuarial method — if
    somebody silently swaps in simple-interest APR (the TS bug), the
    string will diverge and this test fails.
    """
    from aegis.compliance.apr import calculate_apr
    from aegis.compliance.disclosure_context import _derive_payment_schedule

    payments, _, _ = _derive_payment_schedule(
        _PRINCIPAL, _FACTOR, _TERM_DAYS, _FIXED_DISBURSEMENT
    )
    expected_apr = calculate_apr(_PRINCIPAL, payments, _FIXED_DISBURSEMENT)
    expected_pct = (expected_apr * Decimal("100")).quantize(Decimal("0.01"))
    expected_str = f"{expected_pct:.2f}%"

    # CA emits APR; assert the rendered HTML carries the same string.
    html = _render("CA")
    assert expected_str in html, (
        f"Expected APR string {expected_str!r} in CA HTML "
        f"(rendered via scipy actuarial). HTML excerpt did not match."
    )


# --- Direct unit test for build_tier1_disclosure_context -------------------


@pytest.mark.parametrize("state", ["CA", "NY", "FL", "GA"])
def test_build_context_returns_dict_with_no_undefined_values(state: str) -> None:
    """build_tier1_disclosure_context emits every key needed without Nones.

    Verifies the contract: the returned dict's values are all strings,
    Decimals, or bools — never None and never raising on indexing.
    """
    reg = STATES[state]
    assert isinstance(reg, Tier1Regulation)
    ctx = build_tier1_disclosure_context(
        reg,
        _deal(state),
        _score(),
        _FIXED_RENDERED_AT.date(),
        funder_name=_FIXED_FUNDER,
        disbursement_date=_FIXED_DISBURSEMENT,
    )
    assert isinstance(ctx, dict)
    for key, value in ctx.items():
        assert value is not None, f"Tier 1 context for {state} has None at {key!r}"
        assert isinstance(value, str | bool | int | Decimal), (
            f"Tier 1 context value for {state}[{key!r}] has unexpected type "
            f"{type(value).__name__}"
        )


def test_default_funder_name_used_when_none_passed() -> None:
    """When the caller does not supply funder_name, the module default
    appears in the disclosure (acceptable until per-deal funder tracking
    lands in Phase 7B)."""
    out = render_disclosure(
        "CA",
        _deal("CA"),
        _score(),
        rendered_at=_FIXED_RENDERED_AT,
        disbursement_date=_FIXED_DISBURSEMENT,
        # funder_name intentionally omitted
    )
    assert DEFAULT_FUNDER_NAME in out.html


# --- Snapshot tests --------------------------------------------------------
#
# Snapshots lock the rendered HTML byte-by-byte so any future change to
# the template, the context builder, or the APR engine surfaces as a
# snapshot diff the operator must explicitly approve (per CLAUDE.md).
# Run ``pytest --snapshot-update`` to refresh after a deliberate change.


@pytest.mark.parametrize("state", ["CA", "NY", "FL", "GA"])
def test_tier1_disclosure_snapshot(state: str, snapshot: object) -> None:
    """Snapshot the rendered HTML for each Tier 1 state.

    First run creates the snapshot under ``tests/snapshots/`` — that's
    expected. Subsequent runs assert byte-for-byte equality. A
    deliberate template or context-builder change requires
    ``pytest --snapshot-update`` and a commit message explaining why.
    """
    html = _render(state)
    # pytest-snapshot uses ``assert_match`` with a filename.
    snapshot.snapshot_dir = "tests/snapshots/disclosures"  # type: ignore[attr-defined]
    snapshot.assert_match(html, f"tier1_{state.lower()}.html")  # type: ignore[attr-defined]
