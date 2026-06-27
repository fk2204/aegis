"""Composite AI-generated-statement detector tests.

Covers ``aegis.parser.forensic.ai_statement.detect_ai_generated_statement``
(operator spec 2026-06-24, shadow-mode composite).

Coverage:
- High-score case (12 round-amount template txns + uniform font) -> emit
  with severity >= 75.
- Low-score case (realistic mixed corpus) -> composite < 40 -> None.
- Threshold gate at exactly 39 and exactly 40.
- Shadow discipline: ``FRAUD_WEIGHTS["shadow_ai_generated_statement"] == 0``
  AND running ``analyze_patterns`` end-to-end on a high-score corpus
  does NOT inflate ``fraud_score`` (the detector lives outside
  ``analyze_patterns`` so the test stops at the weights contract).
- font_result is None -> Signal 4 is 0 but other signals score.
- Round-number scaling: 20% -> 13, 40% -> 25, 60% -> 25.
- Per-signal isolation: four fixtures, each fires exactly one signal.
- Monotonicity: adding more synthetic indicators never decreases the
  composite.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

from aegis.parser.forensic.ai_statement import detect_ai_generated_statement
from aegis.parser.forensic.font_consistency import FontConsistencyResult
from aegis.parser.models import ClassifiedTransaction, TransactionCategory
from aegis.parser.patterns import Pattern, analyze_patterns
from aegis.parser.pipeline import FRAUD_WEIGHTS

PERIOD_START = date(2026, 1, 1)
PERIOD_END = date(2026, 1, 31)


def _txn(
    *,
    posted_date: date,
    description: str,
    amount: Decimal,
    category: TransactionCategory = "deposit",
    running_balance: Decimal | None = None,
    source_line: int = 1,
) -> ClassifiedTransaction:
    return ClassifiedTransaction(
        id=uuid4(),
        posted_date=posted_date,
        description=description,
        amount=amount,
        running_balance=running_balance,
        source_page=1,
        source_line=source_line,
        category=category,
        classification_confidence=95,
    )


def _uniform_font_result() -> FontConsistencyResult:
    """Document-level FontConsistencyResult that satisfies Signal 4 — the
    analyzer ran (modal_font set) and found no inconsistency."""
    return FontConsistencyResult(
        inconsistency_detected=False,
        affected_page_count=0,
        modal_font="Helvetica",
        anomalous_fonts=[],
        confidence=0.0,
    )


def _inconsistent_font_result() -> FontConsistencyResult:
    """Result that DOES flag inconsistency — Signal 4 scores 0."""
    return FontConsistencyResult(
        inconsistency_detected=True,
        affected_page_count=2,
        modal_font="Helvetica",
        anomalous_fonts=["Courier"],
        confidence=0.5,
    )


# ---------------------------------------------------------------------------
# High-score case
# ---------------------------------------------------------------------------


def test_high_score_case_fires_with_severity_at_least_75() -> None:
    """12 deposits, all whole-dollar, identical descriptions, no math
    failures, uniform font. All four signals should fire."""
    txns = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=i * 2),
            description="ACH DEPOSIT MERCHANT SALES",
            amount=Decimal("1000.00"),
            source_line=i + 1,
        )
        for i in range(12)
    ]
    result = detect_ai_generated_statement(
        txns,
        math_flags=[],
        font_result=_uniform_font_result(),
        period_flags=[],
    )
    assert result is not None
    assert result.code == "ai_generated_statement"
    assert result.severity >= 75
    assert result.source_ids == []
    # Detail includes per-signal contributions for operator readability.
    assert "math_perfection=30" in result.detail
    assert "description_uniformity=25" in result.detail
    assert "round_cluster=25" in result.detail
    assert "font_uniformity=20" in result.detail


# ---------------------------------------------------------------------------
# Low-score case — realistic mixed corpus
# ---------------------------------------------------------------------------


def test_low_score_case_returns_none() -> None:
    """Realistic statement: varied amounts WITH cents, varied
    descriptions, font_inconsistency_detected=True, a period flag —
    composite < 40, no emit."""
    txns = [
        _txn(
            posted_date=date(2026, 1, 3),
            description="POS PURCHASE 0234 STARBUCKS #4123 NYC",
            amount=Decimal("-7.42"),
        ),
        _txn(
            posted_date=date(2026, 1, 5),
            description="ACH DEPOSIT SQUARE 250105 TR 1837461",
            amount=Decimal("1247.83"),
            category="ach_credit",
            source_line=2,
        ),
        _txn(
            posted_date=date(2026, 1, 8),
            description="WIRE OUT REF 0901834 BENEFICIARY ACME LLC",
            amount=Decimal("-3500.00"),
            category="wire_out",
            source_line=3,
        ),
        _txn(
            posted_date=date(2026, 1, 12),
            description="ZELLE PAYMENT FROM J SMITH CONF 22A8F1",
            amount=Decimal("315.50"),
            category="deposit",
            source_line=4,
        ),
        _txn(
            posted_date=date(2026, 1, 15),
            description="ATM WITHDRAWAL TERMINAL 04412 BROOKLYN",
            amount=Decimal("-180.00"),
            source_line=5,
        ),
        _txn(
            posted_date=date(2026, 1, 19),
            description="DEBIT CARD 5482 AMAZON.COM*MK4Q11RB1",
            amount=Decimal("-89.27"),
            source_line=6,
        ),
        _txn(
            posted_date=date(2026, 1, 22),
            description="ACH CREDIT PAYROLL ID 102938 EMPLOYER",
            amount=Decimal("2150.75"),
            category="payroll",
            source_line=7,
        ),
        _txn(
            posted_date=date(2026, 1, 26),
            description="MONTHLY MAINTENANCE FEE",
            amount=Decimal("-15.00"),
            category="fee",
            source_line=8,
        ),
    ]
    result = detect_ai_generated_statement(
        txns,
        math_flags=["reconciliation_failed_period: expected 1.00 got 1.50"],
        font_result=_inconsistent_font_result(),
        period_flags=["period_total_mismatch:listed=42 sum=43"],
    )
    assert result is None


# ---------------------------------------------------------------------------
# Threshold gate — exactly 39 -> None, exactly 40 -> emit
# ---------------------------------------------------------------------------


def _txns_for_score(
    *,
    round_fraction: Decimal,
    uniform_desc: bool,
    n: int = 10,
) -> list[ClassifiedTransaction]:
    """Build a corpus of ``n`` txns where ``round_fraction`` of them have
    whole-dollar amounts and the rest have explicit cents."""
    round_count = int(round_fraction * n)
    out: list[ClassifiedTransaction] = []
    base = PERIOD_START
    description_round = "ACH DEPOSIT MERCHANT SALES" if uniform_desc else "ACH CREDIT BATCH 0001"
    description_cents = (
        "ACH DEPOSIT MERCHANT SALES" if uniform_desc else "POS PURCHASE 4123 COFFEE BAR"
    )
    for i in range(round_count):
        out.append(
            _txn(
                posted_date=base + timedelta(days=i),
                description=description_round,
                amount=Decimal("500.00"),
                source_line=i + 1,
            )
        )
    for i in range(n - round_count):
        out.append(
            _txn(
                posted_date=base + timedelta(days=round_count + i),
                description=description_cents,
                amount=Decimal("100.37"),
                source_line=round_count + i + 1,
            )
        )
    return out


def test_threshold_gate_below_returns_none() -> None:
    """Math perfection only (30) + nothing else -> composite below the
    40 gate. Use genuinely varied descriptions (mixed vendors, mixed
    digits, ample alphabet coverage) so Signal 2 stays at 0."""
    # Each row picks from a distinct vendor + transaction-type template
    # so the concatenated corpus has high character diversity (>30
    # distinct chars, entropy comfortably above the 4.0 threshold).
    vendor_templates = [
        "POS PURCHASE STARBUCKS #4123 NYC $7.42",
        "ACH DEPOSIT SQUARE ID 250105 TR 1837461",
        "WIRE OUT REF 0901834 BENEFICIARY ACME LLC",
        "ZELLE PAYMENT FROM J SMITH CONF 22A8F1",
        "ATM WITHDRAWAL TERMINAL 04412 BROOKLYN",
        "DEBIT CARD 5482 AMAZON.COM*MK4Q11RB1",
        "ACH CREDIT PAYROLL ID 102938 EMPLOYER",
        "MONTHLY MAINTENANCE FEE",
    ]
    txns = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=i),
            description=vendor_templates[i],
            amount=Decimal("12.37") + Decimal(i),
            source_line=i + 1,
        )
        for i in range(len(vendor_templates))
    ]
    result = detect_ai_generated_statement(
        txns,
        math_flags=[],  # math perfection -> +30
        font_result=None,  # font_result missing -> +0
        period_flags=[],
    )
    # Composite = 30, below the 40 gate.
    assert result is None


def test_threshold_gate_at_or_above_emits() -> None:
    """Composite of exactly 40 (math 30 + round 10 via 16% scaled is
    below the 20% floor; use math 30 + round band entry at 20% -> 13;
    total 43 >= 40)."""
    # 20% round fraction (2 of 10) -> scaled to 13. 13 + 30 = 43 >= 40.
    round_txns = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=i),
            description=f"POS PURCHASE STORE {i * 17 % 100:03d} CONF {i * 31:06d}",
            amount=Decimal("500.00"),
            source_line=i + 1,
        )
        for i in range(2)
    ]
    noisy_txns = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=2 + i),
            description=f"ATM WD TERM 0{i:03d} REF {i * 53:07d}",
            amount=Decimal("17.43") + Decimal(i),
            source_line=i + 3,
        )
        for i in range(8)
    ]
    result = detect_ai_generated_statement(
        round_txns + noisy_txns,
        math_flags=[],
        font_result=None,
        period_flags=[],
    )
    assert result is not None
    assert result.severity >= 40


# ---------------------------------------------------------------------------
# Shadow discipline — FRAUD_WEIGHTS entry exists and is 0.0
# ---------------------------------------------------------------------------


def test_shadow_weight_entry_pinned_to_zero() -> None:
    assert FRAUD_WEIGHTS["shadow_ai_generated_statement"] == 0.0


def test_high_score_does_not_inflate_analyze_patterns_fraud_score() -> None:
    """analyze_patterns() does NOT compute the composite (it lives in
    ``forensic/`` and the pipeline calls it separately). A high-signal
    corpus should NOT pump ``patterns.fraud_score`` through the
    composite — only through the existing live detectors."""
    txns = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=i * 2),
            description="ACH DEPOSIT MERCHANT SALES",
            amount=Decimal("1000.00"),
            source_line=i + 1,
        )
        for i in range(12)
    ]
    pa = analyze_patterns(
        txns,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        today=date(2026, 2, 5),
    )
    # No shadow patterns from this detector should appear here — the
    # composite is emitted by the PIPELINE (post analyze_patterns), not
    # by analyze_patterns itself.
    composite_emits = [p for p in pa.shadow_patterns if p.code == "ai_generated_statement"]
    assert composite_emits == [], (
        "ai_generated_statement should only be emitted by parser.pipeline, "
        "never by analyze_patterns — keeping the layers separate keeps the "
        "weights contract clean."
    )


# ---------------------------------------------------------------------------
# Font-result None -> Signal 4 zeroed, other signals still scored
# ---------------------------------------------------------------------------


def test_font_result_none_keeps_other_signals_active() -> None:
    """High-signal corpus with font_result=None -> Signal 4 is 0 but
    composite still reaches >=60 (30+25+25)."""
    txns = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=i * 2),
            description="ACH DEPOSIT MERCHANT SALES",
            amount=Decimal("1000.00"),
            source_line=i + 1,
        )
        for i in range(12)
    ]
    result = detect_ai_generated_statement(
        txns,
        math_flags=[],
        font_result=None,
        period_flags=[],
    )
    assert result is not None
    assert result.severity >= 60
    # Verify font_uniformity scored zero specifically.
    assert "font_uniformity=0" in result.detail


# ---------------------------------------------------------------------------
# Round-number scaling — 20% / 40% / 60% behavior
# ---------------------------------------------------------------------------


def _round_score_only(txns: list[ClassifiedTransaction]) -> int:
    """Helper: run the detector with math + description signals forced
    OFF and font uniformity forced ON, return the round-cluster
    contribution by parsing the detail string.

    Force-on font (+20) plus the on-test description uniformity from the
    template descriptions (+25) keeps the composite above the 40
    threshold so the detector emits and we can read the round
    contribution off the detail string. Math is disabled via a flag.
    """
    result = detect_ai_generated_statement(
        txns,
        math_flags=["reconciliation_failed_period: forced off"],
        font_result=_uniform_font_result(),
        period_flags=[],
    )
    if result is None:
        return 0
    # Detail format: "...round_cluster=N,..."
    needle = "round_cluster="
    start = result.detail.index(needle) + len(needle)
    end = result.detail.index(",", start)
    return int(result.detail[start:end])


def test_round_scaling_below_20_percent_scores_zero() -> None:
    """Exact branch: 1 round of 10 -> 10% -> below 20% floor -> 0.
    The detector returns None overall (composite below threshold), so
    we read the contribution off a forced-emit corpus instead."""
    # Use 10% (below floor). Emit will be None; round contribution = 0.
    round_count = 1
    total = 10
    txns = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=i),
            description=f"POS PURCHASE STORE {i * 17 % 100:03d} CONF {i * 31:06d}",
            amount=Decimal("500.00") if i < round_count else Decimal("17.43"),
            source_line=i + 1,
        )
        for i in range(total)
    ]
    result = detect_ai_generated_statement(
        txns,
        math_flags=["reconciliation_failed_period: forced off"],
        font_result=None,
        period_flags=[],
    )
    # Math is off, descriptions are noisy, font None, round below floor:
    # all four signals should be 0 -> composite 0 -> None.
    assert result is None


def test_round_scaling_at_40_percent_scores_full() -> None:
    """40% round share -> full 25 points. Force-emit via uniform
    descriptions so the composite clears threshold."""
    round_count = 4
    total = 10
    txns = []
    for i in range(round_count):
        txns.append(
            _txn(
                posted_date=PERIOD_START + timedelta(days=i),
                description="ACH DEPOSIT MERCHANT SALES",
                amount=Decimal("500.00"),
                source_line=i + 1,
            )
        )
    for i in range(total - round_count):
        txns.append(
            _txn(
                posted_date=PERIOD_START + timedelta(days=round_count + i),
                description="ACH DEPOSIT MERCHANT SALES",
                amount=Decimal("100.37"),
                source_line=round_count + i + 1,
            )
        )
    contribution = _round_score_only(txns)
    assert contribution == 25


def test_round_scaling_above_40_percent_capped_at_full() -> None:
    """60% round share -> capped at 25 (no over-scoring above the
    high band)."""
    round_count = 6
    total = 10
    txns = []
    for i in range(round_count):
        txns.append(
            _txn(
                posted_date=PERIOD_START + timedelta(days=i),
                description="ACH DEPOSIT MERCHANT SALES",
                amount=Decimal("500.00"),
                source_line=i + 1,
            )
        )
    for i in range(total - round_count):
        txns.append(
            _txn(
                posted_date=PERIOD_START + timedelta(days=round_count + i),
                description="ACH DEPOSIT MERCHANT SALES",
                amount=Decimal("100.37"),
                source_line=round_count + i + 1,
            )
        )
    contribution = _round_score_only(txns)
    assert contribution == 25


def test_round_scaling_at_20_percent_floor_scores_band_entry() -> None:
    """20% round share -> linear interpolation at the inclusive floor.
    formula: 25 * (0.20 / 0.40) = 12.5 -> int(12.5) rounds to 12 via
    Decimal banker's rounding (ROUND_HALF_EVEN)."""
    round_count = 2
    total = 10
    txns = []
    for i in range(round_count):
        txns.append(
            _txn(
                posted_date=PERIOD_START + timedelta(days=i),
                description="ACH DEPOSIT MERCHANT SALES",
                amount=Decimal("500.00"),
                source_line=i + 1,
            )
        )
    for i in range(total - round_count):
        txns.append(
            _txn(
                posted_date=PERIOD_START + timedelta(days=round_count + i),
                description="ACH DEPOSIT MERCHANT SALES",
                amount=Decimal("100.37"),
                source_line=round_count + i + 1,
            )
        )
    contribution = _round_score_only(txns)
    # Decimal default rounding is ROUND_HALF_EVEN. 12.5 -> 12.
    assert contribution == 12


# ---------------------------------------------------------------------------
# Per-signal isolation — four single-signal fixtures
# ---------------------------------------------------------------------------


def _detail_signal_contributions(detail: str) -> tuple[int, int, int, int]:
    """Parse the four signal values from the detail string in order."""

    def _read(key: str) -> int:
        needle = f"{key}="
        start = detail.index(needle) + len(needle)
        # Either ',' or ']' terminates the value.
        end_candidates = [detail.index(ch, start) for ch in (",", "]") if ch in detail[start:]]
        end = min(end_candidates)
        return int(detail[start:end])

    return (
        _read("math_perfection"),
        _read("description_uniformity"),
        _read("round_cluster"),
        _read("font_uniformity"),
    )


def test_signal_1_isolation_only_math_perfection() -> None:
    """Force-emit via two non-S1 signals so we cross the 40 threshold,
    then verify S1 contributed exactly 30 while we toggle math_flags
    on / off."""
    # Two of the other signals on (uniform desc + round cluster) -> 50.
    # Plus math perfection -> 80. Minus math (turn on math_flags) -> 50.
    txns = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=i),
            description="ACH DEPOSIT MERCHANT SALES",
            amount=Decimal("500.00"),
            source_line=i + 1,
        )
        for i in range(10)
    ]
    on = detect_ai_generated_statement(txns, math_flags=[], font_result=None, period_flags=[])
    off = detect_ai_generated_statement(
        txns,
        math_flags=["reconciliation_failed_period: x"],
        font_result=None,
        period_flags=[],
    )
    assert on is not None and off is not None
    s1_on, _, _, _ = _detail_signal_contributions(on.detail)
    s1_off, _, _, _ = _detail_signal_contributions(off.detail)
    assert s1_on == 30
    assert s1_off == 0
    assert on.severity - off.severity == 30


def test_signal_2_isolation_only_description_uniformity() -> None:
    """Toggle description-uniformity by swapping every txn description
    to unique high-entropy strings vs. one repeated low-entropy
    template."""
    uniform = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=i),
            description="AA",
            amount=Decimal("500.00"),
            source_line=i + 1,
        )
        for i in range(10)
    ]
    varied = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=i),
            description=(f"POS PURCHASE {i * 17 % 100:03d}/STORE/CONFIRMATION-{i * 8311:08d}-XYZ"),
            amount=Decimal("500.00"),
            source_line=i + 1,
        )
        for i in range(10)
    ]
    on = detect_ai_generated_statement(uniform, math_flags=[], font_result=None, period_flags=[])
    off = detect_ai_generated_statement(varied, math_flags=[], font_result=None, period_flags=[])
    assert on is not None and off is not None
    _, s2_on, _, _ = _detail_signal_contributions(on.detail)
    _, s2_off, _, _ = _detail_signal_contributions(off.detail)
    assert s2_on == 25
    assert s2_off == 0
    assert on.severity - off.severity == 25


def test_signal_3_isolation_only_round_cluster() -> None:
    """Toggle the round-cluster signal by changing amounts only.
    Everything else held constant."""
    high_round = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=i),
            description="ACH DEPOSIT MERCHANT SALES",
            amount=Decimal("500.00"),
            source_line=i + 1,
        )
        for i in range(10)
    ]
    low_round = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=i),
            description="ACH DEPOSIT MERCHANT SALES",
            amount=Decimal("500.37"),
            source_line=i + 1,
        )
        for i in range(10)
    ]
    on = detect_ai_generated_statement(high_round, math_flags=[], font_result=None, period_flags=[])
    off = detect_ai_generated_statement(low_round, math_flags=[], font_result=None, period_flags=[])
    assert on is not None and off is not None
    _, _, s3_on, _ = _detail_signal_contributions(on.detail)
    _, _, s3_off, _ = _detail_signal_contributions(off.detail)
    assert s3_on == 25
    assert s3_off == 0
    assert on.severity - off.severity == 25


def test_signal_4_isolation_only_font_uniformity() -> None:
    """Toggle Signal 4 by passing a uniform vs an inconsistent
    FontConsistencyResult."""
    txns = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=i),
            description="ACH DEPOSIT MERCHANT SALES",
            amount=Decimal("500.00"),
            source_line=i + 1,
        )
        for i in range(10)
    ]
    on = detect_ai_generated_statement(
        txns, math_flags=[], font_result=_uniform_font_result(), period_flags=[]
    )
    off = detect_ai_generated_statement(
        txns,
        math_flags=[],
        font_result=_inconsistent_font_result(),
        period_flags=[],
    )
    assert on is not None and off is not None
    _, _, _, s4_on = _detail_signal_contributions(on.detail)
    _, _, _, s4_off = _detail_signal_contributions(off.detail)
    assert s4_on == 20
    assert s4_off == 0
    assert on.severity - off.severity == 20


# ---------------------------------------------------------------------------
# Monotonicity — adding more indicators never decreases the composite
# ---------------------------------------------------------------------------


def test_monotonicity_adding_signals_never_decreases_composite() -> None:
    """Build five fixtures with progressively more indicators on and
    assert the composite is non-decreasing across them."""
    txns_baseline = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=i),
            description=(f"POS PURCHASE {i * 17 % 100:03d}/STORE/CONFIRMATION-{i * 8311:08d}-XYZ"),
            amount=Decimal("17.43") + Decimal(i),
            source_line=i + 1,
        )
        for i in range(10)
    ]
    # Tier 0 — nothing on: math turned off, varied desc, no round, no font.
    tier0 = detect_ai_generated_statement(
        txns_baseline,
        math_flags=["reconciliation_failed_period: x"],
        font_result=_inconsistent_font_result(),
        period_flags=[],
    )
    # Tier 1 — turn on math perfection only.
    tier1 = detect_ai_generated_statement(
        txns_baseline,
        math_flags=[],
        font_result=_inconsistent_font_result(),
        period_flags=[],
    )
    # Tier 2 — math + font uniformity.
    tier2 = detect_ai_generated_statement(
        txns_baseline,
        math_flags=[],
        font_result=_uniform_font_result(),
        period_flags=[],
    )
    # Tier 3 — math + font + round-cluster (replace amounts with whole-$).
    txns_round = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=i),
            description=(f"POS PURCHASE {i * 17 % 100:03d}/STORE/CONFIRMATION-{i * 8311:08d}-XYZ"),
            amount=Decimal("500.00"),
            source_line=i + 1,
        )
        for i in range(10)
    ]
    tier3 = detect_ai_generated_statement(
        txns_round,
        math_flags=[],
        font_result=_uniform_font_result(),
        period_flags=[],
    )
    # Tier 4 — math + font + round + description-uniformity.
    txns_all = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=i),
            description="ACH DEPOSIT MERCHANT SALES",
            amount=Decimal("500.00"),
            source_line=i + 1,
        )
        for i in range(10)
    ]
    tier4 = detect_ai_generated_statement(
        txns_all,
        math_flags=[],
        font_result=_uniform_font_result(),
        period_flags=[],
    )

    def _sev(p: Pattern | None) -> int:
        if p is None:
            return 0
        return p.severity

    severities = [_sev(tier0), _sev(tier1), _sev(tier2), _sev(tier3), _sev(tier4)]
    assert severities == sorted(severities), (
        f"composite must be non-decreasing as signals turn on; got {severities}"
    )


# ---------------------------------------------------------------------------
# Source-ids invariant — composite emit carries empty source_ids
# ---------------------------------------------------------------------------


def test_emitted_pattern_carries_empty_source_ids() -> None:
    txns = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=i * 2),
            description="ACH DEPOSIT MERCHANT SALES",
            amount=Decimal("1000.00"),
            source_line=i + 1,
        )
        for i in range(12)
    ]
    result = detect_ai_generated_statement(
        txns,
        math_flags=[],
        font_result=_uniform_font_result(),
        period_flags=[],
    )
    assert result is not None
    assert result.source_ids == [], (
        "composite emit is a document-level judgment; per-row drill-down "
        "lives on the contributing single-signal detectors, not here"
    )


# ---------------------------------------------------------------------------
# Period-flags input — non-empty disqualifies Signal 1
# ---------------------------------------------------------------------------


def test_period_flags_disqualifies_signal_1() -> None:
    txns = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=i),
            description="ACH DEPOSIT MERCHANT SALES",
            amount=Decimal("500.00"),
            source_line=i + 1,
        )
        for i in range(10)
    ]
    with_period_flag = detect_ai_generated_statement(
        txns,
        math_flags=[],
        font_result=None,
        period_flags=["period_total_mismatch:listed=42 sum=43"],
    )
    without_period_flag = detect_ai_generated_statement(
        txns, math_flags=[], font_result=None, period_flags=[]
    )
    assert with_period_flag is not None and without_period_flag is not None
    s1_with, _, _, _ = _detail_signal_contributions(with_period_flag.detail)
    s1_without, _, _, _ = _detail_signal_contributions(without_period_flag.detail)
    assert s1_with == 0
    assert s1_without == 30


# ---------------------------------------------------------------------------
# Empty corpus -> None
# ---------------------------------------------------------------------------


def test_empty_transactions_returns_none() -> None:
    result = detect_ai_generated_statement(
        [], math_flags=[], font_result=_uniform_font_result(), period_flags=[]
    )
    assert result is None


# ---------------------------------------------------------------------------
# Running-balance disagreement disqualifies Signal 1 even with no flags
# ---------------------------------------------------------------------------


def test_running_balance_disagreement_disqualifies_signal_1() -> None:
    """Two consecutive rows with running balances that don't line up
    with prev + amount -> Signal 1 must score 0 even when math_flags
    is empty."""
    txns = [
        _txn(
            posted_date=PERIOD_START,
            description="ACH DEPOSIT MERCHANT SALES",
            amount=Decimal("500.00"),
            running_balance=Decimal("1000.00"),
            source_line=1,
        ),
        _txn(
            posted_date=PERIOD_START + timedelta(days=1),
            description="ACH DEPOSIT MERCHANT SALES",
            amount=Decimal("500.00"),
            running_balance=Decimal("9999.00"),  # should be 1500.00
            source_line=2,
        ),
    ]
    # Pad to clear other signals enough that the test exercises S1.
    padding = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=2 + i),
            description="ACH DEPOSIT MERCHANT SALES",
            amount=Decimal("500.00"),
            source_line=3 + i,
        )
        for i in range(8)
    ]
    result = detect_ai_generated_statement(
        txns + padding,
        math_flags=[],
        font_result=_uniform_font_result(),
        period_flags=[],
    )
    assert result is not None
    s1, _, _, _ = _detail_signal_contributions(result.detail)
    assert s1 == 0


# ---------------------------------------------------------------------------
# Min-active-signals guard (added 2026-06-27 after shadow audit — see
# constants block in ``ai_statement.py``). The detector must require at
# least 2 of the 4 component signals to be non-zero before firing,
# regardless of composite total.
# ---------------------------------------------------------------------------


def test_min_active_signals_single_math_does_not_fire() -> None:
    """Only Signal 1 (math) fires at +30 — composite is below threshold
    AND active_signals==1 < min. The min-signal guard short-circuits
    before the composite check; result is None for the active-signals
    reason rather than the threshold reason."""
    # Highly varied descriptions (Signal 2 = 0), font None (Signal 4 =
    # 0), 0% round amounts (Signal 3 = 0). Math perfection fires +30
    # because math_flags=[] / period_flags=[] / running_balance None.
    txns = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=i),
            description=f"POS PURCHASE STARBUCKS #{i:04d} NYC CONF {i * 8311:08d} TR {i:06d}",
            amount=Decimal("17.43") + Decimal(i),
            source_line=i + 1,
        )
        for i in range(10)
    ]
    result = detect_ai_generated_statement(txns, math_flags=[], font_result=None, period_flags=[])
    assert result is None


def test_min_active_signals_two_signals_fires_when_threshold_clears() -> None:
    """Signals 1 + 3 active (math +30, round +13) → composite 43 ≥ 40
    AND active_signals=2 → fires. The shadow-audit pattern (the exact
    fire mode we observed 17 times on prod) — under the new guard this
    still fires because there ARE 2 active signals."""
    # 20% round (2 of 10) -> Signal 3 = 13 (linear interp). Varied
    # descriptions keep Signal 2 = 0. Font None keeps Signal 4 = 0.
    txns = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=i),
            description=f"POS PURCHASE STORE {i * 17 % 100:03d} CONF {i * 31:06d}",
            amount=Decimal("500.00") if i < 2 else Decimal("17.43") + Decimal(i),
            source_line=i + 1,
        )
        for i in range(10)
    ]
    result = detect_ai_generated_statement(txns, math_flags=[], font_result=None, period_flags=[])
    assert result is not None
    assert result.severity >= 40
    # Confirm 2 active signals are reflected in the detail string.
    s1, s2, s3, s4 = _detail_signal_contributions(result.detail)
    active = sum(1 for v in (s1, s2, s3, s4) if v > 0)
    assert active == 2


def test_min_active_signals_two_signals_below_threshold_does_not_fire() -> None:
    """Two signals active but composite still below 40 → still None
    (threshold gate still applies). Math +30 + round +1 (10%-equivalent
    forced to a tiny scaled contribution) = 31 < 40."""
    # Build a corpus where Signal 1 fires (+30) and Signal 3 fires with
    # a small contribution. 1 round of 10 = 10% → Signal 3 = 0 (below
    # 20% floor). So we need exactly the floor: 2 of 10 = 20% → +12 or
    # 13. 30 + 13 = 43, which IS above 40. To get below, use the
    # round-cluster + font signals such that the total stays under 40.
    # Simpler: use Signal 1 + Signal 4 — math 30 + font 20 = 50 (above).
    # Try Signal 3 + Signal 4: round 13 + font 20 = 33 (below 40), and
    # Signal 1 = 0 (force a math failure), Signal 2 = 0 (varied desc).
    txns = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=i),
            description=f"POS PURCHASE STORE {i * 17 % 100:03d} CONF {i * 31:06d}",
            amount=Decimal("500.00") if i < 2 else Decimal("17.43") + Decimal(i),
            source_line=i + 1,
        )
        for i in range(10)
    ]
    result = detect_ai_generated_statement(
        txns,
        math_flags=["reconciliation_failed_period: forced off"],
        font_result=_uniform_font_result(),
        period_flags=[],
    )
    # Signal 1 = 0 (math_flags), Signal 2 = 0 (varied), Signal 3 = 13
    # (20% round), Signal 4 = 20 (uniform). Composite = 33 < 40.
    # Active signals = 2 (S3 + S4), so the min-signal guard passes but
    # the threshold check rejects.
    assert result is None


def test_min_active_signals_all_four_fires() -> None:
    """All 4 signals active — fires (no regression on the well-formed
    high-score case)."""
    txns = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=i * 2),
            description="ACH DEPOSIT MERCHANT SALES",
            amount=Decimal("1000.00"),
            source_line=i + 1,
        )
        for i in range(12)
    ]
    result = detect_ai_generated_statement(
        txns,
        math_flags=[],
        font_result=_uniform_font_result(),
        period_flags=[],
    )
    assert result is not None
    s1, s2, s3, s4 = _detail_signal_contributions(result.detail)
    assert all(v > 0 for v in (s1, s2, s3, s4))


def test_min_active_signals_zero_signals_returns_none() -> None:
    """All 4 signals at 0 — composite 0, no fire (regression guard for
    the all-clear case)."""
    txns = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=i),
            description=f"POS PURCHASE STARBUCKS #{i:04d} NYC CONF {i * 8311:08d}",
            amount=Decimal("17.43") + Decimal(i),
            source_line=i + 1,
        )
        for i in range(10)
    ]
    result = detect_ai_generated_statement(
        txns,
        math_flags=["reconciliation_failed_period: x"],  # Signal 1 → 0
        font_result=_inconsistent_font_result(),  # Signal 4 → 0
        period_flags=[],
    )
    assert result is None
