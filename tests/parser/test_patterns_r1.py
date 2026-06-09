"""R1.1 + R1.3 shadow-mode detector tests.

Covers the three new detectors added to ``parser/patterns.py`` as part of
the 2026-06-08 audit remediation (plan
``delightful-beaming-meerkat.md`` § R1.1 / R1.3):

  1. ``_detect_fuzzy_mca_candidates``     -> ``mca_position_fuzzy_candidate:``
  2. ``_detect_disguise_candidates``      -> ``mca_disguise_candidate:``
  3. ``_detect_same_day_mca_funder_cluster`` -> ``mca_same_day_cluster:``

All three land in ``PatternAnalysis.shadow_patterns`` with severity 0.
None of them contribute to ``fraud_score`` or ``patterns`` — the live
decline path is byte-identical to pre-R1. Per AEGIS CLAUDE.md §
"Decision-boundary changes — deliberate + shadow-first", these are
emit-only flags surfaced for operator corpus validation. A follow-up
flips them into the scored path once false-positive rate is confirmed
low on the live corpus.

This file deliberately does NOT modify ``tests/parser/test_patterns.py``
— the new detectors are additive and live in a separate file so the
existing patterns regression surface is undisturbed.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

from aegis.parser.models import ClassifiedTransaction, TransactionCategory
from aegis.parser.patterns import Pattern, PatternAnalysis, analyze_patterns

PERIOD_START = date(2026, 1, 1)
PERIOD_END = date(2026, 1, 31)
TODAY = date(2026, 2, 5)


def _txn(
    *,
    posted_date: date,
    description: str,
    amount: Decimal,
    category: TransactionCategory = "mca_debit",
    source_line: int | None = None,
) -> ClassifiedTransaction:
    return ClassifiedTransaction(
        id=uuid4(),
        posted_date=posted_date,
        description=description,
        amount=amount,
        running_balance=None,
        source_page=1,
        source_line=source_line or 1,
        category=category,
        classification_confidence=95,
    )


def _analyze(txns: list[ClassifiedTransaction]) -> PatternAnalysis:
    return analyze_patterns(
        txns, period_start=PERIOD_START, period_end=PERIOD_END, today=TODAY
    )


def _shadow_codes(pa: PatternAnalysis) -> list[str]:
    return [p.code for p in pa.shadow_patterns]


def _live_codes(pa: PatternAnalysis) -> list[str]:
    return [p.code for p in pa.patterns]


def _shadow_with_prefix(pa: PatternAnalysis, prefix: str) -> list[Pattern]:
    return [p for p in pa.shadow_patterns if p.code.startswith(prefix)]


# ---------------------------------------------------------------------------
# R1.1 — fuzzy funder-name matching
# ---------------------------------------------------------------------------


def test_fuzzy_match_fires_on_kappitus_typo_three_occurrences() -> None:
    """KAPPITUS (single extra P) -> KAPITUS, ratio ~ 0.93."""
    txns = [
        _txn(
            posted_date=date(2026, 1, day),
            description="KAPPITUS DAILY ACH",
            amount=Decimal("-250.00"),
            source_line=i + 1,
        )
        for i, day in enumerate((5, 10, 15))
    ]
    pa = _analyze(txns)
    hits = _shadow_with_prefix(pa, "mca_position_fuzzy_candidate:")
    assert len(hits) == 1, f"expected one fuzzy hit, got {[h.code for h in hits]}"
    assert "KAPITUS" in hits[0].code
    assert "_3_" in hits[0].code  # occurrence count
    assert hits[0].severity == 0
    assert len(hits[0].source_ids) == 3
    # Live decline path untouched: no `mca_stacking` because KAPPITUS does
    # not match KNOWN_FUNDERS exactly.
    assert "mca_stacking" not in _live_codes(pa)


def test_fuzzy_match_fires_on_ondek_funding_typo() -> None:
    """ONDEK FUNDING (typo, missing C) -> ONDECK via token-prefix path."""
    txns = [
        _txn(
            posted_date=date(2026, 1, day),
            description="ONDEK FUNDING ACH DEBIT",
            amount=Decimal("-180.00"),
        )
        for day in (3, 8, 13, 18)
    ]
    pa = _analyze(txns)
    hits = _shadow_with_prefix(pa, "mca_position_fuzzy_candidate:")
    assert len(hits) == 1, f"expected one ONDECK fuzzy hit, got {[h.code for h in hits]}"
    assert "ONDECK" in hits[0].code
    assert "_4_" in hits[0].code  # occurrence count
    assert "mca_stacking" not in _live_codes(pa)


def test_fuzzy_match_silent_on_kapital_city_auto_parts() -> None:
    """A legitimate merchant 'KAPITAL CITY AUTO PARTS' must NOT match KAPITUS.

    The token-level SeqMatcher floor (0.80) on the prefix-overlap path
    is the boundary that catches this — SequenceMatcher('KAPITAL',
    'KAPITUS') = 0.714 < 0.80 even though the 5-char prefix 'KAPIT'
    overlaps. Multiple occurrences would otherwise pass the cadence
    floor; the test verifies the matcher rejects ahead of cadence.
    """
    txns = [
        _txn(
            posted_date=date(2026, 1, day),
            description="POS KAPITAL CITY AUTO PARTS 14523",
            amount=Decimal("-87.42"),
            category="other",
        )
        for day in (5, 10, 15, 20, 25)
    ]
    pa = _analyze(txns)
    hits = _shadow_with_prefix(pa, "mca_position_fuzzy_candidate:")
    assert hits == [], f"unexpected fuzzy hit: {[h.code for h in hits]}"


def test_fuzzy_match_silent_on_capital_city_without_cadence() -> None:
    """'CAPITAL CITY' on one or two debits must not flag.

    Two safeguards: (a) the substring 'CAPITAL' alone is not in
    KNOWN_FUNDERS (and 'CAPITAL DAILY' / 'MERCHANT ADVANCE' etc. require
    daily cadence in the exact path), (b) the fuzzy path requires ≥3
    occurrences to flag.
    """
    txns = [
        _txn(
            posted_date=date(2026, 1, day),
            description="ACH DEBIT CAPITAL CITY UTILITY",
            amount=Decimal("-150.00"),
            category="other",
        )
        for day in (5, 10)
    ]
    pa = _analyze(txns)
    hits = _shadow_with_prefix(pa, "mca_position_fuzzy_candidate:")
    assert hits == [], f"unexpected fuzzy hit: {[h.code for h in hits]}"


def test_fuzzy_match_skips_rows_already_exact_matched() -> None:
    """Exact-substring rows ('ONDECK ...') are owned by ``_detect_mca_positions``.

    The fuzzy path explicitly skips them so a clean exact match doesn't
    also surface a duplicate shadow candidate.
    """
    txns = [
        _txn(
            posted_date=date(2026, 1, day),
            description="ONDECK CAPITAL ACH",
            amount=Decimal("-200.00"),
        )
        for day in (5, 10, 15)
    ]
    pa = _analyze(txns)
    assert "mca_stacking" in _live_codes(pa)
    assert _shadow_with_prefix(pa, "mca_position_fuzzy_candidate:") == []


def test_fuzzy_match_below_three_occurrences_does_not_flag() -> None:
    """Two KAPPITUS rows is below the ≥3 occurrence floor — no shadow flag."""
    txns = [
        _txn(
            posted_date=date(2026, 1, day),
            description="KAPPITUS REMIT",
            amount=Decimal("-250.00"),
        )
        for day in (5, 10)
    ]
    pa = _analyze(txns)
    assert _shadow_with_prefix(pa, "mca_position_fuzzy_candidate:") == []


# ---------------------------------------------------------------------------
# R1.1 — disguise descriptors (cadence-gated)
# ---------------------------------------------------------------------------


def test_disguise_settlement_advance_single_occurrence_does_not_fire() -> None:
    """A one-off 'SETTLEMENT ADVANCE' debit must never trigger the disguise flag."""
    txns = [
        _txn(
            posted_date=date(2026, 1, 5),
            description="SETTLEMENT ADVANCE PMT",
            amount=Decimal("-500.00"),
            category="other",
        )
    ]
    pa = _analyze(txns)
    assert _shadow_with_prefix(pa, "mca_disguise_candidate:") == []


def test_disguise_settlement_advance_below_ten_does_not_fire() -> None:
    """Below the ≥10-occurrence floor the disguise flag stays silent."""
    txns = [
        _txn(
            posted_date=date(2026, 1, 1) + timedelta(days=i),
            description="SETTLEMENT ADVANCE PMT",
            amount=Decimal("-500.00"),
            category="other",
        )
        for i in range(8)  # 8 daily debits — under the floor
    ]
    pa = _analyze(txns)
    assert _shadow_with_prefix(pa, "mca_disguise_candidate:") == []


def test_disguise_settlement_advance_fires_on_twelve_daily_occurrences() -> None:
    """12 'SETTLEMENT ADVANCE' debits over 4 weeks at daily cadence -> flag fires."""
    # Twelve debits, ~2-day spacing -> median spacing = 2, under threshold.
    txns = [
        _txn(
            posted_date=date(2026, 1, 1) + timedelta(days=i * 2),
            description="SETTLEMENT ADVANCE DAILY",
            amount=Decimal("-500.00"),
            category="other",
            source_line=i + 1,
        )
        for i in range(12)
    ]
    pa = _analyze(txns)
    hits = _shadow_with_prefix(pa, "mca_disguise_candidate:")
    assert len(hits) == 1, f"expected one disguise hit, got {[h.code for h in hits]}"
    assert "SETTLEMENT ADVANCE" in hits[0].code
    assert hits[0].code.endswith("_12_2")
    assert hits[0].severity == 0
    assert len(hits[0].source_ids) == 12


def test_disguise_revenue_based_lending_fires_at_daily_cadence() -> None:
    """REVENUE BASED LENDING with 11 daily occurrences -> flag fires."""
    txns = [
        _txn(
            posted_date=date(2026, 1, 1) + timedelta(days=i),
            description="REVENUE BASED LENDING ACH",
            amount=Decimal("-300.00"),
            category="other",
        )
        for i in range(11)
    ]
    pa = _analyze(txns)
    hits = _shadow_with_prefix(pa, "mca_disguise_candidate:")
    assert any("REVENUE BASED LENDING" in h.code for h in hits), [h.code for h in hits]


def test_disguise_silent_when_spacing_exceeds_two_days() -> None:
    """10 'CAPITAL ADVANCE' debits but at weekly spacing -> no flag (median > 2d)."""
    txns = [
        _txn(
            posted_date=date(2026, 1, 1) + timedelta(days=i * 7),
            description="CAPITAL ADVANCE",
            amount=Decimal("-1000.00"),
            category="other",
        )
        for i in range(10)
    ]
    # Push period_end so the txns fit inside the analyze window.
    pa = analyze_patterns(
        txns,
        period_start=PERIOD_START,
        period_end=date(2026, 3, 31),
        today=TODAY,
    )
    assert _shadow_with_prefix(pa, "mca_disguise_candidate:") == []


# ---------------------------------------------------------------------------
# R1.3 — same-day funder cluster
# ---------------------------------------------------------------------------


def _exact_mca_position_rows(
    funder_keyword: str, day_offsets: tuple[int, ...]
) -> list[ClassifiedTransaction]:
    """Build exact-match MCA debit rows for ``funder_keyword`` on the given days.

    Three+ occurrences are required so ``_detect_mca_positions`` will
    surface it as a position — the same-day cluster detector only walks
    detected positions.
    """
    return [
        _txn(
            posted_date=date(2026, 1, day),
            description=f"{funder_keyword} ACH DEBIT",
            amount=Decimal("-150.00"),
        )
        for day in day_offsets
    ]


def test_same_day_cluster_fires_on_three_distinct_funders_one_date() -> None:
    """Three distinct funder positions all hitting Jan 15 -> cluster flag fires."""
    cluster_day = 15
    # Three positions, each with ≥3 occurrences so they qualify as
    # detected MCA positions. The cluster_day is shared across all three;
    # the other days are different so no incidental second cluster forms.
    txns: list[ClassifiedTransaction] = []
    txns.extend(_exact_mca_position_rows("ONDECK", (3, 8, cluster_day)))
    txns.extend(_exact_mca_position_rows("KAPITUS", (5, 10, cluster_day)))
    txns.extend(_exact_mca_position_rows("CREDIBLY", (6, 12, cluster_day)))
    pa = _analyze(txns)
    hits = _shadow_with_prefix(pa, "mca_same_day_cluster:")
    assert len(hits) == 1, f"expected one cluster hit, got {[h.code for h in hits]}"
    code = hits[0].code
    assert "2026-01-15" in code
    assert "_3_" in code
    # All three funder labels must appear in the pipe-joined name list.
    for funder in ("ondeck", "kapitus", "credibly"):
        assert funder in code.lower(), f"missing funder {funder!r} in {code!r}"
    assert hits[0].severity == 0


def test_same_day_cluster_silent_on_two_distinct_funders() -> None:
    """Only two funders on the same date -> no cluster flag."""
    cluster_day = 15
    txns: list[ClassifiedTransaction] = []
    txns.extend(_exact_mca_position_rows("ONDECK", (3, 8, cluster_day)))
    txns.extend(_exact_mca_position_rows("KAPITUS", (5, 10, cluster_day)))
    pa = _analyze(txns)
    assert _shadow_with_prefix(pa, "mca_same_day_cluster:") == []


def test_same_day_cluster_with_only_two_positions_does_not_fire() -> None:
    """If only two MCA positions exist in total, the cluster detector exits early."""
    txns: list[ClassifiedTransaction] = []
    txns.extend(_exact_mca_position_rows("ONDECK", (3, 8, 15)))
    txns.extend(_exact_mca_position_rows("KAPITUS", (5, 10, 15)))
    pa = _analyze(txns)
    # Two real mca_positions exist (live path)
    assert "mca_stacking" in _live_codes(pa)
    # but the cluster detector requires ≥3 distinct funders on a single
    # date.
    assert _shadow_with_prefix(pa, "mca_same_day_cluster:") == []


# ---------------------------------------------------------------------------
# Shadow-mode invariants — ensure live decline path is untouched.
# ---------------------------------------------------------------------------


def test_shadow_patterns_do_not_contribute_to_fraud_score() -> None:
    """Even when all three R1 detectors fire, ``fraud_score`` is unchanged.

    Builds a scenario that fires all three shadow flags AND a real
    exact-match position. Asserts ``fraud_score`` equals only the real
    position's severity (15 for one position).
    """
    cluster_day = 20
    txns: list[ClassifiedTransaction] = []
    # Real exact match — contributes severity 15 via mca_stacking.
    txns.extend(_exact_mca_position_rows("ONDECK", (3, 8, cluster_day)))
    txns.extend(_exact_mca_position_rows("KAPITUS", (5, 10, cluster_day)))
    txns.extend(_exact_mca_position_rows("CREDIBLY", (6, 12, cluster_day)))
    # Fuzzy: three KAPPITUS rows (typo, not exact).
    txns.extend(
        _txn(
            posted_date=date(2026, 1, day),
            description="KAPPITUS REMIT",
            amount=Decimal("-250.00"),
        )
        for day in (4, 9, 14)
    )
    # Disguise: 12 daily SETTLEMENT ADVANCE rows.
    txns.extend(
        _txn(
            posted_date=date(2026, 1, 1) + timedelta(days=i * 2),
            description="SETTLEMENT ADVANCE DAILY",
            amount=Decimal("-500.00"),
            category="other",
        )
        for i in range(12)
    )

    pa = _analyze(txns)
    # Three live position-equivalent groups -> mca_stacking severity = 15*3=45.
    # No other live detectors should fire on this synthetic stream.
    assert "mca_stacking" in _live_codes(pa)
    live_total = sum(p.severity for p in pa.patterns)
    assert pa.fraud_score == min(100, live_total)
    # All three shadow flags must be present.
    shadow_codes = _shadow_codes(pa)
    assert any(c.startswith("mca_position_fuzzy_candidate:") for c in shadow_codes)
    assert any(c.startswith("mca_disguise_candidate:") for c in shadow_codes)
    assert any(c.startswith("mca_same_day_cluster:") for c in shadow_codes)
    # All shadow severities are zero.
    assert all(p.severity == 0 for p in pa.shadow_patterns)


def test_no_shadow_patterns_on_clean_statement() -> None:
    """A clean deposit-only statement produces zero shadow flags."""
    txns = [
        _txn(
            posted_date=date(2026, 1, day),
            description=f"CUSTOMER DEPOSIT {day} TRACE 8821{day:02d}33",
            amount=Decimal("1000.00"),
            category="deposit",
        )
        for day in (3, 7, 12, 18, 25)
    ]
    pa = _analyze(txns)
    assert pa.shadow_patterns == []


def test_flags_property_includes_shadow_codes_after_live() -> None:
    """``PatternAnalysis.flags`` returns live + shadow codes (live-first)."""
    txns = [
        _txn(
            posted_date=date(2026, 1, day),
            description="KAPPITUS REMIT",
            amount=Decimal("-250.00"),
        )
        for day in (5, 10, 15)
    ]
    pa = _analyze(txns)
    flags = pa.flags
    live_codes = [p.code for p in pa.patterns]
    # Live codes appear first; shadow codes append.
    for i, code in enumerate(live_codes):
        assert flags[i] == code
    assert flags[len(live_codes):] == [p.code for p in pa.shadow_patterns]
