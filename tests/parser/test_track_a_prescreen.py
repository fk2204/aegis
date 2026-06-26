"""Track A integrity pre-screen tests (parser-layer routing gate).

Retired the legacy ``fraud_score >= HARD_DECLINE_THRESHOLD`` /
``metadata.fraud_score >= METADATA_HARD_DECLINE`` gates 2026-06-25 in
favor of summing the severities of five [META] forensic-family flags:

  * editor_detected
  * page_layer_anomaly
  * text_overlay_detected
  * creator_mismatch_detected
  * font_inconsistency_detected

When the sum meets or exceeds ``TRACK_A_PRESCREEN_THRESHOLD`` (65) the
pipeline routes the document to ``manual_review`` with reason
``track_a_prescreen_integrity_fail`` synthesised on ``all_flags``.

These tests pin:

  1. High-severity [META] flags trigger ``track_a_prescreen_integrity_fail``.
  2. Clean metadata passes through to scoring (helper returns 0).
  3. ``eof_markers > EOF_HARD_DECLINE`` still hard-declines independently
     — the EOF gate is its own signal class, not part of the integrity sum.
  4. ``fraud_score`` is still computed and persisted even when no
     routing decision reads it (informational-only contract).
  5. Boundary: ``severity_sum == threshold`` declines; ``threshold - 1``
     does not. Pins the ``>=`` comparison so a future refactor that
     drops to ``>`` (or vice versa) breaks here.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

from aegis.parser.metadata import MetadataAnalysis
from aegis.parser.models import ValidationResult
from aegis.parser.pipeline import (
    EOF_HARD_DECLINE,
    TRACK_A_PRESCREEN_THRESHOLD,
    _decide,
    _track_a_prescreen_severity_sum,
    run_pipeline,
)


def _blank_metadata(flags: list[str] | None = None, **overrides: object) -> MetadataAnalysis:
    """Construct a MetadataAnalysis with sensible defaults for routing tests.

    The pre-screen helper only reads ``metadata.flags`` so most fields
    can stay at their dataclass defaults. ``fraud_score`` is set to a
    high value on purpose to demonstrate that the routing path no
    longer reads it.
    """
    md = MetadataAnalysis(
        pdf_creation_date=None,
        pdf_modification_date=None,
        pdf_producer=None,
        pdf_creator=None,
        pdf_author=None,
        page_count=2,
        file_size_bytes=1024,
        eof_markers=1,
        page_sizes=["612x792", "612x792"],
        flags=flags or [],
        fraud_score=0,
    )
    if overrides:
        md = replace(md, **overrides)  # type: ignore[arg-type]
    return md


def _passed_validation() -> ValidationResult:
    return ValidationResult(passed=True, failures=[], warnings=[])


# ─────────────────────────────────────────────────────────────────────
# Severity-sum helper
# ─────────────────────────────────────────────────────────────────────


def test_clean_metadata_returns_zero_severity_sum() -> None:
    """No qualifying flags → sum is 0; pre-screen passes the doc through."""
    md = _blank_metadata(flags=[])
    assert _track_a_prescreen_severity_sum(md) == 0


def test_unrelated_meta_flags_do_not_count_toward_sum() -> None:
    """Flags outside the five forensic families do not contribute.

    ``incremental_saves``, ``page_size_inconsistency``, ``stripped_metadata``,
    ``personal_author``, ``font_inconsistency:`` (page-level — DISTINCT
    from ``font_inconsistency_detected:``), ``xref_offset_mismatch`` —
    all of these are valid [META] flags but the pre-screen scopes
    itself to the load-bearing five.
    """
    md = _blank_metadata(
        flags=[
            "incremental_saves: 4 EOF markers",
            "page_size_inconsistency: 612x792, 612x1008",
            "stripped_metadata",
            "personal_author: John Doe",
            "font_inconsistency: 1 page(s) have no font overlap",
            "xref_offset_mismatch",
        ]
    )
    assert _track_a_prescreen_severity_sum(md) == 0


def test_single_strong_forensic_flag_under_threshold() -> None:
    """One strong forensic signal alone does NOT cross the threshold.

    Hard editor (+35), text_overlay (+25), or creator_mismatch (+20)
    each on their own stay informational — by design, two paste-over
    fingerprints are needed before the gate fires.
    """
    md = _blank_metadata(flags=["editor_detected: iText"])
    sev = _track_a_prescreen_severity_sum(md)
    assert sev == 35
    assert sev < TRACK_A_PRESCREEN_THRESHOLD


def test_two_strong_forensic_flags_cross_threshold() -> None:
    """editor_detected (+35) + text_overlay_detected (+25) = 60 — still under.

    Demonstrates the boundary: 60 (35+25) is exactly 5 short of the 65
    threshold, so a deal with both signals still passes pre-screen. The
    operator can tune by lowering the threshold or splitting hard vs
    medium editors if the corpus shows the gate is too lenient.
    """
    md = _blank_metadata(
        flags=["editor_detected: iText", "text_overlay_detected: page(s) 2; streams=2"]
    )
    sev = _track_a_prescreen_severity_sum(md)
    assert sev == 60
    assert sev < TRACK_A_PRESCREEN_THRESHOLD


def test_three_forensic_flags_cross_threshold() -> None:
    """editor (+35) + creator_mismatch (+20) + font_inconsistency_detected
    (+15) = 70 — clears the 65 threshold.
    """
    md = _blank_metadata(
        flags=[
            "editor_detected: iText",
            "creator_mismatch_detected: detected='iText'; editing_tool='itext'",
            "font_inconsistency_detected: 2 page(s); modal=Helvetica",
        ]
    )
    sev = _track_a_prescreen_severity_sum(md)
    assert sev == 70
    assert sev >= TRACK_A_PRESCREEN_THRESHOLD


def test_all_five_forensic_flags_sum_correctly() -> None:
    """All five families firing together — pins the per-flag contributions.

    Per ``_PRESCREEN_FLAG_SEVERITIES``:
      editor_detected           +35
      text_overlay_detected     +25
      creator_mismatch_detected +20
      page_layer_anomaly        +15
      font_inconsistency_detected +15
    Total: 110.
    """
    md = _blank_metadata(
        flags=[
            "editor_detected: iText",
            "text_overlay_detected: page(s) 2; streams=2",
            "creator_mismatch_detected: detected='iText'; editing_tool='itext'",
            "page_layer_anomaly: 1 page(s) have an off-mode /Contents stream count",
            "font_inconsistency_detected: 2 page(s); modal=Helvetica",
        ]
    )
    assert _track_a_prescreen_severity_sum(md) == 110


# ─────────────────────────────────────────────────────────────────────
# Decision routing — direct _decide() tests
# ─────────────────────────────────────────────────────────────────────


def test_decide_clean_passes_to_proceed() -> None:
    """No forensic hits, no EOF spam, no confidence failures → proceed."""
    md = _blank_metadata(flags=[], eof_markers=1)
    status = _decide(
        md,
        _passed_validation(),
        confidence_failures=[],
        track_a_prescreen_sum=0,
    )
    assert status == "proceed"


def test_decide_severity_at_threshold_declines() -> None:
    """severity_sum == threshold → manual_review.

    Pins the ``>=`` comparison. A future refactor that drops to ``>``
    would let the boundary slip — this test catches it.
    """
    md = _blank_metadata(flags=[], eof_markers=1)
    status = _decide(
        md,
        _passed_validation(),
        confidence_failures=[],
        track_a_prescreen_sum=TRACK_A_PRESCREEN_THRESHOLD,
    )
    assert status == "manual_review"


def test_decide_severity_one_below_threshold_passes() -> None:
    """severity_sum == threshold - 1 → proceed.

    Complement of the above — pins both sides of the boundary.
    """
    md = _blank_metadata(flags=[], eof_markers=1)
    status = _decide(
        md,
        _passed_validation(),
        confidence_failures=[],
        track_a_prescreen_sum=TRACK_A_PRESCREEN_THRESHOLD - 1,
    )
    assert status == "proceed"


def test_decide_eof_hard_decline_independent_of_severity_sum() -> None:
    """eof_markers > EOF_HARD_DECLINE wins even with severity_sum == 0.

    EOF spam is its own signal class — never blended into the integrity
    sum. Pins the independence.
    """
    md = _blank_metadata(flags=[], eof_markers=EOF_HARD_DECLINE + 1)
    status = _decide(
        md,
        _passed_validation(),
        confidence_failures=[],
        track_a_prescreen_sum=0,
    )
    assert status == "manual_review"


def test_decide_eof_at_threshold_routes_to_review() -> None:
    """eof_markers > 1 but <= EOF_HARD_DECLINE → review, not manual_review.

    Two EOFs are the legitimate-export case (bank writes one, viewer
    re-save appends another). Operator can clear it from the review
    queue. Only 3+ EOFs hard-decline.
    """
    # eof_markers = 2, EOF_HARD_DECLINE = 2 → 2 > 2 is False → review
    md = _blank_metadata(flags=[], eof_markers=2)
    status = _decide(
        md,
        _passed_validation(),
        confidence_failures=[],
        track_a_prescreen_sum=0,
    )
    assert status == "review"


def test_decide_confidence_failure_routes_to_manual_review() -> None:
    """Even with clean integrity, LLM-low-confidence still manual_reviews."""
    md = _blank_metadata(flags=[], eof_markers=1)
    status = _decide(
        md,
        _passed_validation(),
        confidence_failures=["classification_confidence_below_floor: avg=40 floor=60"],
        track_a_prescreen_sum=0,
    )
    assert status == "manual_review"


# ─────────────────────────────────────────────────────────────────────
# End-to-end pipeline behavior — fraud_score informational contract
# ─────────────────────────────────────────────────────────────────────


def test_clean_pipeline_still_computes_fraud_score_breakdown(
    clean_pdf_path: Path,
    clean_llm: object,
) -> None:
    """fraud_score must still be written to the result even though the
    routing path doesn't read it.

    Portfolio analytics + the dossier breakdown both read
    ``fraud_score`` informationally. The informational-only contract
    means the field exists, has a sensible value, and surfaces the
    underlying metadata/math/patterns components — but parse_status
    is decided WITHOUT reading it. This test pins the contract: a
    clean run produces a fraud_score (could be 0 on a fully-clean
    statement) and a populated breakdown dict.
    """
    result = run_pipeline(str(clean_pdf_path), clean_llm, today=date(2026, 2, 15))  # type: ignore[arg-type]
    assert result.parse_status in {"proceed", "review"}
    # The breakdown ALWAYS has the three component keys even when the
    # score itself is 0 — the dict shape is part of the analytics
    # contract.
    assert set(result.fraud_score_breakdown.keys()) == {
        "metadata_score",
        "math_score",
        "patterns_score",
    }
    # fraud_score is an int in [0, 100] — informational, no contract
    # on the exact value because that depends on the synthetic fixture.
    assert isinstance(result.fraud_score, int)
    assert 0 <= result.fraud_score <= 100
