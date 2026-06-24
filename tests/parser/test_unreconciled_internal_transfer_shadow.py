"""Shadow-mode unreconciled-internal-transfer detector tests.

Covers ``detect_unreconciled_internal_transfers`` added to
``parser/patterns.py`` per operator spec 2026-06-24:

- Detects transfer-OUT rows (``own_account`` classification OR
  ``TRANSFER TO`` / ``WIRE TO`` / ``ACH TO`` / ``ZELLE TO`` description)
  with ``abs(amount) > $500`` that have no matching transfer-in within
  ±$50 / ±5 days anywhere in the submitted bundle.
- Severity: 25 per instance, compound to 40 at 3+ instances, cap 60.
- Shadow-mode: emits to ``PatternAnalysis.shadow_patterns``, code
  ``unreconciled_internal_transfer``, severity-0 carve-out via
  ``FRAUD_WEIGHTS["shadow_unreconciled_internal_transfer"] == 0``.
  Does NOT alter ``fraud_score`` / ``parse_status``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from aegis.parser.models import ClassifiedTransaction, TransactionCategory
from aegis.parser.patterns import (
    Pattern,
    PatternAnalysis,
    analyze_patterns,
    detect_unreconciled_internal_transfers,
)
from aegis.parser.pipeline import FRAUD_WEIGHTS

PERIOD_START = date(2026, 1, 1)
PERIOD_END = date(2026, 1, 31)
TODAY = date(2026, 2, 5)


def _txn(
    *,
    posted_date: date,
    description: str,
    amount: Decimal,
    category: TransactionCategory = "transfer",
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


def _shadow_uit(pa: PatternAnalysis) -> list[Pattern]:
    return [p for p in pa.shadow_patterns if p.code == "unreconciled_internal_transfer"]


def _analyze(txns: list[ClassifiedTransaction]) -> PatternAnalysis:
    return analyze_patterns(txns, period_start=PERIOD_START, period_end=PERIOD_END, today=TODAY)


# ---------------------------------------------------------------------------
# Spec test 1 — matched transfer-out/transfer-in -> no flag
# ---------------------------------------------------------------------------


def test_matched_transfer_out_and_in_does_not_fire() -> None:
    out = _txn(
        posted_date=date(2026, 1, 10),
        description="TRANSFER TO CHK 7722 CONFIRM 1234",
        amount=Decimal("-5000.00"),
        category="transfer",
    )
    inbound = _txn(
        posted_date=date(2026, 1, 10),
        description="TRANSFER FROM CHK 7719 CONFIRM 1234",
        amount=Decimal("5000.00"),
        category="transfer",
    )
    out_only = detect_unreconciled_internal_transfers([out, inbound], [out, inbound])
    assert out_only == [], "matched pair should produce no shadow rows"


# ---------------------------------------------------------------------------
# Spec test 2 — single unmatched -> severity 25
# ---------------------------------------------------------------------------


def test_single_unmatched_transfer_out_severity_25() -> None:
    out = _txn(
        posted_date=date(2026, 1, 15),
        description="WIRE TO HIDDEN ACCT 9940",
        amount=Decimal("-7500.00"),
        category="wire_out",
    )
    # An unrelated deposit far outside the window — must not match.
    unrelated = _txn(
        posted_date=date(2026, 1, 1),
        description="ACH DEPOSIT MERCHANT SALES",
        amount=Decimal("250.00"),
        category="ach_credit",
    )
    hits = detect_unreconciled_internal_transfers([out, unrelated], [out, unrelated])
    assert len(hits) == 1
    assert hits[0].code == "unreconciled_internal_transfer"
    assert hits[0].severity == 25
    assert hits[0].source_ids == [out.id]
    # Detail surfaces counterparty + amount per spec.
    assert "$7500.00" in hits[0].detail
    assert "HIDDEN ACCT 9940" in hits[0].detail


# ---------------------------------------------------------------------------
# Spec test 3 — 3+ unmatched -> severity 40 (compound)
# ---------------------------------------------------------------------------


def test_three_unmatched_transfer_outs_severity_40_compound() -> None:
    outs = [
        _txn(
            posted_date=date(2026, 1, day),
            description=f"TRANSFER TO HIDDEN ACCT {label}",
            amount=Decimal("-1000.00"),
            category="transfer",
            source_line=i + 1,
        )
        for i, (day, label) in enumerate(((5, "A"), (12, "B"), (20, "C")))
    ]
    hits = detect_unreconciled_internal_transfers(outs, outs)
    assert len(hits) == 3
    for h in hits:
        assert h.severity == 40, (
            f"3+ unmatched should bump severity to compound floor 40, got {h.severity}"
        )
        assert h.code == "unreconciled_internal_transfer"
        assert len(h.source_ids) == 1


# ---------------------------------------------------------------------------
# Spec test 4 — transfers <= $500 are ignored
# ---------------------------------------------------------------------------


def test_transfers_at_or_below_500_are_ignored() -> None:
    # $500 exactly — NOT > $500, must be ignored per spec.
    boundary = _txn(
        posted_date=date(2026, 1, 10),
        description="TRANSFER TO MY OTHER ACCT",
        amount=Decimal("-500.00"),
        category="transfer",
    )
    # $499.99 — clearly under the floor.
    under = _txn(
        posted_date=date(2026, 1, 15),
        description="ZELLE TO MY OTHER ACCT",
        amount=Decimal("-499.99"),
        category="transfer",
    )
    hits = detect_unreconciled_internal_transfers([boundary, under], [boundary, under])
    assert hits == [], f"transfers at or below $500 must NOT fire; got {[h.detail for h in hits]}"


# ---------------------------------------------------------------------------
# Spec test 5 — match within 5-day window -> no flag
# ---------------------------------------------------------------------------


def test_match_within_five_day_window_does_not_fire() -> None:
    out = _txn(
        posted_date=date(2026, 1, 10),
        description="WIRE TO BROKERAGE CASH ACCT",
        amount=Decimal("-10000.00"),
        category="wire_out",
    )
    # 5 days later (within the ±5d window) and amount within $50 tolerance.
    inbound = _txn(
        posted_date=date(2026, 1, 15),
        description="WIRE FROM BROKERAGE",
        amount=Decimal("9975.00"),  # $25 less, within ±$50
        category="wire_in",
    )
    hits = detect_unreconciled_internal_transfers([out, inbound], [out, inbound])
    assert hits == [], "match within 5-day window must clear the flag"


def test_match_outside_five_day_window_fires() -> None:
    """Boundary: 6-day gap is OUTSIDE the ±5d window — should fire."""
    out = _txn(
        posted_date=date(2026, 1, 10),
        description="WIRE TO BROKERAGE CASH ACCT",
        amount=Decimal("-10000.00"),
        category="wire_out",
    )
    inbound = _txn(
        posted_date=date(2026, 1, 16),  # 6-day gap, outside window
        description="WIRE FROM BROKERAGE",
        amount=Decimal("10000.00"),
        category="wire_in",
    )
    hits = detect_unreconciled_internal_transfers([out, inbound], [out, inbound])
    assert len(hits) == 1
    assert hits[0].severity == 25


def test_amount_outside_50_tolerance_fires() -> None:
    """Boundary: $51 magnitude difference is OUTSIDE ±$50 — should fire."""
    out = _txn(
        posted_date=date(2026, 1, 10),
        description="ACH TO SAVINGS",
        amount=Decimal("-5000.00"),
        category="transfer",
    )
    inbound = _txn(
        posted_date=date(2026, 1, 11),
        description="ACH FROM CHK",
        amount=Decimal("4949.00"),  # $51 difference, just outside ±$50
        category="ach_credit",
    )
    hits = detect_unreconciled_internal_transfers([out, inbound], [out, inbound])
    assert len(hits) == 1, "$51 magnitude gap is outside the ±$50 tolerance — must fire"


# ---------------------------------------------------------------------------
# Spec test 6 — compound + cap behavior holds
# ---------------------------------------------------------------------------


def test_compound_floor_holds_for_many_unmatched() -> None:
    """Many unmatched legs all stay at the compound severity (40).

    Cap is 60 — the compound floor 40 is well under it. Verify the
    compound rule REPLACES per-instance scaling at the n>=3 threshold
    rather than adding to it (otherwise we'd see 25*n quickly exceeding
    60).
    """
    outs = [
        _txn(
            posted_date=date(2026, 1, day),
            description=f"TRANSFER TO HIDDEN {day}",
            amount=Decimal("-2000.00"),
            category="transfer",
            source_line=i + 1,
        )
        # 5 unmatched legs, well above the compound threshold
        for i, day in enumerate((3, 9, 15, 21, 27))
    ]
    hits = detect_unreconciled_internal_transfers(outs, outs)
    assert len(hits) == 5
    for h in hits:
        assert h.severity == 40, (
            "5 unmatched should stay at compound floor 40, not scale "
            f"to 25*5=125 (cap 60); got {h.severity}"
        )
        # And never exceeds the cap.
        assert h.severity <= 60


def test_two_unmatched_uses_per_instance_scaling() -> None:
    """Below the compound threshold, severity scales 25*n.

    At n=2 the result is 50 — per-instance scaling, not yet at the
    compound floor.
    """
    outs = [
        _txn(
            posted_date=date(2026, 1, day),
            description=f"WIRE TO COUNTERPARTY {day}",
            amount=Decimal("-3000.00"),
            category="wire_out",
            source_line=i + 1,
        )
        for i, day in enumerate((6, 20))
    ]
    hits = detect_unreconciled_internal_transfers(outs, outs)
    assert len(hits) == 2
    for h in hits:
        assert h.severity == 50, "2 unmatched should scale to 25*2=50, got {h.severity}"


# ---------------------------------------------------------------------------
# Bundle-scope check — the match-in lives on a different statement
# ---------------------------------------------------------------------------


def test_match_in_different_statement_clears_the_flag() -> None:
    """Operator emphasized 'anywhere in the submitted bundle'.

    Same upload, two statements. Transfer-out on statement A, matching
    transfer-in on statement B. ``all_bundle_transactions`` is the
    union of both statements' rows; the matcher must see the inbound
    on B and clear the flag.
    """
    statement_a_outs = [
        _txn(
            posted_date=date(2026, 1, 10),
            description="TRANSFER TO CHK 9999",
            amount=Decimal("-8000.00"),
            category="transfer",
        )
    ]
    statement_b_ins = [
        _txn(
            posted_date=date(2026, 1, 11),
            description="TRANSFER FROM CHK 7777",
            amount=Decimal("8000.00"),
            category="transfer",
        )
    ]
    bundle = statement_a_outs + statement_b_ins
    hits = detect_unreconciled_internal_transfers(statement_a_outs, bundle)
    assert hits == [], (
        "bundle-scope match-in must clear the flag even when the "
        "inbound row lives on a different statement"
    )


# ---------------------------------------------------------------------------
# own_account classifier path
# ---------------------------------------------------------------------------


def test_own_account_classifier_id_fires_without_description_token() -> None:
    """OR-branch: classifier flagging row as own_account fires even
    without a TRANSFER TO / WIRE TO / ACH TO / ZELLE TO description
    token.
    """
    # Description has none of the token strings — match must come from
    # the own_account_ids set instead.
    out = _txn(
        posted_date=date(2026, 1, 10),
        description="ONLINE BANKING DEBIT MEMO 1234",
        amount=Decimal("-2500.00"),
        category="transfer",
    )
    hits = detect_unreconciled_internal_transfers([out], [out], own_account_ids={out.id})
    assert len(hits) == 1
    assert hits[0].source_ids == [out.id]
    assert hits[0].severity == 25


def test_no_own_account_id_and_no_token_match_does_not_fire() -> None:
    """Sanity: a debit that is neither classified own_account NOR has a
    matching description token MUST NOT fire.
    """
    out = _txn(
        posted_date=date(2026, 1, 10),
        description="ONLINE BANKING DEBIT MEMO 1234",
        amount=Decimal("-2500.00"),
        category="transfer",
    )
    hits = detect_unreconciled_internal_transfers([out], [out])
    assert hits == []


# ---------------------------------------------------------------------------
# Shadow-mode discipline: FRAUD_WEIGHTS carve-out + fraud_score
# unaffected + parse_status path unchanged
# ---------------------------------------------------------------------------


def test_shadow_weight_is_zero_in_fraud_weights() -> None:
    """``FRAUD_WEIGHTS["shadow_unreconciled_internal_transfer"]`` must
    be 0.0 so the detector cannot accidentally move ``fraud_score``.
    """
    assert "shadow_unreconciled_internal_transfer" in FRAUD_WEIGHTS
    assert FRAUD_WEIGHTS["shadow_unreconciled_internal_transfer"] == 0.0


def test_shadow_detector_does_not_alter_fraud_score() -> None:
    """Run ``analyze_patterns`` on a set of unmatched transfer-outs:
    shadow flag fires, but ``patterns.fraud_score`` is the sum of the
    LIVE ``patterns`` severities only — the shadow row must contribute
    zero.
    """
    outs = [
        _txn(
            posted_date=date(2026, 1, day),
            description=f"TRANSFER TO HIDDEN {day}",
            amount=Decimal("-2000.00"),
            category="transfer",
            source_line=i + 1,
        )
        for i, day in enumerate((5, 12, 20))
    ]
    pa = _analyze(outs)
    # The new shadow detector fires (3 unmatched legs).
    shadow_hits = _shadow_uit(pa)
    assert len(shadow_hits) == 3
    assert all(h.severity == 40 for h in shadow_hits)

    # The fraud_score sums ONLY live patterns. The live
    # ``unreconciled_internal_transfer`` detector (different code path,
    # tighter tolerances) also fires on this fixture — that's expected
    # and orthogonal. What we assert here is the SHADOW detector's
    # severity (3 * 40 = 120) is NOT in the score.
    live_severity_sum = sum(p.severity for p in pa.patterns)
    assert pa.fraud_score == min(100, live_severity_sum), (
        "fraud_score must equal capped sum of LIVE pattern severities "
        "only — shadow patterns must not contribute"
    )
    # Sanity: fraud_score cannot be inflated by the shadow severity.
    shadow_severity_sum = sum(h.severity for h in shadow_hits)
    assert shadow_severity_sum > 0  # 120
    # If the shadow severity had leaked in, fraud_score would be higher
    # than the live-only ceiling. Confirm it isn't.
    assert pa.fraud_score <= live_severity_sum or pa.fraud_score == 100
