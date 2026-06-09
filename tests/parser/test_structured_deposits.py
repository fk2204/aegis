"""M9 structured-deposit (BSA threshold-avoidance) detector tests.

Covers the shadow-mode detector added to ``parser/patterns.py`` for the
audit finding M9. The detector surfaces clusters of deposits in the
$8,500-$9,999 band (just under the 31 CFR § 1010.311 CTR threshold)
that occur in tight ≤14-day windows — the classic 31 USC § 5324
"structuring" / "smurfing" pattern.

Shadow-mode contract:
- Severity 0 on every emitted Pattern.
- Lands in ``PatternAnalysis.shadow_patterns``, NOT ``patterns``.
- Does NOT contribute to ``fraud_score``.
- Does NOT alter ``hard_decline_reasons`` or ``parse_status``.

Per CLAUDE.md § "Decision-boundary changes — deliberate + shadow-first":
this detector emits evidence flags only. Operator validates against
corpus before a follow-up commit flips it into the scored path behind
a config gate.
"""

from __future__ import annotations

from datetime import date
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
    amount: Decimal,
    category: TransactionCategory = "deposit",
    description: str = "DEPOSIT",
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


def _shadow_with_prefix(pa: PatternAnalysis, prefix: str) -> list[Pattern]:
    return [p for p in pa.shadow_patterns if p.code.startswith(prefix)]


def _live_codes(pa: PatternAnalysis) -> list[str]:
    return [p.code for p in pa.patterns]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_three_in_band_deposits_within_14d_window_fires() -> None:
    """Three $9,500 deposits inside a 14-day window — the textbook smurf."""
    txns = [
        _txn(
            posted_date=date(2026, 1, day),
            amount=Decimal("9500.00"),
            source_line=i + 1,
        )
        for i, day in enumerate((3, 8, 13))
    ]
    pa = _analyze(txns)
    hits = _shadow_with_prefix(pa, "structured_deposit_cluster:")
    assert len(hits) == 1, f"expected one cluster, got {[h.code for h in hits]}"
    code = hits[0].code
    assert "3_deposits_in_14_day_window" in code
    # All three calendar dates appear in the flag (auditability — Section 4 of
    # the architecture rule, "every aggregate stores its source transaction IDs").
    assert "20260103" in code
    assert "20260108" in code
    assert "20260113" in code
    # Shadow contract.
    assert hits[0].severity == 0
    assert len(hits[0].source_ids) == 3
    # Live decision-boundary surface untouched.
    assert pa.fraud_score == 0 or "structured_deposit_cluster" not in _live_codes(pa)
    assert "structured_deposit_cluster" not in "".join(_live_codes(pa))


def test_emitted_flag_lists_actual_transaction_uuids() -> None:
    """Source-id traceability: the flag's source_ids ARE the cluster's UUIDs."""
    txns = [
        _txn(posted_date=date(2026, 1, 4), amount=Decimal("9200.00")),
        _txn(posted_date=date(2026, 1, 9), amount=Decimal("9750.00")),
        _txn(posted_date=date(2026, 1, 14), amount=Decimal("8900.00")),
    ]
    expected_ids = {t.id for t in txns}
    pa = _analyze(txns)
    hits = _shadow_with_prefix(pa, "structured_deposit_cluster:")
    assert len(hits) == 1
    got_ids = set(hits[0].source_ids)
    assert got_ids == expected_ids, (
        f"source_ids must echo the cluster's UUIDs verbatim; "
        f"missing={expected_ids - got_ids}, extra={got_ids - expected_ids}"
    )


# ---------------------------------------------------------------------------
# Out-of-band — too high
# ---------------------------------------------------------------------------


def test_three_deposits_above_band_do_not_fire() -> None:
    """$11,000 deposits are above the CTR threshold (would be reported anyway)."""
    txns = [
        _txn(
            posted_date=date(2026, 1, day),
            amount=Decimal("11000.00"),
        )
        for day in (3, 8, 13)
    ]
    pa = _analyze(txns)
    assert _shadow_with_prefix(pa, "structured_deposit_cluster:") == []


# ---------------------------------------------------------------------------
# Out-of-band — too low
# ---------------------------------------------------------------------------


def test_three_deposits_below_band_do_not_fire() -> None:
    """$7,000 deposits are under the avoidance band — routine business volume."""
    txns = [
        _txn(
            posted_date=date(2026, 1, day),
            amount=Decimal("7000.00"),
        )
        for day in (3, 8, 13)
    ]
    pa = _analyze(txns)
    assert _shadow_with_prefix(pa, "structured_deposit_cluster:") == []


# ---------------------------------------------------------------------------
# Window discipline
# ---------------------------------------------------------------------------


def test_three_in_band_deposits_spread_over_30d_do_not_fire() -> None:
    """Three $9,500 deposits across 30 days — no 14-day sub-window has ≥3."""
    txns = [
        _txn(posted_date=date(2026, 1, 1), amount=Decimal("9500.00")),
        _txn(posted_date=date(2026, 1, 16), amount=Decimal("9500.00")),
        _txn(posted_date=date(2026, 1, 30), amount=Decimal("9500.00")),
    ]
    pa = _analyze(txns)
    assert _shadow_with_prefix(pa, "structured_deposit_cluster:") == []


# ---------------------------------------------------------------------------
# Count discipline
# ---------------------------------------------------------------------------


def test_two_in_band_deposits_within_14d_do_not_fire() -> None:
    """Two in-band deposits inside 14d — the ≥3 floor blocks the flag."""
    txns = [
        _txn(posted_date=date(2026, 1, 5), amount=Decimal("9500.00")),
        _txn(posted_date=date(2026, 1, 12), amount=Decimal("9500.00")),
    ]
    pa = _analyze(txns)
    assert _shadow_with_prefix(pa, "structured_deposit_cluster:") == []


# ---------------------------------------------------------------------------
# Boundary — $8,500 fires, $8,499 does not
# ---------------------------------------------------------------------------


def test_band_lower_boundary_8500_fires() -> None:
    """$8,500 exact — included in the avoidance band, three of them flag."""
    txns = [
        _txn(
            posted_date=date(2026, 1, day),
            amount=Decimal("8500.00"),
        )
        for day in (3, 8, 13)
    ]
    pa = _analyze(txns)
    hits = _shadow_with_prefix(pa, "structured_deposit_cluster:")
    assert len(hits) == 1


def test_band_lower_boundary_8499_does_not_fire() -> None:
    """$8,499 — one cent below the band floor, do not flag."""
    txns = [
        _txn(
            posted_date=date(2026, 1, day),
            amount=Decimal("8499.00"),
        )
        for day in (3, 8, 13)
    ]
    pa = _analyze(txns)
    assert _shadow_with_prefix(pa, "structured_deposit_cluster:") == []


# ---------------------------------------------------------------------------
# Boundary — $9,999 fires, $10,000 does not
# ---------------------------------------------------------------------------


def test_band_upper_boundary_9999_fires() -> None:
    """$9,999 — last cent under the CTR threshold, three of them flag."""
    txns = [
        _txn(
            posted_date=date(2026, 1, day),
            amount=Decimal("9999.00"),
        )
        for day in (3, 8, 13)
    ]
    pa = _analyze(txns)
    hits = _shadow_with_prefix(pa, "structured_deposit_cluster:")
    assert len(hits) == 1


def test_band_upper_boundary_10000_does_not_fire() -> None:
    """$10,000 — at the CTR threshold, bank files anyway, no avoidance."""
    txns = [
        _txn(
            posted_date=date(2026, 1, day),
            amount=Decimal("10000.00"),
        )
        for day in (3, 8, 13)
    ]
    pa = _analyze(txns)
    assert _shadow_with_prefix(pa, "structured_deposit_cluster:") == []


# ---------------------------------------------------------------------------
# Category discipline
# ---------------------------------------------------------------------------


def test_three_in_band_ach_credits_fire() -> None:
    """``ach_credit`` is in scope per the BSA-detector category set."""
    txns = [
        _txn(
            posted_date=date(2026, 1, day),
            amount=Decimal("9500.00"),
            category="ach_credit",
            description="ACH CREDIT",
        )
        for day in (3, 8, 13)
    ]
    pa = _analyze(txns)
    hits = _shadow_with_prefix(pa, "structured_deposit_cluster:")
    assert len(hits) == 1


def test_three_in_band_wires_fire() -> None:
    """``wire_in`` is in scope; operator interprets context (wires rarely smurfed)."""
    txns = [
        _txn(
            posted_date=date(2026, 1, day),
            amount=Decimal("9500.00"),
            category="wire_in",
            description="INCOMING WIRE",
        )
        for day in (3, 8, 13)
    ]
    pa = _analyze(txns)
    hits = _shadow_with_prefix(pa, "structured_deposit_cluster:")
    assert len(hits) == 1


def test_refunds_in_band_do_not_fire() -> None:
    """``refund`` is not depositor-initiated — out of detector scope."""
    txns = [
        _txn(
            posted_date=date(2026, 1, day),
            amount=Decimal("9500.00"),
            category="refund",
            description="VENDOR REFUND",
        )
        for day in (3, 8, 13)
    ]
    pa = _analyze(txns)
    assert _shadow_with_prefix(pa, "structured_deposit_cluster:") == []


# ---------------------------------------------------------------------------
# Shadow-mode contract — fraud_score / live patterns untouched
# ---------------------------------------------------------------------------


def test_structured_deposit_detector_does_not_alter_fraud_score() -> None:
    """Shadow contract: severity 0, fraud_score additive contribution = 0."""
    txns = [
        _txn(
            posted_date=date(2026, 1, day),
            amount=Decimal("9500.00"),
        )
        for day in (3, 8, 13)
    ]
    pa = _analyze(txns)
    # Severity floor on the emitted flag.
    hits = _shadow_with_prefix(pa, "structured_deposit_cluster:")
    assert len(hits) == 1
    assert hits[0].severity == 0
    # No equivalent code in the live patterns list.
    assert all(
        not p.code.startswith("structured_deposit_cluster")
        for p in pa.patterns
    )
    # Combined: fraud_score is the sum of live-pattern severities only,
    # so even if other live detectors fire (none should here), the new
    # detector contributes 0 by construction.
    live_only = sum(p.severity for p in pa.patterns)
    assert pa.fraud_score == min(100, live_only)
