"""Tests for ``aegis.web._track_a_labels``.

Pins every registered token → label entry so an accidental rename in the
map gets caught at test time rather than landing in the dossier UI
silently. Also asserts the defensive fallback never returns a raw
underscored token (workers should never see snake_case in the rendered
panel) and that the Jinja Environment exposes the filters.
"""

from __future__ import annotations

import pytest

from aegis.web._templates import templates
from aegis.web._track_a_labels import (
    humanize_track_a_branch,
    humanize_track_a_signal,
)

# ---------------------------------------------------------------------------
# Signal labels — pin every registered entry.
# ---------------------------------------------------------------------------

_SIGNAL_CASES: tuple[tuple[str, str], ...] = (
    ("metadata_score", "Metadata anomaly score"),
    ("editor_detected", "Editor metadata fingerprint"),
    ("metadata_flag", "Metadata flag"),
    ("reconciliation_failed_period", "Reconciliation: period total mismatch"),
    ("reconciliation_failed_deposit", "Reconciliation: deposit total mismatch"),
    (
        "reconciliation_failed_deposit_total",
        "Reconciliation: deposit total mismatch",
    ),
    (
        "reconciliation_failed_withdrawal_total",
        "Reconciliation: withdrawal total mismatch",
    ),
    (
        "reconciliation_failed_running_balance",
        "Reconciliation: running balance drift",
    ),
    (
        "reconciliation_failed_daily_running_balance",
        "Reconciliation: daily running balance drift",
    ),
    (
        "reconciliation_failed_intraday",
        "Reconciliation: intraday row mismatch",
    ),
    (
        "reconciliation_failed_intraday_count",
        "Reconciliation: intraday rows failing",
    ),
    ("future_dated", "Future-dated statement period"),
    ("future_dated_period", "Future-dated statement period"),
)


@pytest.mark.parametrize("token,expected", _SIGNAL_CASES)
def test_humanize_track_a_signal_known_token(token: str, expected: str) -> None:
    assert humanize_track_a_signal(token) == expected


# ---------------------------------------------------------------------------
# Branch labels — pin every Literal member of IntegrityBranch.
# ---------------------------------------------------------------------------

_BRANCH_CASES: tuple[tuple[str, str], ...] = (
    ("clean", "Clean"),
    ("strong_metadata", "Strong metadata anomaly"),
    ("drift_plus_editor", "Editor tampering + reconciliation drift"),
    ("medium_corroborated", "Medium metadata + drift corroboration"),
    ("drift_alone", "Reconciliation drift alone"),
)


@pytest.mark.parametrize("token,expected", _BRANCH_CASES)
def test_humanize_track_a_branch_known_token(token: str, expected: str) -> None:
    assert humanize_track_a_branch(token) == expected


# ---------------------------------------------------------------------------
# Defensive fallback — load-bearing contract.
# ---------------------------------------------------------------------------


def test_unknown_signal_token_title_cases_snake_case() -> None:
    """Brand-new parser detector lands without a registered label —
    snake_case identifier renders as title-cased plain text rather than
    leaking the raw token to the dossier."""
    out = humanize_track_a_signal("reconciliation_failed_foo")
    assert out == "Reconciliation Failed Foo"
    assert "_" not in out
    assert out != "reconciliation_failed_foo"


def test_unknown_branch_token_title_cases_snake_case() -> None:
    """Defense-in-depth — IntegrityBranch is a closed Literal but a
    future addition could ship without updating the map; the fallback
    has to be readable."""
    out = humanize_track_a_branch("brand_new_branch")
    assert out == "Brand New Branch"
    assert "_" not in out


def test_unknown_signal_never_returns_empty_or_raw_token() -> None:
    """The defensive fallback never returns ``""`` or the raw
    snake_case token unchanged for a non-empty input."""
    for token in (
        "unfamiliar_signal_a",
        "some_new_drift_class",
        "ai_generated_score",
    ):
        out = humanize_track_a_signal(token)
        assert out, f"empty label for token {token!r}"
        assert out != token, f"raw token leaked for {token!r}"
        assert "_" not in out


def test_empty_input_returns_empty_string() -> None:
    """Empty / whitespace-only input shouldn't crash and shouldn't
    fabricate output — template can ``{% if … %}`` around it."""
    assert humanize_track_a_signal("") == ""
    assert humanize_track_a_signal("   ") == ""
    assert humanize_track_a_branch("") == ""
    assert humanize_track_a_branch("   ") == ""


def test_token_without_underscores_passes_through_unchanged() -> None:
    """Tokens without underscores aren't reformatted — preserves
    deliberate camelCase or acronyms a caller might pass."""
    assert humanize_track_a_signal("AcronymToken") == "AcronymToken"
    assert humanize_track_a_branch("clean") == "Clean"  # registered


# ---------------------------------------------------------------------------
# Jinja filter registration — verifies the template can actually call them.
# ---------------------------------------------------------------------------


def test_jinja_filters_registered() -> None:
    """The shared ``templates`` singleton must expose both humanizers as
    filters so ``_unified_tracks_panel.html.j2`` can call them via the
    ``|`` syntax. A regression where the imports silently moved would
    cause the template to render raw tokens again."""
    assert "humanize_track_a_signal" in templates.env.filters
    assert "humanize_track_a_branch" in templates.env.filters
    assert templates.env.filters["humanize_track_a_signal"] is humanize_track_a_signal
    assert templates.env.filters["humanize_track_a_branch"] is humanize_track_a_branch


def test_jinja_filter_renders_in_template_expression() -> None:
    """End-to-end render: hand the filter a token through Jinja's
    expression evaluator and confirm the rendered string is the
    humanized form, not the raw token."""
    env = templates.env
    rendered = env.from_string(
        "{{ value | humanize_track_a_branch }}"
    ).render(value="drift_plus_editor")
    assert rendered == "Editor tampering + reconciliation drift"

    rendered_signal = env.from_string(
        "{{ value | humanize_track_a_signal }}"
    ).render(value="reconciliation_failed_period")
    assert rendered_signal == "Reconciliation: period total mismatch"
