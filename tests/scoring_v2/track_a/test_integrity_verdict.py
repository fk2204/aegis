"""Track A — Integrity Verdict — acceptance tests.

Branch coverage + structural guard + Q2 mapping verification + the
two real-merchant acceptance cases:

* **A&R KM** — iText 2.1.7 editor metadata + 4-of-4 month
  reconciliation drift = the competent-fabrication signature →
  ``fail`` via the ``drift_plus_editor`` branch.
* **VU 7722** — March 2026 statement carries reconciliation period
  drift but no editor metadata in the parser's current detection →
  ``review`` via the ``drift_alone`` branch.

The signal strings used in these acceptance tests are the LITERAL
flags AEGIS's parser produces today (per ``aegis.parser.metadata``
and ``aegis.parser.validate``). They are byte-for-byte what the
parser emits — same CLAUDE.md "external-integration test discipline"
rule the rest of the redesign follows.
"""

from __future__ import annotations

import pytest

from aegis.scoring_v2.track_a import (
    DocumentIntegritySignals,
    IntegrityVerdict,
    compute_integrity_verdict,
)

# ─────────────────────────────────────────────────────────────────────
# Branch coverage — one test per Q2 branch
# ─────────────────────────────────────────────────────────────────────


def test_clean_branch_fires_when_nothing_does() -> None:
    """No metadata signal, no reconciliation failure → clean."""
    signals = DocumentIntegritySignals(
        document_id="doc_clean",
        metadata_score=8,
        metadata_flags=("page_count: 4",),
        validation_failures=(),
    )
    v = compute_integrity_verdict(signals)
    assert v.verdict == "clean"
    assert v.branch == "clean"
    assert v.evidence == ()
    assert "no integrity signals fired" in v.rationale.lower()


def test_strong_metadata_branch_fires_at_score_threshold() -> None:
    """metadata_score >= 50 → fail regardless of corroboration."""
    signals = DocumentIntegritySignals(
        document_id="doc_strong",
        metadata_score=72,
        metadata_flags=(
            "editor_detected: Foxit PhantomPDF 11.2",
            "missing_pdf_signature",
        ),
        validation_failures=(),
    )
    v = compute_integrity_verdict(signals)
    assert v.verdict == "fail"
    assert v.branch == "strong_metadata"
    # Both flags surface as evidence so the underwriter sees what
    # contributed to the score.
    signal_kinds = {e.signal for e in v.evidence}
    assert "metadata_score" in signal_kinds
    assert "editor_detected" in signal_kinds
    assert "metadata_flag" in signal_kinds


def test_strong_metadata_with_drift_surfaces_both() -> None:
    """strong_metadata fail with corroborating reconciliation drift —
    BOTH the metadata score row AND each drift failure must appear as
    evidence so the underwriter sees the full pattern and doesn't
    soften the fail to "review" thinking it's metadata noise alone.

    Regression guard for F2 (track_a_audit_2026-06-12): the original
    branch-1 evidence build dropped ``drift_failures`` even though they
    were already computed. The competent-fabrication case can satisfy
    BOTH branch 1 (score >= 50) and branch 2 (editor + drift) — branch
    1 wins on precedence, and its evidence must include the drift rows
    that branch 2 would have surfaced.
    """
    signals = DocumentIntegritySignals(
        document_id="doc_strong_with_drift",
        metadata_score=72,
        metadata_flags=("editor_detected: Foxit PhantomPDF 11.2",),
        validation_failures=("reconciliation_failed_period: bal_2026-01: expected 1000 got 950",),
    )
    v = compute_integrity_verdict(signals)
    assert v.verdict == "fail"
    assert v.branch == "strong_metadata"

    # Evidence MUST include both the metadata_score row AND a row for
    # the reconciliation drift (which branch 1 previously dropped).
    signal_kinds = {e.signal for e in v.evidence}
    assert "metadata_score" in signal_kinds
    drift_rows = [e for e in v.evidence if e.signal.startswith("reconciliation_failed_")]
    assert len(drift_rows) == 1
    assert drift_rows[0].signal == "reconciliation_failed_period"


def test_drift_alone_branch_fires_review() -> None:
    """Reconciliation drift with no editor flag and low metadata
    → review (drift_alone). VU 7722 shape."""
    signals = DocumentIntegritySignals(
        document_id="doc_drift_alone",
        metadata_score=12,
        metadata_flags=("page_count: 6",),
        validation_failures=("reconciliation_failed_period: expected -263.89 got 236.11",),
    )
    v = compute_integrity_verdict(signals)
    assert v.verdict == "review"
    assert v.branch == "drift_alone"
    assert len(v.evidence) == 1
    assert v.evidence[0].signal == "reconciliation_failed_period"
    assert "drift_alone" in v.rationale.lower()
    assert "ocr" in v.rationale.lower()


def test_medium_corroborated_branch_fires_review() -> None:
    """metadata_score in 25-49 + reconciliation failure but NO editor
    flag → review (medium_corroborated)."""
    signals = DocumentIntegritySignals(
        document_id="doc_medium",
        metadata_score=37,
        metadata_flags=("personal_author: 'Jane Doe'",),
        validation_failures=("reconciliation_failed_deposit_total: listed 9200 vs printed 8970",),
    )
    v = compute_integrity_verdict(signals)
    assert v.verdict == "review"
    assert v.branch == "medium_corroborated"
    # Evidence includes the score, the reconciliation failure, AND
    # the non-editor metadata flag for context.
    signal_kinds = {e.signal for e in v.evidence}
    assert "metadata_score" in signal_kinds
    assert "reconciliation_failed_deposit_total" in signal_kinds
    assert "metadata_flag" in signal_kinds


def test_drift_plus_editor_branch_fires_fail() -> None:
    """Editor metadata + reconciliation drift → fail
    (drift_plus_editor). A&R KM shape."""
    signals = DocumentIntegritySignals(
        document_id="doc_arkm_lili_03",
        metadata_score=38,
        metadata_flags=("editor_detected: iText 2.1.7 by 1T3XT",),
        validation_failures=(
            "reconciliation_failed_period: expected -263.89 got 236.11",
            "reconciliation_failed_withdrawal_total: listed 31416.55 vs printed 30726.55",
        ),
    )
    v = compute_integrity_verdict(signals)
    assert v.verdict == "fail"
    assert v.branch == "drift_plus_editor"
    # Editor flag + every drift failure each surface as evidence.
    assert len(v.evidence) == 3
    assert v.evidence[0].signal == "editor_detected"
    assert "iText 2.1.7" in v.evidence[0].detail
    drift_evidence = [e for e in v.evidence if e.signal.startswith("reconciliation_failed")]
    assert len(drift_evidence) == 2
    # Rationale names the competent-fabrication phrase.
    assert "competent-fabrication" in v.rationale.lower()


def test_drift_plus_editor_fires_even_below_medium_metadata_floor() -> None:
    """The drift+editor composition is signal-driven, not score-driven.
    Even a metadata_score of 10 with just the editor flag and drift
    fires fail — the corroboration is the editor flag PRESENCE, not
    its score contribution."""
    signals = DocumentIntegritySignals(
        document_id="doc_low_score_editor",
        metadata_score=10,
        metadata_flags=("editor_detected: iText 2.1.7 by 1T3XT",),
        validation_failures=("reconciliation_failed_period: expected 12 got 0",),
    )
    v = compute_integrity_verdict(signals)
    assert v.verdict == "fail"
    assert v.branch == "drift_plus_editor"


def test_editor_metadata_alone_below_threshold_does_not_fire() -> None:
    """Editor metadata without drift, and below the strong score
    floor, falls through to clean — Preview-export false positive
    guard. Mirrors ``aegis.parser.tampering``'s 'legitimate single
    big customer merchant who opened their PDF in Preview must not
    auto-decline' principle."""
    signals = DocumentIntegritySignals(
        document_id="doc_editor_only",
        metadata_score=22,
        metadata_flags=("editor_detected: Preview (macOS)",),
        validation_failures=(),
    )
    v = compute_integrity_verdict(signals)
    assert v.verdict == "clean"
    assert v.branch == "clean"


def test_long_editor_string_truncates_to_keep_rationale_under_max_length() -> None:
    """F1a guard: a long editor signature (verbose vendor + version +
    build) must not push the drift_plus_editor rationale past the
    ``IntegrityVerdict.rationale`` ``max_length=320`` Pydantic limit.

    Pre-fix, a Pydantic ValidationError would propagate from the
    rationale builder, get swallowed by ``score_deal_inputs``' catch-
    all, and silently downgrade Track A to ``None`` (legacy fraud_score
    fallback). Post-fix the framer truncates ``short_editor`` to 60
    chars + ellipsis, capping the rationale at ~315 chars.
    """
    long_editor = (
        "editor_detected: Some Long Vendor Tool Name with "
        "Version 11.2.3.42 (Build 8a7c) by Producer GmbH"
    )
    # The metadata_flags field strips the "editor_detected: " prefix
    # at extraction time, so the post-strip length is the relevant one
    # for the rationale-builder. Sanity-check the test fixture.
    assert len(long_editor.replace("editor_detected: ", "")) > 60

    signals = DocumentIntegritySignals(
        document_id="doc_long_editor",
        metadata_score=38,
        metadata_flags=(long_editor,),
        validation_failures=("reconciliation_failed_period: expected 1000 got 950",),
    )
    v = compute_integrity_verdict(signals)
    assert v.verdict == "fail"
    assert v.branch == "drift_plus_editor"
    # The Pydantic model enforces 320; this is the contract we're
    # guarding. Re-assert it explicitly so a future relaxation of the
    # field constraint doesn't silently weaken this test.
    assert len(v.rationale) <= 320
    # The truncation must preserve the leading vendor name (the
    # actionable bit for the underwriter) and end with an ellipsis.
    assert "Some Long Vendor" in v.rationale
    assert "..." in v.rationale


# ─────────────────────────────────────────────────────────────────────
# Boundary tests — exact threshold transitions
# ─────────────────────────────────────────────────────────────────────


# Branch transitions live at metadata_score 25 / 50 — mirroring the
# parser-side `_MEDIUM_METADATA_FLOOR` / `_MEDIUM_METADATA_CEIL` (25/49)
# and `_STRONG_METADATA_FLOOR` (50) constants in `aegis.parser.tampering`.
#
# The existing branch-coverage tests above use scattered metadata_score
# values (8, 72, 12, 37, 38, 10, 22) but never exercise the exact
# transition points. Per docs/track_a_audit_2026-06-12.md F3, a future
# refactor that nudges one constant by ±1 would ship a behavioural
# regression caught only by manual triage of the historical lookback —
# which is exactly what Step 2 cutover gates on staying clean.
#
# `test_track_a_thresholds_match_parser_tampering` (below) pins the
# threshold VALUES via direct constant comparison. These tests pin the
# threshold BEHAVIOUR — they catch the asymmetric drift where Track A
# and parser thresholds move apart from each other (the value guard only
# catches symmetric drift where both move together).
_F3_BOUNDARY_DRIFT: tuple[str, ...] = ("reconciliation_failed_period: expected 1000 got 950",)
_F3_BOUNDARY_EDITOR: tuple[str, ...] = ("editor_detected: Foxit PhantomPDF 11.2",)


@pytest.mark.parametrize(
    (
        "metadata_score",
        "metadata_flags",
        "validation_failures",
        "expected_verdict",
        "expected_branch",
    ),
    [
        # Strong-metadata floor (50): exact threshold fires fail with no
        # corroboration required — that IS the strong-metadata branch's
        # contract.
        pytest.param(
            50,
            (),
            (),
            "fail",
            "strong_metadata",
            id="score=50_no_corroboration_strong_metadata_fail",
        ),
        # Strong-metadata ceiling (100): the upper bound of the score
        # range — fails the same way as the floor, no corroboration.
        pytest.param(
            100,
            (),
            (),
            "fail",
            "strong_metadata",
            id="score=100_no_corroboration_strong_metadata_fail",
        ),
        # One below strong floor (49) + drift, no editor: medium_corroborated
        # is the right branch (score in [25,49] AND drift present).
        pytest.param(
            49,
            (),
            _F3_BOUNDARY_DRIFT,
            "review",
            "medium_corroborated",
            id="score=49+drift_no_editor_medium_corroborated_review",
        ),
        # One below strong floor (49) + drift + editor: drift_plus_editor
        # wins by branch precedence (branch 2 evaluates before branch 3).
        # This is the load-bearing case for "score below the strong floor
        # but the signals still corroborate a competent fabrication".
        pytest.param(
            49,
            _F3_BOUNDARY_EDITOR,
            _F3_BOUNDARY_DRIFT,
            "fail",
            "drift_plus_editor",
            id="score=49+drift+editor_drift_plus_editor_fail",
        ),
        # Medium-metadata floor (25) + drift: exact threshold fires
        # medium_corroborated.
        pytest.param(
            25,
            (),
            _F3_BOUNDARY_DRIFT,
            "review",
            "medium_corroborated",
            id="score=25+drift_no_editor_medium_corroborated_review",
        ),
        # One below medium floor (24) + drift: falls through medium to
        # drift_alone (drift present but no metadata corroboration).
        pytest.param(
            24,
            (),
            _F3_BOUNDARY_DRIFT,
            "review",
            "drift_alone",
            id="score=24+drift_no_editor_drift_alone_review",
        ),
        # Bottom (0) + drift: drift_alone — the minimum metadata_score
        # case where drift still surfaces as a reviewable verdict.
        pytest.param(
            0,
            (),
            _F3_BOUNDARY_DRIFT,
            "review",
            "drift_alone",
            id="score=0+drift_no_editor_drift_alone_review",
        ),
    ],
)
def test_metadata_score_boundary_transitions(
    metadata_score: int,
    metadata_flags: tuple[str, ...],
    validation_failures: tuple[str, ...],
    expected_verdict: str,
    expected_branch: str,
) -> None:
    """F3 boundary guard — verdict at exact threshold transitions.

    Each parameterized case pins one transition. A future refactor that
    drifts `_STRONG_METADATA_FLOOR` or `_MEDIUM_METADATA_FLOOR`/`_CEIL`
    relative to the parser side breaks at least one case here even if
    `test_track_a_thresholds_match_parser_tampering` continues to pass
    (the value guard only catches symmetric drift; behavioural guards
    catch the asymmetric kind).
    """
    signals = DocumentIntegritySignals(
        document_id=f"doc_boundary_score_{metadata_score}",
        metadata_score=metadata_score,
        metadata_flags=metadata_flags,
        validation_failures=validation_failures,
    )
    v = compute_integrity_verdict(signals)
    assert v.verdict == expected_verdict
    assert v.branch == expected_branch


# ─────────────────────────────────────────────────────────────────────
# Structural guard — schema MUST NOT carry a decline field
# ─────────────────────────────────────────────────────────────────────


def test_verdict_has_no_decline_or_score_field() -> None:
    """Track A is additive. Schema MUST NOT carry a field that wires
    into the live decline path. Mirrors the Track B / Track C guards.
    """
    fields = set(IntegrityVerdict.model_fields)
    forbidden = {
        "decline",
        "auto_decline",
        "risk_score",
        "fraud_score",
        "score",
        "outcome",
        "tampering_confirmed",
    }
    leaked = fields & forbidden
    assert not leaked, f"Track A output must not carry decline/score fields; leaked: {leaked}"


# ─────────────────────────────────────────────────────────────────────
# Q2 mapping — verify the thresholds match the parser-side rule
# ─────────────────────────────────────────────────────────────────────


def test_track_a_thresholds_match_parser_tampering() -> None:
    """Track A's thresholds MUST stay in sync with the parser's
    existing tampering rule so the two layers can't disagree on the
    same input. The values are duplicated in code (not imported) for
    module-graph cleanliness; this test asserts the equivalence."""
    from aegis.parser import tampering as parser_tampering
    from aegis.scoring_v2.track_a import compute as track_a_compute

    assert track_a_compute._STRONG_METADATA_FLOOR == parser_tampering._STRONG_METADATA_FLOOR
    assert track_a_compute._MEDIUM_METADATA_FLOOR == parser_tampering._MEDIUM_METADATA_FLOOR
    assert track_a_compute._MEDIUM_METADATA_CEIL == parser_tampering._MEDIUM_METADATA_CEIL


# ─────────────────────────────────────────────────────────────────────
# Real-merchant acceptance: A&R KM (fail) + VU 7722 (review)
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def arkm_lili_signals() -> DocumentIntegritySignals:
    """A&R KM's Lili statement signature — VERBATIM from the live prod
    database snapshot pulled 2026-06-05 against merchant
    ``a522a8fb-0a9b-4235-87f3-c019b1bb8e1a``.

    Every Lili statement (2026-02 through 2026-05) carried this
    same signal triple. Two prod realities the test asserts on:

    1. ``metadata_score=0`` — the editor flag fires on the
       ``metadata_flags`` column but does NOT (yet) update the
       persisted ``fraud_score_breakdown.metadata`` integer. This is
       a known quirk of the current parser persistence (the editor
       detection sets the flag string but the score backfill is
       separate); Track A's drift_plus_editor branch is signal-based
       (editor flag presence + drift), not score-based, so the
       verdict is correct REGARDLESS of the persisted score quirk.
       This is the load-bearing point of the verdict design.
    2. The persisted ``all_flags`` column carries the
       ``[MATH] `` category prefix the storage layer prepends; the
       sanitizer in ``signals.py`` strips it so callers can pass
       either the raw parser format or the persisted format.
    """
    return DocumentIntegritySignals(
        document_id="arkm_lili_2026_03",
        metadata_score=0,  # ← real prod value; editor flag present but score still 0
        metadata_flags=("editor_detected: iText 2.1.7 by 1T3XT",),
        validation_failures=(
            # Persisted-form failures (with [MATH] prefix) — verbatim
            # from the live ``all_flags`` column. The signals module
            # strips the prefix before matching.
            "[MATH] reconciliation_failed_period: expected 197.45 got 364.12",
            "[MATH] reconciliation_failed_withdrawal_total: listed 32508.86 vs printed 32167.19",
            "[MATH] reconciliation_failed_intraday: 2026-03-19 p2l1: expected 8047.99 got 8214.66",
        ),
    )


@pytest.fixture
def vu_7722_signals() -> DocumentIntegritySignals:
    """VU 7722 February statement — verbatim from the live prod DB
    snapshot. One reconciliation failure (a $55 withdrawal-total
    drift), no editor metadata.

    The drift_alone branch fires; underwriter adjudicates whether
    it's OCR slop or genuine drift. The ~$55 magnitude is consistent
    with the running-balance-drift signature the operator noted
    (per docs/REMAINING_WORK.md: VU 7722 — '3 of 4 months failed
    reconciliation by $5 / $11 / $55')."""
    return DocumentIntegritySignals(
        document_id="vu7722_2026_02",
        metadata_score=0,  # ← real prod value; no editor metadata
        metadata_flags=(),
        validation_failures=(
            # Verbatim from prod.
            "[MATH] reconciliation_failed_withdrawal_total: listed 341722.93 vs printed 341667.93",
        ),
    )


def test_arkm_lili_verdict_is_fail_drift_plus_editor(
    arkm_lili_signals: DocumentIntegritySignals,
) -> None:
    """The headline acceptance case: A&R KM's iText + drift =
    competent-fabrication signature → fail.

    Notes for the dossier reader:
    - metadata_score is 0 in prod (editor flag fires but the score
      backfill is a separate parser concern). The verdict is correct
      because the drift_plus_editor branch reads the FLAG presence,
      not the score.
    - All three reconciliation failures surface as separate evidence
      rows so the underwriter sees the full pattern (period +
      withdrawal_total + intraday) and can't mistake it for a single
      reconciliation accident.
    """
    v = compute_integrity_verdict(arkm_lili_signals)
    assert v.verdict == "fail"
    assert v.branch == "drift_plus_editor"
    # Evidence cites iText specifically.
    itext_evidence = [e for e in v.evidence if "iText" in e.detail]
    assert len(itext_evidence) == 1
    # And all three reconciliation failures.
    drift_evidence = [e for e in v.evidence if e.signal.startswith("reconciliation_failed")]
    assert len(drift_evidence) == 3


def test_vu_7722_verdict_is_review_drift_alone(
    vu_7722_signals: DocumentIntegritySignals,
) -> None:
    """VU 7722's drift with no editor metadata → review (drift_alone)."""
    v = compute_integrity_verdict(vu_7722_signals)
    assert v.verdict == "review"
    assert v.branch == "drift_alone"
    # Evidence is the single reconciliation failure.
    assert len(v.evidence) == 1
    # Rationale calls out the OCR-vs-drift ambiguity.
    assert "ocr" in v.rationale.lower()


def test_arkm_vs_vu_verdicts_distinguish_via_editor_presence(
    arkm_lili_signals: DocumentIntegritySignals,
    vu_7722_signals: DocumentIntegritySignals,
) -> None:
    """The same reconciliation-failure family fires on both merchants
    — what distinguishes A&R KM (fail) from VU (review) is the editor
    metadata. This is the load-bearing reframe: drift alone could be
    OCR; drift + editor is the competent-fabrication signature."""
    arkm_v = compute_integrity_verdict(arkm_lili_signals)
    vu_v = compute_integrity_verdict(vu_7722_signals)

    # Same drift pattern fires on both.
    assert any(e.signal.startswith("reconciliation_failed") for e in arkm_v.evidence)
    assert any(e.signal.startswith("reconciliation_failed") for e in vu_v.evidence)

    # The verdicts differ because editor metadata is present on A&R only.
    assert arkm_v.verdict == "fail"
    assert vu_v.verdict == "review"
    assert arkm_v.branch == "drift_plus_editor"
    assert vu_v.branch == "drift_alone"
