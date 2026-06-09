"""R4.8 — mobile-responsive dossier review.

The audit (plan-file commit ``d3d802f``) flagged that the dossier was
desktop-only:

    "Currently designed for desktop (masthead + sidebar + main don't
    reflow). Brokers do deals on phones."

These tests lock down the minimal mobile contract added 2026-06-09:

1. The dossier template + base layout both emit a viewport meta tag
   with ``width=device-width``. Without that, mobile browsers render
   the page at desktop width and shrink — every responsive rule
   below would be defeated by the user-agent scaling layer.
2. The dossier stylesheet (``aegis-dossier.css``) carries an
   ``@media (max-width: 768px)`` breakpoint. 768px is the iPad
   portrait / large-phone landscape boundary widely used as the
   "tablet-or-smaller" gate.
3. Every wide ``<table>`` inside ``merchant_detail_dossier.html.j2``
   is wrapped in ``<div class="table-scroll">`` so the rightmost
   columns are reachable via horizontal swipe instead of being cut
   off on a 360-390px phone viewport.
4. The ``.table-scroll`` selector exists in the CSS and uses
   ``overflow-x: auto`` (the actual scroll affordance — the wrapper
   is a no-op without it).

Pure DOM-string tests; no real browser needed. The contract under test
is what the rendered template + stylesheet bytes literally contain,
not how Chrome lays them out. Test discipline mirrors
``tests/web/test_topstrip.py`` (which asserts on substring presence
in the rendered HTML) and ``tests/web/test_dossier_pattern_analysis_source``
(which exercises template helpers directly without spinning a server).
"""

from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Path constants — anchored to the repo root via the test file's location so
# the asserts run identically on developer boxes (Windows path separators)
# and CI (POSIX). pathlib normalizes per-platform.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATES_DIR = _REPO_ROOT / "src" / "aegis" / "web" / "templates"
_STATIC_DIR = _REPO_ROOT / "src" / "aegis" / "web" / "static"

_DOSSIER_TEMPLATE = _TEMPLATES_DIR / "merchant_detail_dossier.html.j2"
_BASE_TEMPLATE = _TEMPLATES_DIR / "base.html.j2"
_DOSSIER_CSS = _STATIC_DIR / "aegis-dossier.css"


def _read(path: Path) -> str:
    """Read a UTF-8 text file. Encoding is explicit so we never depend on
    the Python default — production runs on Linux (utf-8) and dev runs on
    Windows (cp1252 default); the file IS utf-8 on disk."""
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Viewport meta tag — present on BOTH the base layout and the
#    self-contained dossier template.
# ---------------------------------------------------------------------------


def test_base_template_has_viewport_meta() -> None:
    """``base.html.j2`` (the chrome every operator page except the
    dossier extends) emits ``<meta name="viewport" ...>`` with
    ``width=device-width``. Without it Safari iOS scales pages to a
    980px virtual viewport and the breakpoints below would never fire.
    """
    html = _read(_BASE_TEMPLATE)
    # Tolerant of attribute ordering + single/double quotes; the
    # contract under test is "viewport tag exists and includes
    # width=device-width", not exact byte match.
    assert re.search(
        r'<meta\s+name=["\']viewport["\'][^>]*width=device-width', html
    ), "base.html.j2 missing <meta name='viewport' ... width=device-width>"


def test_dossier_template_has_viewport_meta() -> None:
    """``merchant_detail_dossier.html.j2`` is the ONLY operator page
    that does not extend ``base.html.j2`` (self-contained for editorial
    purity per the template's header comment). It carries its own
    viewport meta — losing it would silently regress mobile rendering
    on the highest-traffic surface.
    """
    html = _read(_DOSSIER_TEMPLATE)
    assert re.search(
        r'<meta\s+name=["\']viewport["\'][^>]*width=device-width', html
    ), (
        "merchant_detail_dossier.html.j2 missing "
        "<meta name='viewport' ... width=device-width>"
    )


# ---------------------------------------------------------------------------
# 2. ``@media (max-width: 768px)`` breakpoint present in
#    ``aegis-dossier.css``.
# ---------------------------------------------------------------------------


def test_dossier_css_has_mobile_breakpoint() -> None:
    """The dossier stylesheet carries the R4.8 ``@media (max-width:
    768px)`` block. Tolerant of internal whitespace (some authors write
    ``@media(max-width:768px)`` without spaces). The exact 768px gate
    is asserted because it's the documented "phone or small tablet"
    boundary — a future change to 600px or 1024px is a real shift in
    contract and should be a deliberate edit (and a deliberate test
    update), not a silent regression."""
    css = _read(_DOSSIER_CSS)
    assert re.search(
        r"@media\s*\(\s*max-width\s*:\s*768px\s*\)", css
    ), (
        "aegis-dossier.css missing @media (max-width: 768px) breakpoint — "
        "R4.8 contract"
    )


def test_dossier_css_table_scroll_rule_exists() -> None:
    """The ``.table-scroll`` utility class is defined with
    ``overflow-x: auto``. The wrapper is the load-bearing piece — the
    template hands a wide table to it expecting touch scroll on
    phones. Without ``overflow-x: auto`` the wrapper would be a
    no-op div and the rightmost columns would be clipped or push the
    page to horizontal scroll.

    Both the selector AND the property are asserted so a future
    refactor can't accidentally split the rule into something that
    matches the selector regex but no longer hides overflow."""
    css = _read(_DOSSIER_CSS)
    assert ".table-scroll" in css, (
        "aegis-dossier.css missing .table-scroll utility class"
    )
    # The selector lives in two rules — the wrapper itself and the
    # nested ``> table`` min-width hint. The wrapper carries the
    # overflow-x. We assert the property within ~200 chars of the
    # FIRST .table-scroll selector occurrence to confirm they're in
    # the same rule block, not coincidentally adjacent.
    match = re.search(
        r"\.dossier-page\s+\.table-scroll\s*\{[^}]*overflow-x\s*:\s*auto",
        css,
    )
    assert match, (
        ".table-scroll selector exists but overflow-x:auto is not in its "
        "rule block — wrapper would be a no-op"
    )


# ---------------------------------------------------------------------------
# 3. Every wide ``<table>`` in the dossier is wrapped in
#    ``<div class="table-scroll">``.
#
# "Wide" = any table that today exceeds the typical mobile viewport
# (~390px on iPhone 14 Pro). All four tables in the dossier qualify:
#
#   - .ledger (inside stacking debit details — 3 columns + amt + cite)
#   - .audit-ledger x3 (statements ledger 8 cols, profile 2 cols
#     with 200px label column, audit log 4 cols)
#
# The assertion counts how many ``<table`` openings live inside a
# ``<div class="table-scroll">`` block — if a future edit adds a new
# table without the wrapper, this test fails.
# ---------------------------------------------------------------------------


def test_every_table_wrapped_in_table_scroll() -> None:
    """Every ``<table`` element in the dossier template is preceded by
    a ``<div class="table-scroll">`` opener.

    Strategy: find every ``<table`` occurrence; for each, walk
    backward to the nearest ``<div`` and assert it carries the
    ``table-scroll`` class. Detects two bug classes:
       (a) a new table added without the wrapper, and
       (b) the wrapper present but on the wrong sibling (e.g. wrapping
           a ``<details>`` instead of the ``<table>`` itself)."""
    html = _read(_DOSSIER_TEMPLATE)

    # Find every <table ...> opening tag and its starting byte index.
    table_positions = [m.start() for m in re.finditer(r"<table\b", html)]
    assert table_positions, (
        "merchant_detail_dossier.html.j2 contains no <table> elements — "
        "this test (and the audit it locks down) has lost its subject. "
        "Verify the dossier still renders ledger / audit data before "
        "deleting this assertion."
    )

    for pos in table_positions:
        # Scan the 400 chars preceding the <table> looking for the
        # most-recent <div ...> opening. The dossier indents 4-8
        # spaces per level + a comment block at most ~250 chars; 400
        # is generous without being so wide that it catches a
        # different div in a different section.
        window_start = max(0, pos - 400)
        window = html[window_start:pos]
        # Last opening div tag in the window — this is the table's
        # immediate parent (or near-parent through Jinja).
        div_opens = list(re.finditer(r'<div\b[^>]*>', window))
        assert div_opens, (
            f"<table at byte {pos} has no enclosing <div> in the 400 "
            f"chars before it; window:\n{window!r}"
        )
        nearest = div_opens[-1].group(0)
        assert "table-scroll" in nearest, (
            f"<table at byte {pos} is not wrapped in "
            f'<div class="table-scroll">. Nearest <div> tag was: '
            f"{nearest!r}"
        )


# ---------------------------------------------------------------------------
# 4. Smoke test on the *content* of the mobile breakpoint — confirm the
#    three load-bearing rules (sidebar stacks, KPI grid auto-fits, body
#    grid collapses) actually live inside the ``@media`` block.
#
# This guards against a future "split the @media block in two" refactor
# accidentally moving the rules outside the breakpoint guard and
# changing desktop layout. The exact selectors here are deliberate:
# they're the three highest-stakes rules from the R4.8 design.
# ---------------------------------------------------------------------------


def test_media_block_contains_sidebar_stacking_rule() -> None:
    """Inside the ``@media (max-width: 768px)`` block, ``.sheet``
    drops to a single-column grid. Without this rule the 260px
    sidebar would still claim a column on phones and crowd main.
    """
    css = _read(_DOSSIER_CSS)
    media_block = _extract_media_block(css)
    assert ".sheet" in media_block, (
        "@media block missing .sheet override — sidebar would not stack"
    )
    assert "grid-template-columns: 1fr" in media_block, (
        "@media block missing grid-template-columns:1fr — sidebar would "
        "not stack"
    )


def test_media_block_contains_kpi_auto_fit_rule() -> None:
    """Inside ``@media``, ``.spread`` (KPI strip) switches to
    ``repeat(auto-fit, minmax(...))`` so 5 tiles wrap on phone
    widths. This mirrors the U11 portfolio + U2 stacking-card pattern
    already in production for other dashboards."""
    css = _read(_DOSSIER_CSS)
    media_block = _extract_media_block(css)
    assert ".spread" in media_block, (
        "@media block missing .spread override — KPI grid would not wrap"
    )
    assert "auto-fit" in media_block, (
        "@media block missing auto-fit — KPI grid would not wrap"
    )


def test_media_block_contains_aside_rail_stack_rule() -> None:
    """``aside.rail`` un-sticks on mobile so it stacks above main
    instead of floating across content. ``position: static`` is the
    key declaration — desktop uses ``position: sticky``."""
    css = _read(_DOSSIER_CSS)
    media_block = _extract_media_block(css)
    assert "aside.rail" in media_block, (
        "@media block missing aside.rail override — sticky sidebar would "
        "remain on mobile"
    )
    assert "position: static" in media_block, (
        "@media block missing position:static on aside.rail — sticky "
        "behavior would remain on mobile"
    )


def _extract_media_block(css: str) -> str:
    """Return the contents of the FIRST ``@media (max-width: 768px)``
    block (between the opening ``{`` and its matching ``}``). Uses a
    simple depth counter — ``@media`` blocks contain nested rule
    blocks, so a naive ``[^}]*`` regex would stop at the first inner
    ``}``."""
    match = re.search(r"@media\s*\(\s*max-width\s*:\s*768px\s*\)\s*\{", css)
    assert match, "no @media (max-width: 768px) block found"
    start = match.end()
    depth = 1
    i = start
    while i < len(css) and depth > 0:
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, "unterminated @media block (depth never returned to 0)"
    return css[start : i - 1]
