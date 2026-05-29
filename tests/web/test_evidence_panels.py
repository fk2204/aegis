"""Template-rendering tests for the per-flag evidence drill-down panels.

Each test loads the dispatch partial (``_evidence_panel.html.j2``) directly
with a Jinja Environment scoped to ``src/aegis/web/templates`` and asserts
the key piece of each flag's natural shape:

- wash_deposit_suspected: pair count + ↓/↑ pair markers
- preloan_spike: spike rows AND a baseline subheader (when full
  transaction context is supplied)
- acceleration_clause_triggered: normal cadence subheader + spike row
- recent_account_opening: explanation paragraph with the period date
- payroll_absent: explanation paragraph with the period + revenue figure
- fallback (any other code with source_transactions): flat table
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from aegis.parser.models import ClassifiedTransaction
from aegis.web._pattern_cards import PATTERN_COPY, PatternCard

_TEMPLATES_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "aegis" / "web" / "templates"
)


def _money_filter(value: object) -> str:
    """Minimal money filter mirroring the one registered on the real env."""
    if value is None:
        return ""
    try:
        amt = Decimal(str(value))
    except Exception:
        return str(value)
    sign = "-" if amt < 0 else ""
    return f"{sign}${abs(amt):,.2f}"


@pytest.fixture
def env() -> Environment:
    e = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(enabled_extensions=("html", "j2", "html.j2")),
        undefined=StrictUndefined,
    )
    e.filters["money"] = _money_filter
    return e


def _txn(
    *,
    amount: str,
    posted_date: date,
    description: str = "ACME CORP",
    category: str = "deposit",
    source_page: int = 1,
    source_line: int = 1,
) -> ClassifiedTransaction:
    return ClassifiedTransaction(
        posted_date=posted_date,
        description=description,
        amount=Decimal(amount),
        source_page=source_page,
        source_line=source_line,
        category=category,
        classification_confidence=90,
    )


def _make_card(
    code: str,
    source_transactions: list[ClassifiedTransaction] | None = None,
    *,
    detail: str = "test detail",
    severity: int = 25,
) -> PatternCard:
    copy = PATTERN_COPY.get(code)
    title = copy.title if copy else code
    description = copy.description if copy else ""
    return PatternCard(
        code=code,
        title=title,
        description=description,
        detail=detail,
        severity=severity,
        severity_band="warn",
        source_transactions=source_transactions or [],
    )


# -- wash_deposit_suspected --------------------------------------------------


def test_wash_deposit_panel_renders_pair_count_and_arrows(env: Environment) -> None:
    """The wash-deposit panel must surface pair count and ↓/↑ markers
    so the round-trip pattern is obvious at a glance."""
    deposit_1 = _txn(amount="12400.00", posted_date=date(2026, 5, 3))
    withdrawal_1 = _txn(amount="-12350.00", posted_date=date(2026, 5, 7))
    deposit_2 = _txn(amount="8900.00", posted_date=date(2026, 5, 11))
    withdrawal_2 = _txn(amount="-8890.00", posted_date=date(2026, 5, 13))
    card = _make_card(
        "wash_deposit_suspected",
        [deposit_1, withdrawal_1, deposit_2, withdrawal_2],
    )

    rendered = env.get_template("_evidence_panel.html.j2").render(card=card)

    assert "2 pairs" in rendered
    assert "Pair 1" in rendered
    assert "Pair 2" in rendered
    assert "↓" in rendered
    assert "↑" in rendered


# -- preloan_spike -----------------------------------------------------------


def test_preloan_spike_panel_renders_baseline_section_when_context_present(
    env: Environment,
) -> None:
    """When ``latest_transactions`` is in scope, the preloan_spike panel
    surfaces a pre-spike baseline subheader so the contrast in scale
    is visible."""
    spike_a = _txn(amount="124000.00", posted_date=date(2026, 5, 21))
    spike_b = _txn(amount="98500.00", posted_date=date(2026, 5, 22))
    baseline_a = _txn(
        amount="4200.00", posted_date=date(2026, 4, 27), description="OLD ACH"
    )
    baseline_b = _txn(
        amount="3800.00", posted_date=date(2026, 4, 30), description="OLD DEPOSIT"
    )
    card = _make_card("preloan_spike", [spike_a, spike_b])
    all_transactions = [spike_a, spike_b, baseline_a, baseline_b]

    rendered = env.get_template("_evidence_panel.html.j2").render(
        card=card, latest_transactions=all_transactions
    )

    assert "Spike transactions" in rendered
    assert "Baseline" in rendered
    assert "OLD ACH" in rendered or "OLD DEPOSIT" in rendered


def test_preloan_spike_panel_collapses_baseline_without_context(
    env: Environment,
) -> None:
    """Without ``latest_transactions`` the panel must still render the
    spike rows but note that the baseline panel is hidden — never crash."""
    spike = _txn(amount="124000.00", posted_date=date(2026, 5, 21))
    card = _make_card("preloan_spike", [spike])

    rendered = env.get_template("_evidence_panel.html.j2").render(
        card=card, latest_transactions=[]
    )

    assert "Spike transactions" in rendered
    assert "Baseline" not in rendered or "hidden" in rendered.lower()


# -- acceleration_clause_triggered ------------------------------------------


def test_acceleration_panel_separates_normal_cadence_from_spike(
    env: Environment,
) -> None:
    """Acceleration evidence shows the normal recurring debits first,
    then the trailing 5-10x debit labeled as the spike."""
    normal_1 = _txn(
        amount="-612.00",
        posted_date=date(2026, 5, 1),
        description="ONDECK ACH PMT",
        category="mca_debit",
    )
    normal_2 = _txn(
        amount="-612.00",
        posted_date=date(2026, 5, 4),
        description="ONDECK ACH PMT",
        category="mca_debit",
    )
    normal_3 = _txn(
        amount="-612.00",
        posted_date=date(2026, 5, 7),
        description="ONDECK ACH PMT",
        category="mca_debit",
    )
    spike = _txn(
        amount="-4500.00",
        posted_date=date(2026, 5, 18),
        description="ONDECK ACH PMT",
        category="mca_debit",
    )
    card = _make_card(
        "acceleration_clause_triggered",
        [normal_1, normal_2, normal_3, spike],
    )

    rendered = env.get_template("_evidence_panel.html.j2").render(card=card)

    assert "Normal recurring debits" in rendered
    assert "Acceleration debit" in rendered
    # ratio rendered in the spike subheader (4500 / 612 ≈ 7.4)
    assert "7." in rendered


# -- recent_account_opening explanation panel -------------------------------


@dataclass
class _AnalysisStub:
    """Tiny stub matching the attributes the explanation panels read."""

    statement_period_start: date | None = None
    statement_period_end: date | None = None
    true_revenue: Decimal | None = None


def test_recent_account_opening_panel_renders_period_date(env: Environment) -> None:
    card = _make_card("recent_account_opening", [])
    analysis = _AnalysisStub(statement_period_start=date(2026, 5, 7))

    rendered = env.get_template("_evidence_panel.html.j2").render(
        card=card, analysis=analysis
    )

    assert "May 07, 2026" in rendered or "May 7, 2026" in rendered
    assert "6-month" in rendered


def test_recent_account_opening_panel_renders_without_analysis(
    env: Environment,
) -> None:
    """No analysis in scope — panel falls back to a generic explanation."""
    card = _make_card("recent_account_opening", [])

    rendered = env.get_template("_evidence_panel.html.j2").render(card=card)

    assert "6-month" in rendered


# -- payroll_absent explanation panel ---------------------------------------


def test_payroll_absent_panel_renders_period_and_revenue(env: Environment) -> None:
    card = _make_card("payroll_absent", [])
    analysis = _AnalysisStub(
        statement_period_start=date(2026, 5, 1),
        statement_period_end=date(2026, 5, 28),
        true_revenue=Decimal("84250.00"),
    )

    rendered = env.get_template("_evidence_panel.html.j2").render(
        card=card, analysis=analysis
    )

    assert "May 01" in rendered or "May 1" in rendered
    assert "May 28" in rendered
    assert "84,250" in rendered


# -- fallback to flat table for other codes ---------------------------------


def test_fallback_panel_renders_flat_table_for_unspecialized_code(
    env: Environment,
) -> None:
    txn = _txn(
        amount="-500.00",
        posted_date=date(2026, 5, 3),
        description="NSF FEE",
        category="nsf_fee",
    )
    # nsf_clustering_short is a real code in PATTERN_COPY with no custom shape
    card = _make_card("nsf_clustering_short", [txn])

    rendered = env.get_template("_evidence_panel.html.j2").render(card=card)

    assert "<table class=\"ledger\">" in rendered
    assert "NSF FEE" in rendered


def test_panel_renders_nothing_for_code_without_evidence_and_without_explanation(
    env: Environment,
) -> None:
    """A flag code that has no source_transactions and no explanation
    panel registered renders nothing — preserves the empty-state collapse
    so the dossier doesn't show a click-to-expand toggle with no body."""
    card = _make_card("withdrawal_acceleration", [])

    rendered = env.get_template("_evidence_panel.html.j2").render(card=card)

    assert "<details>" not in rendered


# ---------------------------------------------------------------------------
# Stage 3 — Copy-rows button injection
#
# The button is purely additive: gated on source_transactions so the
# explanation-only panels (recent_account_opening, payroll_absent)
# never get a button for rows that don't exist. JS wiring lives in
# static/evidence_copy.js — these tests only assert the DOM is present
# in the right places.
# ---------------------------------------------------------------------------


def test_copy_button_renders_on_flat_evidence_panel(env: Environment) -> None:
    """A card with source_transactions using the flat fallback must
    include the copy-rows button."""
    txn = _txn(amount="-150.00", posted_date=date(2026, 5, 11), description="NSF FEE")
    card = _make_card("nsf_clustering_short", [txn])

    rendered = env.get_template("_evidence_panel.html.j2").render(card=card)

    assert 'class="evidence-copy-btn"' in rendered
    assert 'data-flag-code="nsf_clustering_short"' in rendered
    assert "Copy rows" in rendered


def test_copy_button_renders_on_custom_wash_deposit_panel(env: Environment) -> None:
    """Custom-shape panels (wash_deposit etc.) get the button too —
    JS finds tables anywhere inside the enclosing <details>, multi-table
    layouts copy cleanly."""
    d1 = _txn(amount="12400.00", posted_date=date(2026, 5, 3))
    w1 = _txn(amount="-12350.00", posted_date=date(2026, 5, 7))
    card = _make_card("wash_deposit_suspected", [d1, w1])

    rendered = env.get_template("_evidence_panel.html.j2").render(card=card)

    assert 'class="evidence-copy-btn"' in rendered
    assert 'data-flag-code="wash_deposit_suspected"' in rendered


def test_copy_button_omitted_on_recent_account_opening_explanation(
    env: Environment,
) -> None:
    """Explanation-only panels carry no rows to copy. Gating on
    source_transactions keeps the button out of these panels so workers
    don't click something that produces empty clipboard output."""
    card = _make_card("recent_account_opening", [])

    # Pass analysis=None — the explanation template's
    # `analysis is defined and analysis` guard takes the no-analysis
    # fallback branch, no stub needed.
    rendered = env.get_template("_evidence_panel.html.j2").render(
        card=card, analysis=None
    )

    # Explanation panel renders (it's in _explanation_codes) but no
    # copy button — gated on source_transactions which is empty.
    assert "<details>" in rendered
    assert "evidence-copy-btn" not in rendered


def test_copy_button_omitted_on_payroll_absent_explanation(
    env: Environment,
) -> None:
    """Symmetric guard for payroll_absent — the other explanation-only code."""
    card = _make_card("payroll_absent", [])

    rendered = env.get_template("_evidence_panel.html.j2").render(
        card=card, analysis=None
    )

    assert "<details>" in rendered
    assert "evidence-copy-btn" not in rendered


def test_copy_button_is_inside_details_wrapper(env: Environment) -> None:
    """The button must sit inside the enclosing <details> so the JS's
    .closest('details') lookup finds the right wrapper. Catches a
    placement regression that would scope the button to the wrong panel
    (or to nothing)."""
    txn = _txn(amount="-150.00", posted_date=date(2026, 5, 11), description="NSF FEE")
    card = _make_card("nsf_clustering_short", [txn])

    rendered = env.get_template("_evidence_panel.html.j2").render(card=card)

    # Find the button's offset and the closing </details>; button must
    # appear before the closing tag of the enclosing <details>.
    btn_idx = rendered.find('class="evidence-copy-btn"')
    close_idx = rendered.rfind("</details>")
    assert btn_idx != -1
    assert close_idx != -1
    assert btn_idx < close_idx, "copy button must sit inside the <details>"
