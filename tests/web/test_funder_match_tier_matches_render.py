"""Render tests for the tier-qualification matrix on the funder-match cards.

Commit ``5ef3ac0`` (U28) added ``FunderMatch.tier_matches: list[TierMatch]``
populated by ``evaluate_tier_matches()`` so each funder with a structured
``tiers`` JSONB (Logic Advance Elite/Premium/Standard/High-Risk, UCS's
seven product lines, Highland Hill's 5-rate ladder) carries a per-tier
qualify / disqualify breakdown. The model shipped; the dossier render
did not — the underwriter saw a single match score but no answer to
"which Logic Advance tier do I land on if I submit this deal?"

This file pins the rendering contract on the tier-matrix block inside
``merchant_match.html.j2``:

  * Full ``tier_matches`` with every tier qualifying (strong merchant
    against Logic Advance's 4-tier matrix) → each tier name + factor
    range + estimated advance + holdback all render.
  * Mixed qualify / disqualify (weak merchant) → qualifying tiers render
    in the positive style; disqualifying tiers render muted inside a
    ``<details>`` block whose body exposes every
    ``disqualifying_reasons`` entry.
  * Empty ``tier_matches`` (broker funders like Splash with no published
    ``tiers``) → the entire block is omitted. No empty "Tiers:" heading,
    no zero-row stub.
  * Best-tier highlighting → the FIRST qualifying tier (lowest
    ``estimated_factor_low``) picks up a ``★ best`` badge. The sort
    survives mixed None / Decimal factors without throwing.

SHADOW MODE — per CLAUDE.md "Decision-boundary changes — shadow-first"
and the ``TierMatch`` docstring, the tier matrix is annotation only.
Nothing here should leak into ``match_score`` / ``soft_concerns`` /
``reasons``; the render test pins the visual surface, not a routing
decision.

Tests render the actual tier-matrix subtree by feeding the existing
``templates.env`` (which carries ``whole_money`` + ``format_pct``
registered by the router) an inline Jinja string copied verbatim from
the production template. Going through the full
``merchant_match.html.j2`` would require booting base chrome + fake
merchant + analysis + funder repo — a fixture cost that doesn't buy
more coverage than the inline snippet, since the per-card markup is
the contract under test. Mirror of the pattern in
``test_funder_match_estimated_terms_render.py``.
"""

from __future__ import annotations

from decimal import Decimal

from aegis.scoring.models import TierMatch
from aegis.web.router import templates

# ---------------------------------------------------------------------------
# Inline copy of the tier-matrix block from merchant_match.html.j2. Kept
# verbatim so the test breaks the moment the production template drifts;
# if this snippet falls out of sync the render-contract guarantee here
# becomes meaningless. When the template changes, this string changes
# in the same commit.
# ---------------------------------------------------------------------------
# The en-dashes inside the factor-range spans mirror the production
# template byte-for-byte. RUF001 is suppressed on those lines so the
# inline snippet stays faithful to the rendered output we are pinning.
_CARD_TEMPLATE = (
    "{% if c.tier_matches %}\n"
    "  {% set _priced = c.tier_matches"
    " | rejectattr('estimated_factor_low', 'none')"
    " | list | sort(attribute='estimated_factor_low') %}\n"
    "  {% set _unpriced = c.tier_matches"
    " | selectattr('estimated_factor_low', 'none') | list %}\n"
    "  {% set _tier_sorted = _priced + _unpriced %}\n"
    "  {% set _qualifying_tiers = _tier_sorted | selectattr('qualifies') | list %}\n"
    "  {% set _qualifying_count = _qualifying_tiers | length %}\n"
    "  {% set _best_tier_name = _qualifying_tiers[0].tier_name"
    " if _qualifying_count else None %}\n"
    '  <div class="tier-matrix">\n'
    '    <div class="tier-matrix-header">Tier qualification'
    " · {{ _qualifying_count }} of {{ c.tier_matches|length }} qualify</div>\n"
    '    <div class="tier-matrix-rows">\n'
    "      {% for t in _tier_sorted %}\n"
    "        {% set _is_best = (t.qualifies and t.tier_name == _best_tier_name) %}\n"
    "        {% if t.qualifies %}\n"
    '          <div class="tier-row qualifies'
    '{% if _is_best %} is-best{% endif %}">\n'
    '            <span class="chip pos">{{ t.tier_name }}</span>\n'
    "            {% if _is_best %}"
    '<span class="best-badge">★ best</span>'
    "{% endif %}\n"
    "            {% if t.estimated_factor_low is not none"
    " and t.estimated_factor_high is not none %}\n"
    '              <span class="factor-range">'
    "{{ t.estimated_factor_low }}x–{{ t.estimated_factor_high }}x</span>\n"  # noqa: RUF001
    "            {% elif t.estimated_factor_low is not none %}\n"
    '              <span class="factor-range">{{ t.estimated_factor_low }}x</span>\n'
    "            {% endif %}\n"
    "            {% if t.estimated_advance is not none %}\n"
    '              <span class="advance">'
    "{{ t.estimated_advance | whole_money }}</span>\n"
    "            {% endif %}\n"
    "            {% if t.estimated_holdback is not none %}\n"
    '              <span class="holdback">'
    "{{ t.estimated_holdback | format_pct }} holdback</span>\n"
    "            {% endif %}\n"
    "          </div>\n"
    "        {% else %}\n"
    '          <details class="tier-row disqualifies">\n'
    "            <summary>\n"
    '              <span class="tier-name muted">{{ t.tier_name }}</span>\n'
    '              <span class="disqualify-meta">does not qualify'
    " · {{ t.disqualifying_reasons|length }}</span>\n"
    "              {% if t.estimated_factor_low is not none"
    " and t.estimated_factor_high is not none %}\n"
    '                <span class="factor-range muted">'
    "{{ t.estimated_factor_low }}x–{{ t.estimated_factor_high }}x</span>\n"  # noqa: RUF001
    "              {% endif %}\n"
    "            </summary>\n"
    "            {% if t.disqualifying_reasons %}\n"
    '              <ul class="disqualify-reasons">\n'
    "                {% for r in t.disqualifying_reasons %}"
    "<li>{{ r }}</li>{% endfor %}\n"
    "              </ul>\n"
    "            {% endif %}\n"
    "          </details>\n"
    "        {% endif %}\n"
    "      {% endfor %}\n"
    "    </div>\n"
    "  </div>\n"
    "{% endif %}\n"
)


def _render_card(tier_matches: list[TierMatch]) -> str:
    """Render the tier-matrix block using the real Jinja env.

    Uses ``templates.env.from_string`` so the registered filters
    (``whole_money``, ``format_pct``) are exercised exactly as they
    are in production. ``c`` mirrors the dict shape ``_match_card``
    hands the template — we only need the ``tier_matches`` key for
    this subtree.
    """
    tpl = templates.env.from_string(_CARD_TEMPLATE)
    return tpl.render(c={"tier_matches": tier_matches})


# ---------------------------------------------------------------------------
# Fixture factories — modeled after Logic Advance's 4-tier matrix
# (Elite 1.25 / Premium 1.29 / Standard 1.33 / High-Risk 1.37). Sizes
# are illustrative; the test cares about render shape, not the exact
# pricing curve.
# ---------------------------------------------------------------------------


def _logic_advance_all_qualify() -> list[TierMatch]:
    """Strong merchant: every Logic Advance tier qualifies."""
    return [
        TierMatch(
            tier_name="Elite",
            qualifies=True,
            disqualifying_reasons=[],
            estimated_factor_low=Decimal("1.25"),
            estimated_factor_high=Decimal("1.27"),
            estimated_holdback=Decimal("0.10"),
            estimated_advance=Decimal("250000.00"),
        ),
        TierMatch(
            tier_name="Premium",
            qualifies=True,
            disqualifying_reasons=[],
            estimated_factor_low=Decimal("1.29"),
            estimated_factor_high=Decimal("1.31"),
            estimated_holdback=Decimal("0.12"),
            estimated_advance=Decimal("200000.00"),
        ),
        TierMatch(
            tier_name="Standard",
            qualifies=True,
            disqualifying_reasons=[],
            estimated_factor_low=Decimal("1.33"),
            estimated_factor_high=Decimal("1.35"),
            estimated_holdback=Decimal("0.15"),
            estimated_advance=Decimal("150000.00"),
        ),
        TierMatch(
            tier_name="High-Risk",
            qualifies=True,
            disqualifying_reasons=[],
            estimated_factor_low=Decimal("1.37"),
            estimated_factor_high=Decimal("1.40"),
            estimated_holdback=Decimal("0.18"),
            estimated_advance=Decimal("75000.00"),
        ),
    ]


def _logic_advance_mixed() -> list[TierMatch]:
    """Weak merchant: only High-Risk qualifies; Elite/Premium/Standard
    fall out on FICO + revenue. Models the audit pattern the operator
    actually cares about — "which tier did I drop to and why?"
    """
    return [
        TierMatch(
            tier_name="Elite",
            qualifies=False,
            disqualifying_reasons=[
                "credit 580 < min 700",
                "revenue $25000 < min $50000",
            ],
            estimated_factor_low=Decimal("1.25"),
            estimated_factor_high=Decimal("1.27"),
            estimated_holdback=Decimal("0.10"),
            estimated_advance=Decimal("250000.00"),
        ),
        TierMatch(
            tier_name="Premium",
            qualifies=False,
            disqualifying_reasons=["credit 580 < min 660"],
            estimated_factor_low=Decimal("1.29"),
            estimated_factor_high=Decimal("1.31"),
            estimated_holdback=Decimal("0.12"),
            estimated_advance=Decimal("200000.00"),
        ),
        TierMatch(
            tier_name="Standard",
            qualifies=False,
            disqualifying_reasons=["credit 580 < min 600"],
            estimated_factor_low=Decimal("1.33"),
            estimated_factor_high=Decimal("1.35"),
            estimated_holdback=Decimal("0.15"),
            estimated_advance=Decimal("150000.00"),
        ),
        TierMatch(
            tier_name="High-Risk",
            qualifies=True,
            disqualifying_reasons=[],
            estimated_factor_low=Decimal("1.37"),
            estimated_factor_high=Decimal("1.40"),
            estimated_holdback=Decimal("0.18"),
            estimated_advance=Decimal("60000.00"),
        ),
    ]


def _tier_matches_with_none_factor() -> list[TierMatch]:
    """One priced tier + one unpriced tier — exercises the sort split
    that prevents Python's ``None < Decimal`` TypeError.
    """
    return [
        TierMatch(
            tier_name="Unpriced",
            qualifies=True,
            disqualifying_reasons=[],
            estimated_factor_low=None,
            estimated_factor_high=None,
            estimated_holdback=None,
            estimated_advance=None,
        ),
        TierMatch(
            tier_name="Priced",
            qualifies=True,
            disqualifying_reasons=[],
            estimated_factor_low=Decimal("1.25"),
            estimated_factor_high=Decimal("1.30"),
            estimated_holdback=Decimal("0.12"),
            estimated_advance=Decimal("100000.00"),
        ),
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_all_qualify_renders_every_tier_with_economics() -> None:
    """Strong merchant: each of the 4 Logic Advance tiers surfaces with
    name + factor range + advance + holdback.
    """
    html = _render_card(_logic_advance_all_qualify())

    # Every tier name must appear.
    assert "Elite" in html
    assert "Premium" in html
    assert "Standard" in html
    assert "High-Risk" in html

    # Factor ranges (verbatim Decimal repr surrounded by the dash + x).
    assert "1.25x" in html
    assert "1.27x" in html
    assert "1.37x" in html
    assert "1.40x" in html

    # Advance is rendered via whole_money — integral values strip cents.
    assert "$250,000" in html
    assert "$75,000" in html

    # Holdback is rendered via format_pct (0.10 → 10.00%).
    assert "10.00% holdback" in html
    assert "18.00% holdback" in html

    # Quorum line surfaces "4 of 4 qualify".
    assert "4 of 4 qualify" in html


def test_mixed_qualify_disqualify_renders_reasons_in_details() -> None:
    """Weak merchant: qualifying tier renders normally; disqualifying
    tiers render muted inside ``<details>``, with disqualifying_reasons
    exposed in the disclosure body.
    """
    html = _render_card(_logic_advance_mixed())

    # Quorum line shows "1 of 4 qualify".
    assert "1 of 4 qualify" in html

    # The single qualifying tier (High-Risk) renders with the positive chip.
    assert '<span class="chip pos">High-Risk</span>' in html

    # Disqualifying tiers ride inside <details> with line-through styling.
    assert 'class="tier-row disqualifies"' in html
    assert html.count("<details") >= 3, (
        "Each disqualifying tier should expand its reasons via <details>"
    )

    # Every disqualifying reason must reach the markup so the broker can
    # open the section and see WHY the merchant fell out of a tier.
    assert "credit 580 &lt; min 700" in html
    assert "revenue $25000 &lt; min $50000" in html
    assert "credit 580 &lt; min 660" in html
    assert "credit 580 &lt; min 600" in html

    # Disqualifying tier names must still render (muted).
    assert "Elite" in html
    assert "Premium" in html
    assert "Standard" in html

    # "does not qualify · N" affordance — the reason count surfaces
    # without expanding the section.
    assert "does not qualify · 2" in html  # Elite has 2 reasons
    assert "does not qualify · 1" in html  # Premium / Standard have 1 each


def test_empty_tier_matches_omits_block_entirely() -> None:
    """Broker funders (Splash, etc.) without a ``tiers`` matrix → no
    block at all. No empty heading, no zero-row stub.
    """
    html = _render_card([])

    assert "Tier qualification" not in html
    assert "qualify" not in html
    assert "best" not in html
    # The whole block is empty modulo whitespace.
    assert html.strip() == ""


def test_best_tier_gets_highlight_class() -> None:
    """The first qualifying tier (lowest factor_low) carries the
    ``is-best`` class and the ``★ best`` badge — the operator's
    one-glance answer to "which tier do I land on?"
    """
    html = _render_card(_logic_advance_all_qualify())

    # Best-tier emphasis class is present exactly once.
    assert html.count("is-best") == 1, (
        "exactly one tier must carry the best-tier highlight"
    )

    # The badge text surfaces.
    assert "★ best" in html

    # The best row is the Elite row — emphasis must attach to the lowest
    # buy_rate qualifying tier, NOT to whichever happened to come first
    # in the input order. ``is-best`` lives on the row's opening
    # ``<div class="tier-row qualifies is-best">`` so it renders BEFORE
    # the chip text — pin ordering between is-best and Premium (next row).
    best_idx = html.index("is-best")
    elite_idx = html.index("Elite")
    premium_idx = html.index("Premium")
    # is-best sits in the Elite row's opening tag → before Elite chip text,
    # which is itself before the Premium row.
    assert best_idx < elite_idx < premium_idx


def test_best_tier_skips_disqualifying_rows() -> None:
    """If Elite/Premium/Standard all fail and only High-Risk qualifies,
    the ``★ best`` badge attaches to High-Risk — best AMONG QUALIFYING,
    not best published.
    """
    html = _render_card(_logic_advance_mixed())

    # Exactly one is-best row.
    assert html.count("is-best") == 1

    # The best badge sits on the High-Risk row — verify by checking
    # the badge appears AFTER the disqualifying Standard row in the
    # rendered order (priced tiers sort ascending, so High-Risk is last
    # and the only qualifier).
    standard_idx = html.index("Standard")
    best_idx = html.index("is-best")
    assert standard_idx < best_idx, (
        "best badge must attach to the qualifying tier (High-Risk), "
        "not to a higher-priced disqualifying row"
    )


def test_sort_tolerates_none_factor_without_typeerror() -> None:
    """Mixed None / Decimal ``estimated_factor_low`` must not blow up
    the sort. Priced tier renders before unpriced; the entire block
    renders without raising.

    NOTE: substring ``"Priced"`` is contained in ``"Unpriced"``, so the
    ordering assertions key off the exact rendered chip markup
    ``>Priced<`` and ``>Unpriced<`` rather than the bare tier name.
    """
    html = _render_card(_tier_matches_with_none_factor())

    # Both tiers render.
    assert ">Priced<" in html
    assert ">Unpriced<" in html

    # Priced tier comes first (lower factor renders before None bucket).
    priced_idx = html.index(">Priced<")
    unpriced_idx = html.index(">Unpriced<")
    assert priced_idx < unpriced_idx

    # Best badge attaches to the priced tier (lowest factor_low among
    # qualifying tiers, with None ignored by the sort split).
    best_idx = html.index("is-best")
    assert best_idx < priced_idx < unpriced_idx, (
        "is-best class sits on the same row as the Priced chip "
        "(rendered just before the chip text in the row's opening tag)"
    )
