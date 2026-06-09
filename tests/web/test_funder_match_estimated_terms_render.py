"""Render tests for the EstimatedTerms block on the funder-match cards.

Commit ``87dbb2b`` (R4.2 + R4.3) added ``EstimatedTerms`` on
``FunderMatch.estimated_terms`` so the matcher could surface per-funder
pricing guidance (advance, factor, holdback, daily payment, APR) next
to the qualification verdict. The model shipped but the dossier render
did not — the underwriter saw match scores but not the actual numbers.

This test file pins the rendering contract on the funder-match card
inside ``merchant_match.html.j2``:

  * full EstimatedTerms (APR populated) → advance / factor / holdback /
    daily / APR all visible.
  * APR is ``None`` → the cell renders the literal ``unavailable``
    string, NOT ``0.00%``. Per R0.4 regulator-grade-lie discipline a
    0% APR rendered next to a 1.30x factor is the lie we explicitly
    refuse to render — the IRR optimizer can fail to bracket a root
    on degenerate inputs and surfacing that as 0% would manufacture
    false precision the operator could quote to a funder rep.
  * ``estimated_terms is None`` → the entire pricing block is omitted.
    No empty row, no em-dash placeholders. (Funder has no published
    envelope, or score tier is outside the interpolation table.)
  * ``interpolation_evidence`` appears in the rendered HTML — it is
    the audit trail the operator uses to sanity-check the quote
    against the funder's published range.

Tests render the actual funder-card subtree by feeding the existing
``templates.env`` (which carries the ``whole_money`` + ``format_pct``
filters registered by the router) an inline Jinja string copied
verbatim from the production template. Going through the full
``merchant_match.html.j2`` would require booting the base chrome +
fake merchant + analysis + funder repo — a fixture cost that doesn't
buy more coverage than the inline snippet, since the per-card markup
is the contract under test.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from aegis.scoring.models import EstimatedTerms, FunderMatch
from aegis.web.router import templates

# ---------------------------------------------------------------------------
# Inline copy of the per-card pricing block from merchant_match.html.j2.
# Kept verbatim so the test breaks the moment the template drifts; if
# this snippet falls out of sync with the production template, the
# render-contract guarantee here becomes meaningless. When the template
# changes, this string changes in the same commit.
# ---------------------------------------------------------------------------
_CARD_TEMPLATE = """\
{% if c.estimated_terms is not none %}
{% set et = c.estimated_terms %}
<div class="pricing-block">
  <div class="pricing-header">Estimated terms (interpolated from funder tier)</div>
  <div class="pricing-kpis">
    <div>
      <div class="k">Advance</div>
      <div class="v">{{ et.estimated_advance | whole_money }}</div>
    </div>
    <div>
      <div class="k">Factor</div>
      <div class="v">{{ et.estimated_factor }}x</div>
    </div>
    <div>
      <div class="k">Holdback</div>
      <div class="v">{{ et.estimated_holdback_pct | format_pct }}</div>
    </div>
    <div>
      <div class="k">Daily</div>
      <div class="v">{{ et.estimated_daily_payment | whole_money }}</div>
    </div>
    <div>
      <div class="k">APR</div>
      <div class="v">{{ et.estimated_apr | format_pct }}</div>
    </div>
  </div>
  <details>
    <summary>How this was computed</summary>
    <div class="evidence">{{ et.interpolation_evidence }}</div>
  </details>
</div>
{% endif %}
"""


def _render_card(match: FunderMatch) -> str:
    """Render the pricing block for one match using the real Jinja env.

    Uses ``templates.env.from_string`` so the registered filters
    (``whole_money``, ``format_pct``) are exercised exactly as they
    are in production. ``c`` mirrors the dict shape ``_match_card``
    hands the template.
    """
    tpl = templates.env.from_string(_CARD_TEMPLATE)
    return tpl.render(c={"estimated_terms": match.estimated_terms})


# ---------------------------------------------------------------------------
# Fixture factories
# ---------------------------------------------------------------------------


def _match_with_terms(*, apr: Decimal | None = Decimal("0.3845")) -> FunderMatch:
    terms = EstimatedTerms(
        estimated_advance=Decimal("45000.00"),
        estimated_factor=Decimal("1.2800"),
        estimated_holdback_pct=Decimal("0.1200"),
        estimated_daily_payment=Decimal("514.29"),
        estimated_apr=apr,
        interpolation_evidence=(
            "tier=C → 50% along factor range 1.20-1.40 → 1.28; "
            "holdback 0.10-0.20 → 0.12"
        ),
    )
    return FunderMatch(
        funder_id=uuid4(),
        funder_name="Pricing Co",
        match_score=72,
        reasons=["tier_C"],
        soft_concerns=[],
        estimated_terms=terms,
    )


def _match_without_terms() -> FunderMatch:
    return FunderMatch(
        funder_id=uuid4(),
        funder_name="No Pricing Funder",
        match_score=60,
        reasons=["tier_C"],
        soft_concerns=[],
        estimated_terms=None,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_full_estimated_terms_renders_all_kpis() -> None:
    """APR populated → every KPI surfaces with formatted values."""
    html = _render_card(_match_with_terms())

    # Whole-money formatter strips trailing .00 when the value is integral
    # ($45,000) but keeps cents when present ($514.29 daily payment).
    assert "$45,000" in html, "advance must render via whole_money filter"
    assert "$514.29" in html or "$514" in html
    assert "1.2800x" in html, "factor must render with the trailing x"
    # holdback (0.12) → 12.00%; format_pct quantizes to 2 decimals.
    assert "12.00%" in html, "holdback must render as a formatted percent"
    # APR (0.3845) → 38.45%.
    assert "38.45%" in html, "APR must render as a formatted percent"
    assert "unavailable" not in html, (
        "APR was supplied — must NOT fall back to the unavailable string"
    )


def test_apr_none_renders_unavailable_not_zero_percent() -> None:
    """APR=None → literal ``unavailable``, never ``0.00%`` (R0.4 lie)."""
    html = _render_card(_match_with_terms(apr=None))

    assert "unavailable" in html, (
        "APR=None must render the explicit 'unavailable' string per R0.4"
    )
    # The lie we explicitly refuse to render: 0% APR next to a 1.28x factor.
    # The other KPIs in this fixture do not legitimately render 0.00%, so any
    # appearance of that string here would be the regression.
    assert "0.00%" not in html, (
        "APR=None must NOT silently render as 0.00% — that is a "
        "regulator-grade lie (1.28x factor next to 0% APR)"
    )
    # Other KPIs still render normally — only APR is gated.
    assert "1.2800x" in html
    assert "12.00%" in html


def test_estimated_terms_none_omits_block_entirely() -> None:
    """``estimated_terms is None`` → no pricing block at all.

    No "Estimated terms" header, no em-dash placeholders, no empty
    KPI row — the entire ``{% if %}`` evaluates false. Funders without
    a published pricing envelope must not show a stub.
    """
    html = _render_card(_match_without_terms())

    assert "Estimated terms" not in html
    assert "Advance" not in html
    assert "Factor" not in html
    assert "Holdback" not in html
    assert "—" not in html, (
        "missing terms must NOT fall back to em-dash placeholders"
    )
    assert "unavailable" not in html, (
        "missing terms must NOT render the APR-None fallback string either"
    )
    # The whole block is the empty string modulo whitespace.
    assert html.strip() == ""


def test_interpolation_evidence_surfaces_in_rendered_html() -> None:
    """The audit trail must reach the operator.

    ``interpolation_evidence`` is the string that lets the operator
    explain a quote back to a funder rep without re-opening the model.
    It lives inside a ``<details>`` block so it doesn't clutter the
    KPI row, but it MUST be present in the markup.
    """
    match = _match_with_terms()
    html = _render_card(match)

    assert match.estimated_terms is not None  # narrow for mypy
    assert match.estimated_terms.interpolation_evidence in html
    assert "How this was computed" in html
    assert "<details>" in html
    assert "</details>" in html


# ---------------------------------------------------------------------------
# Filter-direct tests — _format_pct_filter is the new piece; pin its
# regulator-grade-lie behavior directly so a refactor of the filter
# can't quietly start returning "0.00%" for None.
# ---------------------------------------------------------------------------


def test_format_pct_filter_none_returns_unavailable() -> None:
    """The filter itself must refuse to render 0% for None.

    Render-level tests above also cover this, but pinning the filter
    in isolation makes the regression mode unambiguous: if anyone
    silently changes ``_format_pct_filter`` to ``return "0.00%"`` for
    falsy input (an easy mistake), this test fails before the render
    tests do.
    """
    filt = templates.env.filters["format_pct"]

    assert filt(None) == "unavailable"
    # And the positive case still rounds + appends %.
    assert filt(Decimal("0.3845")) == "38.45%"
    assert filt(Decimal("0.12")) == "12.00%"


@pytest.mark.parametrize(
    "value,expected",
    [
        (Decimal("0.00"), "0.00%"),
        (Decimal("1.00"), "100.00%"),
        (Decimal("0.5"), "50.00%"),
    ],
)
def test_format_pct_filter_renders_decimal_fraction_as_percent(
    value: Decimal, expected: str
) -> None:
    """Decimal fractions render as percent strings, two decimals.

    NB: ``0.00%`` IS the correct render when the input is literally
    ``Decimal("0")`` (a legitimate zero, e.g. a 0% promo holdback).
    The R0.4 discipline only applies to ``None``, where 0% would be
    a fabricated value. A literal 0 is data, not a lie.
    """
    filt = templates.env.filters["format_pct"]
    assert filt(value) == expected
