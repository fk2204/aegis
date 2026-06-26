"""Cashflow-signal computation for Track B.

Each public function in this module computes ONE signal from the
parse bundle + classifications. The functions are pure (no I/O) so
they're trivially unit-testable, and they live in their own module
so the band-computation logic stays small and readable.

Signals computed here:

* ``compute_period_days`` — span of the bundle in days.
* ``compute_monthly_revenue`` — normalised to 30 days.
* ``compute_running_balance_stats`` — ADB + lowest + negative days,
  or None when the running_balance field is sparse.
* ``compute_nsf_count`` — count of ``category='nsf_fee'`` rows.
* ``compute_mca_position_count`` — count of ``category='mca_debit'``
  rows; one row per MCA debit instance.
* ``compute_international_share_pct`` — share of revenue from
  international_client class. Pulled from the same aggregation so
  Track B and Track C agree on the number.

Statement-period normalisation uses transaction posted_date because
that's what we have directly from ``ClassifiedTransaction``. The
parse pipeline's ``StatementSummary.period_start`` /
``period_end`` would be more authoritative, but the band only needs
the bundle's span; this keeps Track B independent of the per-document
``AnalysisRow`` shape.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from aegis.money import Money
from aegis.parser.models import ClassifiedTransaction
from aegis.scoring_v2.aggregation import BundleAggregation

# At least 25% of rows must carry running_balance for the balance
# stats to be considered reliable. Below this threshold, the balance
# trace is too sparse to support ADB / lowest / negative days, and
# the signal surfaces as ``None`` (insufficient data) instead of a
# misleading fabricated number.
_RUNNING_BALANCE_COVERAGE_FLOOR: Decimal = Decimal("0.25")


def _flatten(
    transactions_by_doc: Mapping[str, list[ClassifiedTransaction]],
) -> list[ClassifiedTransaction]:
    return [t for txns in transactions_by_doc.values() for t in txns]


def compute_period_days(
    transactions_by_doc: Mapping[str, list[ClassifiedTransaction]],
) -> int:
    """Span of the bundle in days, computed from posted_date.

    Returns 0 for an empty bundle. Uses max-minus-min so overlapping
    months across accounts don't get double-counted.
    """
    flat = _flatten(transactions_by_doc)
    if not flat:
        return 0
    dates = [t.posted_date for t in flat]
    return (max(dates) - min(dates)).days + 1  # inclusive both ends


def compute_monthly_revenue(revenue_total: Money, period_days: int) -> Money:
    """Normalise total revenue to a 30-day-equivalent figure.

    Zero when ``period_days`` is zero (empty bundle). Uses
    Decimal arithmetic; no float coercion.
    """
    if period_days <= 0:
        return Money(Decimal("0"))
    raw = Decimal(str(revenue_total)) * Decimal("30") / Decimal(period_days)
    return Money(raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_running_balance_stats(
    transactions_by_doc: Mapping[str, list[ClassifiedTransaction]],
) -> tuple[Money | None, Money | None, int]:
    """Return (average_daily_balance, lowest_balance, negative_days).

    The first two are ``None`` when running_balance coverage is too
    sparse for a credible computation. ``negative_days`` is always
    an int (possibly 0) since it only counts days where we DO have
    a running_balance.
    """
    flat = _flatten(transactions_by_doc)
    if not flat:
        return None, None, 0
    with_balance = [t for t in flat if t.running_balance is not None]
    coverage = Decimal(len(with_balance)) / Decimal(len(flat)) if flat else Decimal("0")

    # Negative days are always counted from the rows we DO have.
    neg_days: set[date] = set()
    for t in with_balance:
        if t.running_balance is not None and Decimal(str(t.running_balance)) < 0:
            neg_days.add(t.posted_date)

    if coverage < _RUNNING_BALANCE_COVERAGE_FLOOR:
        return None, None, len(neg_days)

    # End-of-day balance per date: take the last (chronologically
    # latest in the day's transaction order) running_balance value.
    by_date: dict[date, Decimal] = {}
    by_date_order: dict[date, int] = defaultdict(int)
    for t in with_balance:
        if t.running_balance is None:
            continue
        idx = by_date_order[t.posted_date]
        # Take the latest running_balance seen for the date (assumes
        # the bundle preserves intra-day order via source_line).
        by_date[t.posted_date] = Decimal(str(t.running_balance))
        by_date_order[t.posted_date] = idx + 1

    if not by_date:
        return None, None, 0

    eod_values = list(by_date.values())
    adb_raw = sum(eod_values, start=Decimal("0")) / Decimal(len(eod_values))
    adb = Money(adb_raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    lowest = Money(min(eod_values))
    return adb, lowest, len(neg_days)


def compute_nsf_count(
    transactions_by_doc: Mapping[str, list[ClassifiedTransaction]],
) -> int:
    """Count transactions classified as ``nsf_fee`` by the parser."""
    return sum(1 for t in _flatten(transactions_by_doc) if t.category == "nsf_fee")


def compute_mca_position_count(
    transactions_by_doc: Mapping[str, list[ClassifiedTransaction]],
) -> int:
    """Count transactions classified as ``mca_debit`` by the parser.

    Each ``mca_debit`` row represents an active MCA debit pull (one
    instance of a daily/weekly remittance). This count is a coarse
    proxy for stacking — the underwriter cross-references against
    named funders to count distinct positions. Track B exposes the
    raw count; the band logic treats anything >= 1 as a concern and
    >= 3 as elevated.
    """
    return sum(1 for t in _flatten(transactions_by_doc) if t.category == "mca_debit")


def compute_mca_position_breakdown(
    transactions_by_doc: Mapping[str, list[ClassifiedTransaction]],
) -> tuple[int, int]:
    """Split the MCA-debit count into ``(confirmed, pattern)``.

    Walks every ``mca_debit`` row; ``confirmed`` counts those whose
    description contains a ``KNOWN_FUNDERS`` substring (named funder
    recognized — high confidence). ``pattern`` counts the rest — the
    LLM classified them as MCA but no named funder string is present,
    so the operator should verify before treating them as stacking.

    Total returned by ``compute_mca_position_count`` is the sum of the
    two; the buckets are exhaustive across ``mca_debit`` rows.

    Imported lazily from :mod:`aegis.parser.patterns` so the Track B
    layer doesn't take a hard dependency on the parser package at
    import time.
    """
    # Local import keeps the module dependency direction one-way
    # (track_b → parser at call time, not import time).
    from aegis.parser.patterns import KNOWN_FUNDERS

    confirmed = 0
    pattern = 0
    for t in _flatten(transactions_by_doc):
        if t.category != "mca_debit":
            continue
        desc_lower = t.description.lower()
        if any(f in desc_lower for f in KNOWN_FUNDERS):
            confirmed += 1
        else:
            pattern += 1
    return confirmed, pattern


def compute_international_share_pct(
    aggregation: BundleAggregation,
) -> Decimal:
    """Share of revenue from the ``international_client`` class.

    Reads the same aggregation Track C uses, so the number Track B
    surfaces in its reason ("89% international concentration") is
    identical to Track C's by_class share. Zero when revenue_total
    is zero.
    """
    revenue = Decimal(str(aggregation.revenue_total))
    if revenue <= Decimal("0"):
        return Decimal("0.00")
    intl = sum(
        (
            Decimal(str(r.incoming_total))
            for r in aggregation.by_class
            if r.counterparty == "international_client"
        ),
        start=Decimal("0"),
    )
    raw = (intl / revenue) * Decimal("100")
    return raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


__all__ = [
    "compute_international_share_pct",
    "compute_mca_position_breakdown",
    "compute_mca_position_count",
    "compute_monthly_revenue",
    "compute_nsf_count",
    "compute_period_days",
    "compute_running_balance_stats",
]
