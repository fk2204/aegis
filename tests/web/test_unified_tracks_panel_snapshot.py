"""Snapshot tests for the Track A panel HTML (Wave 3 item 3.1).

Locks the rendered ``_unified_tracks_panel.html.j2`` shape for the four
canonical Track A verdicts so a future refactor must regenerate the
snapshots intentionally and the diff is reviewable.

Why
---
The panel template absorbed Track A humanizer filters in commit
``4ceacd9`` and the F1/F2 fixes (``c3beb25``, ``67797aa``, ``eb6bf7a``)
changed what gets rendered for the ``strong_metadata`` branch + locked
the rationale length at 320 chars. There was zero snapshot coverage on
the panel HTML — silent regressions would have shipped. These four
snapshots bind the rendered output to the four canonical verdicts.

Pattern
-------
Mirrors ``tests/compliance/test_disclosure_tier1_context.py`` and
``tests/compliance/test_broker_compensation_letter.py`` — both use
``pytest-snapshot``'s ``snapshot.assert_match(html, "<name>.html")``
with a per-domain ``snapshot.snapshot_dir = "tests/snapshots/<topic>"``.
Snapshot files commit alongside the test; regenerate with
``pytest --snapshot-update`` and a commit message explaining why.

Rendering mechanism
-------------------
The partial is included from ``merchant_detail_dossier.html.j2`` under
the variable name ``unified_tracks``. To isolate the partial we reuse
the production ``aegis.web._templates.templates`` Jinja singleton
(``templates.env``) — this is the same env that wires up the Wave 2
humanizer filters (``humanize_track_a_branch`` /
``humanize_track_a_signal``). We then call
``env.get_template("_unified_tracks_panel.html.j2").render(unified_tracks=view)``.
The partial's ``{% include "_status_chip.html.j2" %}`` resolves through
the same env so the rendered HTML matches what the dossier route
emits 1:1.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from aegis.scoring_v2.dossier_panel import (
    UnifiedTracksView,
    build_unified_tracks_view,
)
from aegis.storage import DocumentRow
from aegis.web._templates import templates


def _doc(
    *,
    metadata_flags: tuple[str, ...] = (),
    all_flags: tuple[str, ...] = (),
    metadata_score: int = 0,
) -> DocumentRow:
    """Construct a synthetic DocumentRow with the integrity signals only.

    Snapshot determinism requires a stable ``uploaded_at`` — the panel
    sorts documents by uploaded_at descending. We use a fixed UTC
    instant so the same doc always sorts to the same slot regardless
    of clock drift between runs.
    """
    return DocumentRow.model_validate(
        {
            "id": uuid4(),
            "file_hash": "z" * 64,
            "byte_size": 1024,
            "original_filename": "stmt.pdf",
            "parse_status": "manual_review",
            "metadata_flags": list(metadata_flags),
            "all_flags": list(all_flags),
            "fraud_score_breakdown": {"metadata": metadata_score},
            # Fixed timestamp — snapshot stability. The panel only
            # uses uploaded_at to sort; the value never appears in
            # the rendered HTML.
            "uploaded_at": datetime(2026, 6, 11, 12, 0, tzinfo=UTC),
        }
    )


def _build_view(
    *,
    metadata_flags: tuple[str, ...] = (),
    all_flags: tuple[str, ...] = (),
    metadata_score: int = 0,
) -> UnifiedTracksView:
    """Build a UnifiedTracksView with a single document carrying the
    given integrity signals. Track B + C will be None (no transactions);
    the panel renders the insufficient-data empty state for them, which
    is what we want — the snapshot is locking Track A's shape.
    """
    doc = _doc(
        metadata_flags=metadata_flags,
        all_flags=all_flags,
        metadata_score=metadata_score,
    )
    return build_unified_tracks_view(
        documents=[doc],
        list_transactions=lambda _id: [],
    )


def _render_panel(view: UnifiedTracksView) -> str:
    """Render the unified-tracks partial in isolation.

    Reuses ``aegis.web._templates.templates.env`` so the Wave 2
    humanizer filters are active and ``{% include "_status_chip.html.j2" %}``
    resolves through the same template loader the dossier route uses.
    """
    template = templates.env.get_template("_unified_tracks_panel.html.j2")
    return template.render(unified_tracks=view)


# ─────────────────────────────────────────────────────────────────────
# Snapshot dir convention — mirrors tests/compliance snapshot tests.
# Each case writes to tests/snapshots/dossier_panel/panel_<branch>.html.
# ─────────────────────────────────────────────────────────────────────


_SNAPSHOT_DIR = "tests/snapshots/dossier_panel"


def test_panel_strong_metadata_snapshot(snapshot: Any) -> None:
    """F2 case — strong metadata fail with corroborating drift.

    Signature: ``metadata_score=72`` + iText editor flag + a
    ``reconciliation_failed_period`` drift failure. Expects the
    rendered HTML to carry BOTH the metadata_score evidence row AND
    the reconciliation drift row (the F2 fix surfaces drift on
    strong_metadata fails as corroborating evidence).
    """
    view = _build_view(
        metadata_score=72,
        metadata_flags=("editor_detected: iText 2.1.7 by 1T3XT",),
        all_flags=(
            "reconciliation_failed_period: expected 1000 got 950",
        ),
    )
    assert view.integrity_worst_verdict == "fail"
    assert len(view.integrity_verdicts) == 1
    assert view.integrity_verdicts[0].branch == "strong_metadata"

    html = _render_panel(view)
    snapshot.snapshot_dir = _SNAPSHOT_DIR
    snapshot.assert_match(html, "panel_strong_metadata.html")


def test_panel_drift_plus_editor_snapshot(snapshot: Any) -> None:
    """A&R KM-shaped fail — iText editor + multiple reconciliation
    drift failures, metadata_score in the medium band (38).

    Branch precedence: even though metadata_score < 50, the editor
    flag + drift combo fires the drift_plus_editor branch and the
    verdict lands fail.
    """
    view = _build_view(
        metadata_score=38,
        metadata_flags=("editor_detected: iText 2.1.7 by 1T3XT",),
        all_flags=(
            "reconciliation_failed_period: expected -263.89 got 236.11",
            "reconciliation_failed_withdrawal_total: "
            "listed 31416.55 vs printed 30726.55",
        ),
    )
    assert view.integrity_worst_verdict == "fail"
    assert len(view.integrity_verdicts) == 1
    assert view.integrity_verdicts[0].branch == "drift_plus_editor"

    html = _render_panel(view)
    snapshot.snapshot_dir = _SNAPSHOT_DIR
    snapshot.assert_match(html, "panel_drift_plus_editor.html")


def test_panel_drift_alone_snapshot(snapshot: Any) -> None:
    """VU 7722-shaped review — reconciliation drift with no editor
    metadata and a low metadata_score (10).

    Branch: drift_alone — could be genuine OCR / parser miss, surfaces
    as review for the underwriter to adjudicate.
    """
    view = _build_view(
        metadata_score=10,
        metadata_flags=(),
        all_flags=(
            "reconciliation_failed_withdrawal_total: "
            "listed 100 vs printed 90",
        ),
    )
    assert view.integrity_worst_verdict == "review"
    assert len(view.integrity_verdicts) == 1
    assert view.integrity_verdicts[0].branch == "drift_alone"

    html = _render_panel(view)
    snapshot.snapshot_dir = _SNAPSHOT_DIR
    snapshot.assert_match(html, "panel_drift_alone.html")


def test_panel_clean_snapshot(snapshot: Any) -> None:
    """Clean case — innocuous metadata flag, no editor, no drift,
    low metadata_score.

    Branch: clean. Surfaces as a green CLEAN chip + no evidence list.
    The innocuous ``page_count:`` flag is what keeps the doc from
    getting skipped entirely by ``build_unified_tracks_view``
    (which omits docs with no signal source at all).
    """
    view = _build_view(
        metadata_score=8,
        metadata_flags=("page_count: 4",),
        all_flags=(),
    )
    assert view.integrity_worst_verdict == "clean"
    assert len(view.integrity_verdicts) == 1
    assert view.integrity_verdicts[0].branch == "clean"

    html = _render_panel(view)
    snapshot.snapshot_dir = _SNAPSHOT_DIR
    snapshot.assert_match(html, "panel_clean.html")


# ─────────────────────────────────────────────────────────────────────
# Sanity invariants — checked OUTSIDE the snapshot bytes so a refresh
# (--snapshot-update) does not silently drop these properties.
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("metadata_flags", "all_flags", "metadata_score", "humanized_label"),
    [
        (
            ("editor_detected: iText 2.1.7 by 1T3XT",),
            ("reconciliation_failed_period: expected 1000 got 950",),
            72,
            "Strong metadata anomaly",
        ),
        (
            ("editor_detected: iText 2.1.7 by 1T3XT",),
            (
                "reconciliation_failed_period: expected -263.89 got 236.11",
                "reconciliation_failed_withdrawal_total: "
                "listed 31416.55 vs printed 30726.55",
            ),
            38,
            "Editor tampering + reconciliation drift",
        ),
        (
            (),
            (
                "reconciliation_failed_withdrawal_total: "
                "listed 100 vs printed 90",
            ),
            10,
            "Reconciliation drift alone",
        ),
        (
            ("page_count: 4",),
            (),
            8,
            "Clean",
        ),
    ],
)
def test_panel_humanized_branch_label_appears_in_body(
    metadata_flags: tuple[str, ...],
    all_flags: tuple[str, ...],
    metadata_score: int,
    humanized_label: str,
) -> None:
    """Each verdict's humanized branch label appears in the rendered
    body (not just as a ``title=`` tooltip).

    Bounds the humanizer's output as a guard against a refactor that
    accidentally drops the filter from the template; the snapshot
    would catch the resulting HTML diff, but this assertion catches
    the semantic regression directly.
    """
    view = _build_view(
        metadata_flags=metadata_flags,
        all_flags=all_flags,
        metadata_score=metadata_score,
    )
    html = _render_panel(view)
    # The humanized label appears as the visible body text of the
    # branch cell — preceded by '>' (the cell open tag's close) and
    # followed by '<' (the closing cell tag). This is the same shape
    # the existing test_dossier_humanizes_* tests assert.
    assert f">{humanized_label}<" in html


def test_panel_raw_branch_token_in_tooltip_only_for_non_clean() -> None:
    """The raw engineer branch token (e.g. ``drift_plus_editor``) must
    appear in a ``title=`` tooltip — the Wave 2 humanizer design.

    Clean is excluded because ``branch="clean"`` would also match the
    humanized label "Clean" in lower-case; the meaningful guard is on
    the non-clean branches whose raw token is structurally distinct
    from the humanized label.
    """
    for metadata_flags, all_flags, metadata_score, raw_branch in [
        (
            ("editor_detected: iText 2.1.7 by 1T3XT",),
            ("reconciliation_failed_period: expected 1000 got 950",),
            72,
            "strong_metadata",
        ),
        (
            ("editor_detected: iText 2.1.7 by 1T3XT",),
            (
                "reconciliation_failed_period: expected -263.89 got 236.11",
            ),
            38,
            "drift_plus_editor",
        ),
        (
            (),
            (
                "reconciliation_failed_withdrawal_total: "
                "listed 100 vs printed 90",
            ),
            10,
            "drift_alone",
        ),
    ]:
        view = _build_view(
            metadata_flags=metadata_flags,
            all_flags=all_flags,
            metadata_score=metadata_score,
        )
        html = _render_panel(view)
        assert f'title="{raw_branch}"' in html, (
            f"Raw branch token {raw_branch!r} missing from title= tooltip"
        )


def test_panel_rationale_respects_pydantic_320_char_limit() -> None:
    """The IntegrityVerdict.rationale Pydantic field caps at 320 chars
    (models.py). The F1 fix in ``frame_drift_plus_editor`` truncates
    long editor vendor strings to keep the rationale below the cap so
    the verdict does not silently downgrade to None. Verify the
    rendered rationale on the longest fail case (drift_plus_editor)
    stays within bounds.
    """
    view = _build_view(
        metadata_score=38,
        metadata_flags=(
            # Deliberately long vendor string — exercises the
            # truncation guard in frame_drift_plus_editor.
            "editor_detected: SomeVendor PhantomPDF Editor "
            "VeryLongBuildIdentifier 2026.06.11.alpha.build.42",
        ),
        all_flags=(
            "reconciliation_failed_period: expected -263.89 got 236.11",
        ),
    )
    assert len(view.integrity_verdicts) == 1
    assert len(view.integrity_verdicts[0].rationale) <= 320
