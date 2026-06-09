"""R1.8 — NSF secondary validation pass (shadow).

Today ``parser/classify.py`` is the sole authority on whether a row is an
``nsf_fee``. The LLM mislabeling a "FEE -50" row, or labeling a benign
maintenance fee as NSF, has no downstream check. R1.8 introduces
``secondary_validate_nsf`` — a deterministic pass that flags NSF rows
lacking corroborating evidence (negative running-balance day or co-located
ACH return / reversal / chargeback) AND independently flags low-confidence
NSF rows.

Per CLAUDE.md decision-boundary shadow rule, this pass emits flags only;
it does NOT change ``parse_status`` and does NOT relabel rows. Operator
validates against corpus, then flips routing via config in a follow-up
commit.

Test surface:

* corroborated cases — negative-balance day, co-located return, co-located
  chargeback, day-1 reversal — emit no corroboration flag
* uncorroborated case — positive balance, no return / reversal anywhere
  in the window — emits ``nsf_corroboration_missing:...``
* low-confidence case — confidence < 80 emits ``nsf_low_confidence:...``
  regardless of corroboration
* high-confidence corroborated case — no flags at all
* edge cases: no NSF rows in statement, mixed NSF rows with different
  outcomes, single row firing BOTH flags simultaneously
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from aegis.parser.models import ClassifiedTransaction, TransactionCategory
from aegis.parser.nsf_secondary import (
    NSFValidationIssue,
    secondary_validate_nsf,
)

PERIOD_START = date(2026, 3, 1)
PERIOD_END = date(2026, 3, 31)


def _txn(
    *,
    amount: Decimal,
    category: TransactionCategory,
    description: str = "row",
    posted: date = date(2026, 3, 15),
    running_balance: Decimal | None = None,
    classification_confidence: int = 95,
) -> ClassifiedTransaction:
    """Build a ClassifiedTransaction with sensible defaults."""
    return ClassifiedTransaction(
        id=uuid4(),
        posted_date=posted,
        description=description,
        amount=amount,
        running_balance=running_balance,
        source_page=1,
        source_line=1,
        category=category,
        classification_confidence=classification_confidence,
    )


def _run(
    txns: list[ClassifiedTransaction],
    *,
    beginning_balance: Decimal = Decimal("5000.00"),
) -> list[NSFValidationIssue]:
    return secondary_validate_nsf(
        txns,
        beginning_balance=beginning_balance,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
    )


# -- corroboration paths -----------------------------------------------------


def test_nsf_on_negative_balance_day_emits_no_corroboration_flag() -> None:
    """Negative running-balance on the NSF day = corroborated → no flag."""
    issues = _run(
        [
            _txn(
                amount=Decimal("-35.00"),
                category="nsf_fee",
                description="NSF FEE",
                posted=date(2026, 3, 10),
                running_balance=Decimal("-120.50"),
            ),
        ],
        beginning_balance=Decimal("100.00"),
    )
    assert issues == [], (
        f"NSF on negative day should not fire corroboration flag; got {issues}"
    )


def test_nsf_on_positive_day_without_returns_fires_corroboration_flag() -> None:
    """Positive balance day, no return/reversal anywhere → fires flag."""
    issues = _run(
        [
            _txn(
                amount=Decimal("-35.00"),
                category="nsf_fee",
                description="NSF FEE",
                posted=date(2026, 3, 10),
                running_balance=Decimal("4965.00"),  # still positive
            ),
        ]
    )
    assert len(issues) == 1
    assert issues[0].kind == "corroboration_missing"
    assert "nsf_corroboration_missing" in issues[0].flag_text
    assert "2026-03-10" in issues[0].flag_text
    assert "$35.00" in issues[0].flag_text
    assert "would_route_review" in issues[0].flag_text


def test_same_day_chargeback_corroborates_nsf() -> None:
    """A chargeback on the SAME day as the NSF = corroborated."""
    issues = _run(
        [
            _txn(
                amount=Decimal("-35.00"),
                category="nsf_fee",
                description="FEE",
                posted=date(2026, 3, 10),
                running_balance=Decimal("4965.00"),
            ),
            _txn(
                amount=Decimal("-250.00"),
                category="chargeback",
                description="MERCHANT CHARGEBACK",
                posted=date(2026, 3, 10),
            ),
        ]
    )
    assert issues == []


def test_day_before_ach_return_corroborates_nsf() -> None:
    """A row with 'RETURN' on day-1 corroborates a next-day NSF fee."""
    issues = _run(
        [
            _txn(
                amount=Decimal("-150.00"),
                category="ach_credit",
                description="ACH RETURN INSUFFICIENT FUNDS",
                posted=date(2026, 3, 9),
            ),
            _txn(
                amount=Decimal("-35.00"),
                category="nsf_fee",
                description="OD FEE",
                posted=date(2026, 3, 10),
                running_balance=Decimal("4815.00"),
            ),
        ]
    )
    assert issues == []


def test_same_day_reversal_corroborates_nsf() -> None:
    """'REVERSAL' in any same-day row's description corroborates."""
    issues = _run(
        [
            _txn(
                amount=Decimal("-35.00"),
                category="nsf_fee",
                description="FEE",
                posted=date(2026, 3, 10),
                running_balance=Decimal("4965.00"),
            ),
            _txn(
                amount=Decimal("-200.00"),
                category="other",
                description="ACH DEBIT REVERSAL",
                posted=date(2026, 3, 10),
            ),
        ]
    )
    assert issues == []


def test_distant_return_does_not_corroborate() -> None:
    """A return 2 days before falls outside the 1-day window → no corroboration."""
    issues = _run(
        [
            _txn(
                amount=Decimal("-150.00"),
                category="ach_credit",
                description="ACH RETURN INSUFFICIENT FUNDS",
                posted=date(2026, 3, 8),  # 2 days before
            ),
            _txn(
                amount=Decimal("-35.00"),
                category="nsf_fee",
                description="OD FEE",
                posted=date(2026, 3, 10),
                running_balance=Decimal("4815.00"),
            ),
        ]
    )
    assert len(issues) == 1
    assert issues[0].kind == "corroboration_missing"


# -- low-confidence path -----------------------------------------------------


def test_low_confidence_nsf_fires_independently_of_corroboration() -> None:
    """Confidence=60 NSF fires the low-confidence flag even when corroborated.

    Two independent signals: corroboration and confidence. Either failure
    surfaces a flag. The negative-balance day suppresses the corroboration
    flag but NOT the confidence flag.
    """
    issues = _run(
        [
            _txn(
                amount=Decimal("-35.00"),
                category="nsf_fee",
                description="NSF FEE",
                posted=date(2026, 3, 10),
                running_balance=Decimal("-50.00"),  # negative = corroborated
                classification_confidence=60,
            ),
        ],
        beginning_balance=Decimal("100.00"),
    )
    assert len(issues) == 1
    assert issues[0].kind == "low_confidence"
    assert "nsf_low_confidence" in issues[0].flag_text
    assert "conf60" in issues[0].flag_text
    assert "would_route_review" in issues[0].flag_text


def test_high_confidence_on_negative_balance_day_no_flags() -> None:
    """conf=90, day-was-negative — clean row, no flags."""
    issues = _run(
        [
            _txn(
                amount=Decimal("-35.00"),
                category="nsf_fee",
                description="NSF FEE",
                posted=date(2026, 3, 10),
                running_balance=Decimal("-120.00"),
                classification_confidence=90,
            ),
        ],
        beginning_balance=Decimal("100.00"),
    )
    assert issues == []


def test_confidence_at_floor_does_not_fire() -> None:
    """Confidence == 80 is at the floor; only confidence < 80 fires."""
    issues = _run(
        [
            _txn(
                amount=Decimal("-35.00"),
                category="nsf_fee",
                description="NSF FEE",
                posted=date(2026, 3, 10),
                running_balance=Decimal("-50.00"),
                classification_confidence=80,
            ),
        ],
        beginning_balance=Decimal("100.00"),
    )
    assert issues == []


def test_confidence_just_below_floor_fires() -> None:
    """Confidence == 79 fires the low_confidence flag."""
    issues = _run(
        [
            _txn(
                amount=Decimal("-35.00"),
                category="nsf_fee",
                description="NSF FEE",
                posted=date(2026, 3, 10),
                running_balance=Decimal("-50.00"),
                classification_confidence=79,
            ),
        ],
        beginning_balance=Decimal("100.00"),
    )
    assert len(issues) == 1
    assert issues[0].kind == "low_confidence"
    assert "conf79" in issues[0].flag_text


# -- both flags simultaneously ----------------------------------------------


def test_uncorroborated_low_confidence_fires_both_flags() -> None:
    """A single row hits both checks — both flags appear, kind distinguishes."""
    issues = _run(
        [
            _txn(
                amount=Decimal("-35.00"),
                category="nsf_fee",
                description="MAINTENANCE FEE",
                posted=date(2026, 3, 10),
                running_balance=Decimal("4965.00"),  # positive
                classification_confidence=55,  # < 80
            ),
        ]
    )
    kinds = sorted(issue.kind for issue in issues)
    assert kinds == ["corroboration_missing", "low_confidence"]


# -- empty / negative cases --------------------------------------------------


def test_no_nsf_rows_returns_empty() -> None:
    """Statement with no NSF classifications returns an empty list."""
    issues = _run(
        [
            _txn(
                amount=Decimal("1000.00"),
                category="deposit",
                description="ACH DEPOSIT",
                posted=date(2026, 3, 10),
            ),
            _txn(
                amount=Decimal("-50.00"),
                category="fee",
                description="MAINTENANCE FEE",
                posted=date(2026, 3, 15),
            ),
        ]
    )
    assert issues == []


def test_inverted_period_returns_empty() -> None:
    """period_end before period_start is degenerate — return empty defensively."""
    txns = [
        _txn(
            amount=Decimal("-35.00"),
            category="nsf_fee",
            description="NSF FEE",
            posted=date(2026, 3, 10),
        ),
    ]
    out = secondary_validate_nsf(
        txns,
        beginning_balance=Decimal("100.00"),
        period_start=date(2026, 3, 31),
        period_end=date(2026, 3, 1),
    )
    assert out == []


# -- structural assertions ---------------------------------------------------


def test_issue_carries_source_id_and_date() -> None:
    """Every issue carries the originating row's id + date for audit drill-down."""
    nsf = _txn(
        amount=Decimal("-35.00"),
        category="nsf_fee",
        description="MAINT FEE",
        posted=date(2026, 3, 10),
        running_balance=Decimal("4965.00"),
        classification_confidence=50,
    )
    issues = _run([nsf])
    assert len(issues) == 2
    for issue in issues:
        assert issue.source_id == str(nsf.id)
        assert issue.posted_date == date(2026, 3, 10)


def test_description_with_commas_does_not_break_flag_text() -> None:
    """Commas in descriptions get neutralized so the flag is readable."""
    issues = _run(
        [
            _txn(
                amount=Decimal("-35.00"),
                category="nsf_fee",
                description="WIRE FEE, OD CHARGE",
                posted=date(2026, 3, 10),
                running_balance=Decimal("4965.00"),
            ),
        ]
    )
    assert len(issues) == 1
    # commas replaced with spaces; flag text contains the snippet without commas
    assert "," not in issues[0].flag_text.split("_", 4)[4]
