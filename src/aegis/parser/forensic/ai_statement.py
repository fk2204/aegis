"""Composite AI-generated-statement detector (SHADOW MODE).

Folds four orthogonal "too clean to be a real bank export" signals into
a single 0..100 composite score:

  Signal 1 — Math perfection (weight 30)
  Signal 2 — Description character-distribution uniformity (weight 25)
  Signal 3 — Round-number amount clustering              (weight 25)
  Signal 4 — Font uniformity across rows                 (weight 20)

Composite >= 40 -> emit a ``Pattern`` with code
``ai_generated_statement``; below threshold returns ``None``.

WHY this lives separately from
``parser.patterns._ai_generated_statement_score`` (which is a 0..100
text-style heuristic computed inside ``analyze_patterns``): the existing
scorer reads only description-text properties (uppercase / digit-noise /
round-share). This composite mixes those style signals with the math-
perfection + font-uniformity evidence from OTHER layers (validation
results, the forensic font-consistency analyzer). Keeping it in
``forensic/`` mirrors the cross-layer composition pattern used by
``forensic.creator_fingerprint`` (which fuses metadata creator strings
with extracted bank name).

SHADOW DISCIPLINE — CLAUDE.md "Decision-boundary changes — shadow-first":
- The Pattern emitted by this module MUST land on
  ``PatternAnalysis.shadow_patterns``, NEVER on ``patterns``. The
  pipeline wiring is what enforces that placement (the detector is a
  pure function; the caller decides where the result goes).
- ``FRAUD_WEIGHTS["shadow_ai_generated_statement"]`` is pinned to 0.0
  so this signal cannot contribute to ``fraud_score`` even if a future
  refactor accidentally folds shadow patterns into the live sum.
- This detector does NOT touch ``parse_status``, hard-decline reasons,
  or any other live decision branch. Pipeline operators flip to live
  via a config change AFTER corpus + shadow-audit validation.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from aegis.parser.forensic.font_consistency import FontConsistencyResult
from aegis.parser.models import ClassifiedTransaction
from aegis.parser.patterns import Pattern

# ─────────────────────────────────────────────────────────────────────
# Weights — must sum to 100. The composite score is a weighted sum of
# binary-fired (or scaled) per-signal contributions.
# ─────────────────────────────────────────────────────────────────────

_SIGNAL_1_WEIGHT: Final[int] = 30
_SIGNAL_2_WEIGHT: Final[int] = 25
_SIGNAL_3_WEIGHT: Final[int] = 25
_SIGNAL_4_WEIGHT: Final[int] = 20

# Composite threshold. Below this, return None — no Pattern emitted, no
# shadow flag surfaced. Tunable post-corpus-validation; starting value
# picked so two strong signals (any two of the four) clear the bar
# without one signal alone being able to fire.
_COMPOSITE_THRESHOLD: Final[int] = 40

# Min-active-signals guard (added 2026-06-27 after shadow audit).
#
# WHY: 14-day shadow audit on prod showed 17 fires with a 65% false-
# positive rate (11 of 17 on docs at parse_status="proceed"). All fires
# were driven by ONLY Signal 1 (math_perfection=30) + Signal 3
# (round_cluster=12-25). Signals 2 (description_uniformity) and 4
# (font_uniformity) were 0 on every fire — the composite was operating
# as a 2-signal detector but the 40 threshold was calibrated for the
# 4-signal land. Real low-volume merchants with clean books + round
# rents trip Signals 1+3 alone and get false-flagged.
#
# This guard requires ≥2 of the 4 component signals to be NON-ZERO
# before the detector can fire, regardless of composite total. Single-
# signal high-score cases (e.g., math_perfection=30 alone) exit early
# even when the threshold check would otherwise pass.
#
# The guard runs BEFORE the composite threshold check — early-exit
# semantics make the intent explicit. Per CLAUDE.md "Decision-boundary
# changes — shadow-first": this calibration ships live (not shadow-
# first) because the shadow data ALREADY validated the false-positive
# rate. Re-shadowing a recalibration of an already-shadow detector
# would be redundant.
_MIN_ACTIVE_SIGNALS: Final[int] = 2

# ─────────────────────────────────────────────────────────────────────
# Signal 2 — description entropy threshold.
#
# WHY Shannon entropy of CHARACTER distributions:
# Real OCR / bank-export descriptions mix case, abbreviations, occasional
# noise (trace ids, confirmation numbers, masked digits). The character
# histogram across the whole description corpus is therefore broad.
# AI-generated statements tend to reuse the SAME template phrasing
# ("ACH DEPOSIT MERCHANT SALES", "ACH DEPOSIT MERCHANT SALES", ...) so
# the histogram collapses onto a small alphabet.
#
# WHY 4.0 bits/char as the threshold (tuned empirically):
# Shannon entropy of the printable-ASCII alphabet is ~6.5 bits/char
# uniform. EMPIRICAL CORPUS MEASUREMENT (run during this commit's test
# authoring):
#   * 12 copies of "ACH DEPOSIT MERCHANT SALES" -> 3.74 bits/char
#     (15 distinct chars across 300 total)
#   * realistic 8-row varied bank-description sample
#     (POS / ACH / WIRE / ZELLE / ATM / DEBIT / PAYROLL / FEE with
#     trace ids and merchant suffixes) -> 4.68 bits/char
#     (37 distinct chars)
# 4.0 sits between them so the pathological-uniformity case fires and
# the realistic varied corpus does not. The signal is intentionally a
# WEAK discriminator on its own (char-level entropy is bounded by
# alphabet size); the composite is designed to need a second
# corroborating signal before clearing the 40 threshold.
#
# Tunable post-corpus-validation — the operator should walk a sample
# of real low-volume merchants (one Square deposit/day style) AND a
# sample of fake-generator output before tightening.
# ─────────────────────────────────────────────────────────────────────

_DESCRIPTION_ENTROPY_THRESHOLD: Final[float] = 4.0

# ─────────────────────────────────────────────────────────────────────
# Signal 3 — round-number scaling.
#
# WHY a scaled response rather than binary fire/no-fire:
# Statements with mostly bill payments + payroll deposits naturally cluster
# around round amounts (rent = $X,XXX.00, salary = $Y,YYY.00). A binary
# threshold at 40% would fire on plausible legitimate cases. The two-band
# response keeps the lower band SILENT (fraction < 20% gets 0 score) and
# the upper band CAPPED (fraction >= 40% gets the full 25), with linear
# interpolation in between to reward stronger evidence without exploding
# false-positive rate on the middle ground.
#
# Concrete math:
#   fraction < 0.20  ->  score = 0
#   0.20 <= fraction < 0.40  ->  score = 25 * (fraction / 0.40)
#   fraction >= 0.40  ->  score = 25
#
# Note: at fraction = 0.20 the linear formula evaluates to 12.5 (rounded
# to 13). The boundary is INCLUSIVE on 0.20 — so 20% earns 13 of the 25
# available points, not 0. That matches the spec ("below that, score 0")
# being interpreted as "STRICTLY below 0.20 scores 0". The branch test
# below covers this.
# ─────────────────────────────────────────────────────────────────────

_ROUND_AMOUNT_LOW_BAND: Final[Decimal] = Decimal("0.20")
_ROUND_AMOUNT_HIGH_BAND: Final[Decimal] = Decimal("0.40")


# ─────────────────────────────────────────────────────────────────────
# Detector entry point
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _SignalContributions:
    """Per-signal contribution breakdown — kept on the side for the
    ``detail`` string and for the per-signal isolation tests."""

    signal_1_math_perfection: int
    signal_2_description_uniformity: int
    signal_3_round_number_cluster: int
    signal_4_font_uniformity: int

    @property
    def composite(self) -> int:
        return (
            self.signal_1_math_perfection
            + self.signal_2_description_uniformity
            + self.signal_3_round_number_cluster
            + self.signal_4_font_uniformity
        )


def detect_ai_generated_statement(
    transactions: list[ClassifiedTransaction],
    math_flags: list[str],
    font_result: FontConsistencyResult | None,
    period_flags: list[str],
) -> Pattern | None:
    """Return a SHADOW ``Pattern`` when composite >= 40, else None.

    Arguments:
        transactions: every classified row on the statement under
            analysis. Empty list -> no signal possible -> None.
        math_flags: validation-layer math failures. Any entry starting
            with ``reconciliation_failed_`` (or any non-empty entry)
            disqualifies Signal 1 — real statements have small
            reconciliation imperfections AI fakes don't.
        font_result: the document-level rollup from
            ``forensic.font_consistency.analyze``. Passed in by the
            pipeline so this detector does NOT re-open the PDF. None
            when font analysis didn't run (vision-only mode, image-only
            PDFs) — Signal 4 then scores 0 (absence of evidence isn't
            evidence of fraud).
        period_flags: any aggregate-layer flag that documents a
            period-scope reconciliation problem. Future-compat hook —
            today the validation layer does not emit ``period_*``
            failures, but the convention reserves the prefix for
            aggregate/period-level checks the pipeline may surface
            later. Any non-empty entry disqualifies Signal 1 on the
            same logic as ``math_flags``.

    The returned ``Pattern`` carries:
        code     = "ai_generated_statement"
        severity = the 0..100 composite score
        detail   = "score=N/100 signals=[s1=A, s2=B, s3=C, s4=D]"
        source_ids = []  # composite — no single contributing txn id

    Source-ids is empty by design: the composite is a document-level
    judgment, not a per-row judgment. Per-row drilldown is available
    through the other detectors in ``patterns.py`` that contribute
    pieces of the same signal (``_round_number_deposits``,
    ``_ai_generated_statement_score``).
    """
    if not transactions:
        return None

    contributions = _SignalContributions(
        signal_1_math_perfection=_signal_1_math_perfection(transactions, math_flags, period_flags),
        signal_2_description_uniformity=_signal_2_description_uniformity(transactions),
        signal_3_round_number_cluster=_signal_3_round_number_cluster(transactions),
        signal_4_font_uniformity=_signal_4_font_uniformity(font_result),
    )

    # Min-active-signals guard — early-exit BEFORE the composite check.
    # Single-signal high-score cases (e.g. math_perfection=30 alone with
    # no other signals) exit here even if their total would otherwise
    # clear the threshold. See ``_MIN_ACTIVE_SIGNALS`` constant block
    # for the shadow-audit calibration rationale.
    active_signals = sum(
        1
        for v in (
            contributions.signal_1_math_perfection,
            contributions.signal_2_description_uniformity,
            contributions.signal_3_round_number_cluster,
            contributions.signal_4_font_uniformity,
        )
        if v > 0
    )
    if active_signals < _MIN_ACTIVE_SIGNALS:
        return None

    composite = contributions.composite
    if composite < _COMPOSITE_THRESHOLD:
        return None

    detail = (
        f"score={composite}/100 signals=["
        f"math_perfection={contributions.signal_1_math_perfection},"
        f"description_uniformity={contributions.signal_2_description_uniformity},"
        f"round_cluster={contributions.signal_3_round_number_cluster},"
        f"font_uniformity={contributions.signal_4_font_uniformity}]"
    )
    return Pattern(
        code="ai_generated_statement",
        severity=composite,
        detail=detail,
        source_ids=[],
    )


# ─────────────────────────────────────────────────────────────────────
# Per-signal scorers
# ─────────────────────────────────────────────────────────────────────


def _signal_1_math_perfection(
    transactions: list[ClassifiedTransaction],
    math_flags: list[str],
    period_flags: list[str],
) -> int:
    """30 points iff zero math failures AND zero period failures AND no
    transaction-level running-balance disagreement.

    The running-balance secondary check looks for rows whose stored
    ``running_balance`` is inconsistent with the prior row's running
    balance + this row's amount (within a 1-cent tolerance). The
    validation layer covers this with the
    ``reconciliation_failed_intraday`` flag, but the explicit secondary
    pass here defends against a future pipeline change that omits the
    intraday check from ``math_flags`` — Signal 1 wants math
    PERFECTION, not just "the gates we happen to run agree."
    """
    if math_flags:
        return 0
    if period_flags:
        return 0
    if _running_balance_disagrees(transactions):
        return 0
    return _SIGNAL_1_WEIGHT


def _running_balance_disagrees(
    transactions: list[ClassifiedTransaction],
) -> bool:
    """True when any consecutive pair of rows with running_balance set
    disagrees by more than $0.01 from `prev + curr.amount`.

    Rows without running_balance are SKIPPED — many statements omit the
    column on summary rows or wires. The check needs BOTH neighbors to
    have the value to compare.
    """
    sorted_txns = sorted(transactions, key=lambda t: (t.posted_date, t.source_page, t.source_line))
    prev_balance: Decimal | None = None
    tolerance = Decimal("0.01")
    for txn in sorted_txns:
        if txn.running_balance is None:
            prev_balance = None
            continue
        if prev_balance is not None:
            expected = prev_balance + txn.amount
            if abs(expected - txn.running_balance) > tolerance:
                return True
        prev_balance = txn.running_balance
    return False


def _signal_2_description_uniformity(
    transactions: list[ClassifiedTransaction],
) -> int:
    """25 points when the character-level Shannon entropy of all
    descriptions concatenated falls below ``_DESCRIPTION_ENTROPY_THRESHOLD``.

    Returns 0 when the corpus is empty (no descriptions) — defensive,
    not a fraud signal.
    """
    corpus = "".join(txn.description for txn in transactions if txn.description)
    if not corpus:
        return 0
    entropy = _shannon_entropy(corpus)
    if entropy < _DESCRIPTION_ENTROPY_THRESHOLD:
        return _SIGNAL_2_WEIGHT
    return 0


def _shannon_entropy(s: str) -> float:
    """Shannon entropy of the character distribution in ``s``, in bits.

    H = -Σ p_i * log2(p_i) over distinct chars i. Returns 0.0 on empty
    input (avoiding log2(0) / by-zero); 0.0 also when all chars are
    identical (single bucket -> p=1 -> contribution = 0).
    """
    if not s:
        return 0.0
    counts = Counter(s)
    total = len(s)
    entropy = 0.0
    for count in counts.values():
        probability = count / total
        entropy -= probability * math.log2(probability)
    return entropy


def _signal_3_round_number_cluster(
    transactions: list[ClassifiedTransaction],
) -> int:
    """0..25 scaled to the fraction of transactions whose amount is a
    whole-dollar value (no cents).

    Scaling is the two-band response documented at the constants block
    above — strictly-below 20% scores 0, 40% or higher scores the full
    weight, linear in between. Uses Decimal arithmetic throughout —
    fraction comparisons are Decimal-vs-Decimal so float coercion never
    enters the computation.
    """
    total = len(transactions)
    if total == 0:
        return 0
    round_count = sum(1 for t in transactions if t.amount % Decimal("1.00") == 0)
    fraction = Decimal(round_count) / Decimal(total)
    if fraction < _ROUND_AMOUNT_LOW_BAND:
        return 0
    if fraction >= _ROUND_AMOUNT_HIGH_BAND:
        return _SIGNAL_3_WEIGHT
    # Linear interpolation across [low_band, high_band).
    # score = weight * (fraction / high_band). Quantize to int so
    # severity stays an integer (Pattern.severity is typed int).
    scaled = (Decimal(_SIGNAL_3_WEIGHT) * fraction) / _ROUND_AMOUNT_HIGH_BAND
    return int(scaled.quantize(Decimal("1")))


def _signal_4_font_uniformity(
    font_result: FontConsistencyResult | None,
) -> int:
    """20 points when the document-wide font profile is "pixel-perfect
    identical" — proxy: font_consistency analyzer ran, produced a modal
    font, and did NOT flag any page as inconsistent.

    WHY this proxy:
    ``FontConsistencyResult`` does not expose per-row font tuples
    (the analyzer collapses everything to page-level inconsistency
    booleans + a modal-font string). The strongest "every row uses the
    same font family + size" assertion we can make from the available
    rollup is:
      * The analyzer ran successfully (``modal_font != ""`` — a
        non-empty modal means it found readable text spans and computed
        a per-page modal at least once).
      * No page was flagged as inconsistent
        (``affected_page_count == 0`` AND ``inconsistency_detected
        is False``). A page that mixes fonts on its transaction rows
        would fail at least one of the three analyzer branches.

    A real bank export typically shows multiple font families across
    headers / summary blocks / transaction rows, so the analyzer's
    modal-font logic + per-page family-mismatch detection identifies
    most variance. The proxy is INTENTIONALLY conservative — it returns
    0 for any document where the analyzer didn't run (image-only PDFs
    in vision mode -> font_result is None; PDFs with no extractable
    text -> modal_font is ""). Absence of evidence is not evidence of
    fraud; Signal 4 simply doesn't contribute on those documents.
    """
    if font_result is None:
        return 0
    if font_result.inconsistency_detected:
        return 0
    if font_result.affected_page_count > 0:
        return 0
    if not font_result.modal_font:
        # Null-result path — analyzer fell back without running
        # successfully. Don't penalize.
        return 0
    return _SIGNAL_4_WEIGHT


__all__ = ["detect_ai_generated_statement"]
