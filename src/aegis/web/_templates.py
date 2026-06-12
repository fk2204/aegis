"""Shared Jinja2 templates singleton + filter registrations.

Extracted from ``router.py`` during R4.1 so sub-routers under
``aegis.web.routers`` can ``from aegis.web._templates import templates``
without re-importing ``aegis.web.router`` (and its 5k-line dependency
graph). Filter registration happens at module import time exactly once.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from fastapi.templating import Jinja2Templates

from aegis.web._flag_labels import humanize_flag
from aegis.web._pattern_cards import (
    humanize_hard_decline,
    humanize_soft_concern,
)
from aegis.web._track_a_labels import (
    humanize_track_a_branch,
    humanize_track_a_signal,
)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# Jinja filters accept arbitrary template-side values (None, Decimal, int,
# str). The unions below cover what AEGIS actually sends through — typing
# more narrowly would force callers to pre-coerce, defeating the filter's
# purpose. Justifies the broad input types per CLAUDE.md "Any" rule.
_MoneyLike = Decimal | int | float | str | None
_NumericLike = int | str | None


def _money_filter(value: _MoneyLike, *, whole: bool = False) -> str:
    """Format a Decimal/int/float as $X,XXX[.XX]. None → em-dash."""
    if value is None or value == "":
        return "—"
    try:
        d = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return str(value)
    sign = "-" if d < 0 else ""
    d = abs(d)
    if whole or d == d.to_integral_value():
        whole_part = int(d)
        return f"{sign}${whole_part:,}"
    cents = d.quantize(Decimal("0.01"))
    int_part, _, frac = str(cents).partition(".")
    return f"{sign}${int(int_part):,}.{frac}"


def _whole_money_filter(value: _MoneyLike) -> str:
    return _money_filter(value, whole=True)


def _format_pct_filter(value: _MoneyLike) -> str:
    """Render a Decimal fraction (0.365) as a percent (``36.5%``).

    Returns ``"unavailable"`` for ``None`` rather than ``0.00%`` — per
    the R0.4 regulator-grade-lie discipline: a 0% APR rendered next to
    a 1.30x factor is the lie we explicitly refuse to render.
    Estimated-terms APR may be ``None`` when the IRR optimizer cannot
    bracket a root; surfacing that as 0% would manufacture false
    precision the operator could then quote to a funder rep.
    """
    if value is None:
        return "unavailable"
    try:
        d = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return str(value)
    pct = (d * Decimal("100")).quantize(Decimal("0.01"))
    int_part, _, frac = str(pct).partition(".")
    if not frac:
        return f"{int_part}%"
    return f"{int_part}.{frac}%"


def _days_label_filter(value: _NumericLike) -> str:
    if value is None or value == "":
        return "—"
    try:
        n = int(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{n} day" if n == 1 else f"{n} days"


def _fraud_band(score: _NumericLike) -> str:
    """Map fraud_score 0-100 to a risk band keyed off pipeline.py thresholds.

    Bands mirror parser.pipeline constants exactly: REVIEW_THRESHOLD=35,
    HARD_DECLINE_THRESHOLD=65. Keeps UI legend in sync with parse_status gate.
    """
    if score is None:
        return "unknown"
    try:
        n = int(score)
    except (TypeError, ValueError):
        return "unknown"
    if n < 35:
        return "clear"
    if n < 65:
        return "review"
    return "decline"


templates.env.filters["money"] = _money_filter
templates.env.filters["whole_money"] = _whole_money_filter
templates.env.filters["format_pct"] = _format_pct_filter
templates.env.filters["days_label"] = _days_label_filter
templates.env.filters["fraud_band"] = _fraud_band
templates.env.filters["humanize_flag"] = humanize_flag
# Verdict-section humanizers — added in the dossier signal-legibility
# consolidation (v2 catalog Bucket B; ``_pattern_cards.py``). Used by
# merchant_detail_dossier.html.j2 to render the hard-decline and
# soft-concern lists as worker-language sentences instead of raw
# identifier strings. Pattern cards already render through their own
# PATTERN_COPY map; these two close the gap for the verdict section.
templates.env.filters["humanize_hard_decline"] = humanize_hard_decline
templates.env.filters["humanize_soft_concern"] = humanize_soft_concern
# Track A signal/branch humanizers — Wave 2 dossier-legibility pass.
# ``_unified_tracks_panel.html.j2`` renders ``v.branch`` and
# ``e.signal`` straight from the engineer-facing IntegrityVerdict /
# EvidenceItem models; these filters project those tokens to the
# plain-English form workers actually read, with the raw token
# preserved as a ``title=`` tooltip for the underwriter who wants the
# code-level label on hover.
templates.env.filters["humanize_track_a_signal"] = humanize_track_a_signal
templates.env.filters["humanize_track_a_branch"] = humanize_track_a_branch


__all__ = ["templates"]
