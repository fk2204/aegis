"""Shadow-mode unreconciled-internal-transfer detector tests.

Covers ``detect_unreconciled_internal_transfers`` added to
``parser/patterns.py`` per operator spec 2026-06-24 (with same-day
operator follow-up corrections):

- Detects transfer-OUT rows (``own_account`` classification OR
  ``TRANSFER TO`` / ``WIRE TO`` / ``ACH TO`` / ``ZELLE TO`` description)
  with ``abs(amount) > $500`` that have no matching transfer-in within
  ``max($50, 0.1% * magnitude)`` / ±5 days anywhere in the submitted
  bundle.
- Severity: monotonic ramp ``min(60, 25 + (n - 1) * 10)``. n=1 → 25;
  n=2 → 35; n=3 → 45; n=4 → 55; n=5+ → 60 (cap). No drop at any n.
- Shadow-mode: emits to ``PatternAnalysis.shadow_patterns``, code
  ``unreconciled_internal_transfer_v2`` (``_v2`` suffix disambiguates
  from the live ``unreconciled_internal_transfer``), severity-0
  carve-out via
  ``FRAUD_WEIGHTS["shadow_unreconciled_internal_transfer_v2"] == 0``.
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

SHADOW_CODE = "unreconciled_internal_transfer_v2"
SHADOW_WEIGHT_KEY = "shadow_unreconciled_internal_transfer_v2"


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
    return [p for p in pa.shadow_patterns if p.code == SHADOW_CODE]


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
    assert hits[0].code == SHADOW_CODE
    assert hits[0].severity == 25
    assert hits[0].source_ids == [out.id]
    # Detail surfaces counterparty + amount per spec.
    assert "$7500.00" in hits[0].detail
    assert "HIDDEN ACCT 9940" in hits[0].detail


# ---------------------------------------------------------------------------
# Spec test 3 — 3 unmatched -> severity 45 (monotonic ramp, n=3)
# ---------------------------------------------------------------------------


def test_three_unmatched_transfer_outs_severity_45_ramp() -> None:
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
        assert h.severity == 45, f"3 unmatched should ramp to 25 + (3-1)*10 = 45, got {h.severity}"
        assert h.code == SHADOW_CODE
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
    # 5 days later (within the ±5d window). Tolerance on $10k is
    # max($50, $10) = $50, so a $25 gap is well inside.
    inbound = _txn(
        posted_date=date(2026, 1, 15),
        description="WIRE FROM BROKERAGE",
        amount=Decimal("9975.00"),  # $25 less, within $50 floor
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


def test_amount_outside_50_floor_fires_on_small_transfer() -> None:
    """Floor branch: $5k transfer, tolerance = max($50, $5) = $50.

    $51 gap is outside the floor — must fire.
    """
    out = _txn(
        posted_date=date(2026, 1, 10),
        description="ACH TO SAVINGS",
        amount=Decimal("-5000.00"),
        category="transfer",
    )
    inbound = _txn(
        posted_date=date(2026, 1, 11),
        description="ACH FROM CHK",
        amount=Decimal("4949.00"),  # $51 difference, just outside the $50 floor
        category="ach_credit",
    )
    hits = detect_unreconciled_internal_transfers([out, inbound], [out, inbound])
    assert len(hits) == 1, "$51 magnitude gap is outside the $50 floor — must fire"


# ---------------------------------------------------------------------------
# Proportional-tolerance branch (operator correction 2026-06-24)
# ---------------------------------------------------------------------------


def test_large_wire_tolerance_scales_with_magnitude() -> None:
    """0.1% branch: $100k transfer, tolerance = max($50, $100) = $100.

    A $90 gap is INSIDE the proportional tolerance — must clear the flag.
    With the previous fixed-$50 tolerance, this would have manufactured a
    false positive on a routine wire fee.
    """
    out = _txn(
        posted_date=date(2026, 1, 10),
        description="WIRE TO TREASURY SWEEP",
        amount=Decimal("-100000.00"),
        category="wire_out",
    )
    inbound = _txn(
        posted_date=date(2026, 1, 11),
        description="WIRE FROM TREASURY",
        amount=Decimal("99910.00"),  # $90 less — inside the $100 proportional tolerance
        category="wire_in",
    )
    hits = detect_unreconciled_internal_transfers([out, inbound], [out, inbound])
    assert hits == [], "tolerance on a $100k transfer must scale to $100; $90 gap should clear"


def test_large_wire_outside_proportional_tolerance_fires() -> None:
    """Boundary on the proportional branch: $100k transfer, tolerance $100.

    A $101 gap is outside the proportional tolerance — must fire.
    """
    out = _txn(
        posted_date=date(2026, 1, 10),
        description="WIRE TO TREASURY SWEEP",
        amount=Decimal("-100000.00"),
        category="wire_out",
    )
    inbound = _txn(
        posted_date=date(2026, 1, 11),
        description="WIRE FROM TREASURY",
        amount=Decimal("99899.00"),  # $101 less — outside the $100 proportional tolerance
        category="wire_in",
    )
    hits = detect_unreconciled_internal_transfers([out, inbound], [out, inbound])
    assert len(hits) == 1, "$101 gap on a $100k wire is outside the proportional tolerance"
    # Detail string should surface the per-row tolerance, not a global constant.
    assert "$100.00" in hits[0].detail


# ---------------------------------------------------------------------------
# Severity ramp boundaries (monotonic, no drop at any n)
# ---------------------------------------------------------------------------


def test_severity_ramp_caps_at_60_for_many_unmatched() -> None:
    """5+ unmatched legs cap at 60 (raw ramp would yield 65 at n=5)."""
    outs = [
        _txn(
            posted_date=date(2026, 1, day),
            description=f"TRANSFER TO HIDDEN {day}",
            amount=Decimal("-2000.00"),
            category="transfer",
            source_line=i + 1,
        )
        for i, day in enumerate((3, 9, 15, 21, 27))
    ]
    hits = detect_unreconciled_internal_transfers(outs, outs)
    assert len(hits) == 5
    for h in hits:
        assert h.severity == 60, (
            f"5 unmatched at the cap: min(60, 25 + 4*10) = 60; got {h.severity}"
        )


def test_two_unmatched_uses_ramp_value_35() -> None:
    """At n=2 the ramp yields 25 + (2-1)*10 = 35. No drop, no compound floor."""
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
        assert h.severity == 35, f"2 unmatched ramps to 35, got {h.severity}"


def test_severity_ramp_is_monotonic_across_counts() -> None:
    """severity(n+1) >= severity(n) for every n in [1, 10] — the
    invariant the ramp design exists to guarantee.
    """
    from aegis.parser.patterns import _unreconciled_transfer_severity

    prev = -1
    for n in range(1, 11):
        s = _unreconciled_transfer_severity(n)
        assert s >= prev, f"severity({n}) = {s} dropped below severity({n - 1}) = {prev}"
        prev = s
    assert _unreconciled_transfer_severity(10) == 60


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
    """``FRAUD_WEIGHTS["shadow_unreconciled_internal_transfer_v2"]`` must
    be 0.0 so the detector cannot accidentally move ``fraud_score``.
    """
    assert SHADOW_WEIGHT_KEY in FRAUD_WEIGHTS
    assert FRAUD_WEIGHTS[SHADOW_WEIGHT_KEY] == 0.0


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
    # The new shadow detector fires (3 unmatched legs, ramp value 45).
    shadow_hits = _shadow_uit(pa)
    assert len(shadow_hits) == 3
    assert all(h.severity == 45 for h in shadow_hits)

    # The fraud_score sums ONLY live patterns. The live
    # ``unreconciled_internal_transfer`` detector (different code path,
    # tighter tolerances) also fires on this fixture — that's expected
    # and orthogonal. What we assert here is the SHADOW detector's
    # severity (3 * 45 = 135) is NOT in the score.
    live_severity_sum = sum(p.severity for p in pa.patterns)
    assert pa.fraud_score == min(100, live_severity_sum), (
        "fraud_score must equal capped sum of LIVE pattern severities "
        "only — shadow patterns must not contribute"
    )
    # Sanity: shadow severity must not be in the score.
    shadow_severity_sum = sum(h.severity for h in shadow_hits)
    assert shadow_severity_sum > 0  # 135
    assert pa.fraud_score <= live_severity_sum or pa.fraud_score == 100


# ---------------------------------------------------------------------------
# Counterparty allowlist (added 2026-06-27 after shadow audit — see
# ``_ALLOWLISTED_TRANSFER_COUNTERPARTIES`` block in patterns.py).
#
# Shadow audit (14d) showed 100% FP rate on this detector — every fire
# was a government/payroll wire (NYS Dept of Labor x3) or an own-account
# self-transfer. The allowlist excludes those counterparties BEFORE the
# matching loop so they never enter the unmatched-leg count.
# ---------------------------------------------------------------------------


def test_allowlist_nys_dept_of_labor_excluded() -> None:
    """NYS Dept of Labor wire (the exact pattern from the prod fires) →
    excluded, not flagged."""
    nys = _txn(
        posted_date=date(2026, 1, 10),
        description="CBUSOL TRANSFER DEBIT - WIRE TO NYS Dept of Labor",
        amount=Decimal("-2500.00"),
        category="wire_out",
    )
    hits = detect_unreconciled_internal_transfers([nys], [nys])
    assert hits == [], "NYS Dept of Labor wire must be allowlisted"


def test_allowlist_irs_payment_excluded() -> None:
    """IRS quarterly tax payment → excluded."""
    irs = _txn(
        posted_date=date(2026, 1, 15),
        description="WIRE TO IRS 9876543 TAX PAYMENT",
        amount=Decimal("-15000.00"),
        category="wire_out",
    )
    hits = detect_unreconciled_internal_transfers([irs], [irs])
    assert hits == [], "IRS wire must be allowlisted"


def test_allowlist_state_of_prefix_excluded() -> None:
    """'State of California Unemployment' → matches the 'state of '
    prefix → excluded."""
    state = _txn(
        posted_date=date(2026, 1, 20),
        description="ACH TO STATE OF CALIFORNIA UNEMPLOYMENT",
        amount=Decimal("-4500.00"),
        category="ach_credit",
    )
    hits = detect_unreconciled_internal_transfers([state], [state])
    assert hits == [], "State of … wire must be allowlisted"


def test_allowlist_own_account_transfer_excluded() -> None:
    """Self-transfer labeled 'own account' → excluded (the exact pattern
    from the prod self-transfer fire)."""
    own = _txn(
        posted_date=date(2026, 1, 25),
        description="TRANSFER TO OWN ACCOUNT US BANK CHK 7722",
        amount=Decimal("-3000.00"),
        category="transfer",
    )
    hits = detect_unreconciled_internal_transfers([own], [own])
    assert hits == [], "Own-account transfer must be allowlisted"


def test_allowlist_unknown_counterparty_still_fires_regression_guard() -> None:
    """Regression guard: a genuinely-unmatched transfer to an unknown
    counterparty must STILL fire. The allowlist is opt-in by token, not
    opt-out by default."""
    suspicious = _txn(
        posted_date=date(2026, 1, 10),
        description="WIRE TO ACME OFFSHORE LLC ACCT 99887",
        amount=Decimal("-8000.00"),
        category="wire_out",
    )
    hits = detect_unreconciled_internal_transfers([suspicious], [suspicious])
    assert len(hits) == 1, "Unknown counterparty must still fire — allowlist is opt-in by token"
    assert hits[0].code == SHADOW_CODE
    assert hits[0].severity == 25  # n=1 on monotonic ramp


def test_allowlist_exclusion_count_in_detail_string() -> None:
    """When allowlisted transfers are excluded alongside a genuine
    flagged transfer, the emit's detail string includes the exclusion
    count as evidence of the allowlist firing."""
    nys = _txn(
        posted_date=date(2026, 1, 5),
        description="WIRE TO NYS Dept of Labor — UI WITHHOLDING",
        amount=Decimal("-1500.00"),
        category="wire_out",
    )
    irs = _txn(
        posted_date=date(2026, 1, 7),
        description="ACH TO IRS QUARTERLY TAX",
        amount=Decimal("-3000.00"),
        category="ach_credit",
    )
    suspicious = _txn(
        posted_date=date(2026, 1, 12),
        description="WIRE TO OFFSHORE ACCT 9988",
        amount=Decimal("-7500.00"),
        category="wire_out",
    )
    hits = detect_unreconciled_internal_transfers([nys, irs, suspicious], [nys, irs, suspicious])
    # Only the suspicious one fires; the two government wires were
    # excluded by the allowlist BEFORE the matching loop.
    assert len(hits) == 1
    assert "OFFSHORE ACCT 9988" in hits[0].detail
    # Detail surfaces the exclusion count.
    assert "2 transfer(s) excluded" in hits[0].detail
    assert "government/self-transfer allowlist" in hits[0].detail


def test_allowlist_case_insensitive_substring_match() -> None:
    """Allowlist tokens are matched case-insensitive substring against
    the lowercased description. Mixed-case bank rendering must still
    match."""
    mixed = _txn(
        posted_date=date(2026, 1, 8),
        description="Wire To Workers Comp Insurance Premium",
        amount=Decimal("-2200.00"),
        category="wire_out",
    )
    hits = detect_unreconciled_internal_transfers([mixed], [mixed])
    assert hits == [], "Mixed-case 'Workers Comp' must match the lowercase allowlist token"
