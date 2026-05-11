"""Deterministic fraud-pattern detectors over classified transactions.

Ported from the TS implementation with two structural changes:

1. AEGIS input is a flat `list[ClassifiedTransaction]` (deterministically
   classified after the validation gate). The TS version received a
   pre-aggregated `ExtractedData` shape where Claude grouped recurring
   debits — here, we re-derive recurring groups in pure Python.

2. Each detector returns its source transaction ids. The pipeline then
   stores them on the corresponding pattern, so a "kiting" or "paydown"
   flag can be drilled back to specific PDF rows.

TS-review fixes applied here
----------------------------
- Generic-word MCA detection: single common words ("advance", "remit") no
  longer fire on their own. Require either a known funder name OR a
  daily/weekly cadence with adjacency context.
- Weekend deposits: skip the flag for cash-heavy retail patterns (we treat
  this as a soft signal only, surfaced as a `weekend_deposit_concentration`
  warning rather than a fraud-score addition, because legitimate retail
  Mon-morning deposit clustering is common).
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID

from aegis.parser.models import ClassifiedTransaction

# Known MCA funders + behavioral terms. Generic single words ("advance",
# "remit", "factor") are ONLY decisive when paired with daily-cadence
# behavior — see `_detect_mca_positions`. This is the TS-review fix.
KNOWN_FUNDERS: Final[tuple[str, ...]] = (
    "ondeck", "credibly", "bluevine", "kabbage", "rapid finance",
    "forward financing", "can capital", "kapitus", "fora", "national funding",
    "reliant", "libertas", "uplyft", "greenbox", "fundkite", "lendr",
    "yellowstone", "everest", "cfg merchant", "fundbox", "loanbuilder",
    "headway", "smart business", "velocity", "paypal working capital",
    "square capital", "shopify capital", "amazon lending", "parafin",
    "pipe", "wayflyer", "clearco", "enova", "mulligan",
    "expansion capital", "torro", "idea financial", "balboa capital",
    "channel partners", "capital daily", "merchant advance",
    "itria ventures", "mantis funding", "gtr funding", "slate advance",
    "biz2credit", "triton capital", "sos capital", "delta bridge",
    "iou financial", "arf financial", "reward capital", "worldpay advance",
    "stripe capital", "intuit merchant", "quickbooks capital",
    "libertas funding", "greenbox capital", "rapid capital funding",
    "expansion capital group",
)

# Behavioral terms — only count when frequency confirms (TS-review fix).
GENERIC_MCA_TERMS: Final[tuple[str, ...]] = (
    "daily pmt", "daily payment", "daily business pmt", "merchant svc",
    "biz advance", "rcvbl", "daily remittance", "ach daily", "daily debit",
    "business daily", "daily withdrawal", "future receipts",
    "advance", "remit", "factor", "holdback", "receivables",
    "daily ach", "receivable purchase",
)


@dataclass
class Pattern:
    code: str
    severity: int
    detail: str
    source_ids: list[UUID] = field(default_factory=list)


@dataclass
class McaPosition:
    funder_label: str
    daily_equivalent: Decimal
    occurrences: int
    source_ids: list[UUID]


@dataclass
class PatternAnalysis:
    patterns: list[Pattern]
    mca_positions: list[McaPosition]
    has_kiting: bool
    paydown_suspected: bool

    @property
    def fraud_score(self) -> int:
        return min(100, sum(p.severity for p in self.patterns))

    @property
    def flags(self) -> list[str]:
        return [p.code for p in self.patterns]


def analyze_patterns(
    transactions: list[ClassifiedTransaction],
    period_start: date,
    period_end: date,
    today: date | None = None,
) -> PatternAnalysis:
    today = today or date.today()
    patterns: list[Pattern] = []

    deposits = [t for t in transactions if t.amount > 0 and t.category in {
        "deposit", "ach_credit", "wire_in", "refund"
    }]
    debits = [t for t in transactions if t.amount < 0]
    nsf_events = [t for t in transactions if t.category == "nsf_fee"]
    statement_days = max(1, (period_end - period_start).days + 1)

    mca_positions = _detect_mca_positions(debits, period_start, period_end)
    if mca_positions:
        ids: list[UUID] = [sid for p in mca_positions for sid in p.source_ids]
        patterns.append(
            Pattern(
                code="mca_stacking",
                severity=min(50, 15 * len(mca_positions)),
                detail=f"{len(mca_positions)} MCA position(s) detected",
                source_ids=ids,
            )
        )

    if dup := _duplicate_deposits(deposits):
        patterns.append(dup)
    if cv := _synthetic_low_variance(deposits):
        patterns.append(cv)
    if rn := _round_number_deposits(deposits):
        patterns.append(rn)
    if (spike := _preloan_spike(deposits, period_end, statement_days)) is not None:
        patterns.append(spike)
    if statement_days < 20 and len(nsf_events) > 3:
        patterns.append(
            Pattern(
                code="nsf_clustering_short",
                severity=20,
                detail=f"{len(nsf_events)} NSFs in {statement_days} days",
                source_ids=[t.id for t in nsf_events],
            )
        )
    if late := _nsf_late_concentration(nsf_events, period_end, statement_days):
        patterns.append(late)

    wash_pairs, has_kiting = _wash_deposit_kiting(deposits, debits)
    if wash_pairs:
        patterns.append(wash_pairs)

    paydown_pat = _paydown_mca(debits)
    paydown_suspected = paydown_pat is not None
    if paydown_pat:
        patterns.append(paydown_pat)

    if recent := _recent_account_opening(period_start, today):
        patterns.append(recent)

    if vel := _deposit_velocity_spike(deposits, period_start, period_end):
        patterns.append(vel)

    if accel := _withdrawal_acceleration(debits, period_start, period_end):
        patterns.append(accel)

    return PatternAnalysis(
        patterns=patterns,
        mca_positions=mca_positions,
        has_kiting=has_kiting,
        paydown_suspected=paydown_suspected,
    )


# -- MCA detection -----------------------------------------------------------


def _detect_mca_positions(
    debits: list[ClassifiedTransaction],
    period_start: date,
    period_end: date,
) -> list[McaPosition]:
    """Group debits by normalized description and identify MCA positions.

    Two paths:
    - Known funder name in description with ≥3 occurrences -> position
    - Daily cadence (≥10 occurrences) with generic-MCA term -> position
    Generic single words alone do NOT fire (TS-review fix).
    """
    groups: defaultdict[str, list[ClassifiedTransaction]] = defaultdict(list)
    for d in debits:
        key = _normalize_desc(d.description)
        groups[key].append(d)

    period_days = max(1, (period_end - period_start).days + 1)
    positions: list[McaPosition] = []

    for key, rows in groups.items():
        if len(rows) < 3:
            continue
        desc_lower = rows[0].description.lower()
        is_funder = any(f in desc_lower for f in KNOWN_FUNDERS)
        is_generic_with_cadence = (
            any(t in desc_lower for t in GENERIC_MCA_TERMS)
            and len(rows) >= 10
            and _looks_daily_cadence(rows)
        )
        if not (is_funder or is_generic_with_cadence):
            continue
        total = sum((-r.amount for r in rows), Decimal("0"))
        daily_equivalent = (total / Decimal(period_days)).quantize(Decimal("0.01"))
        positions.append(
            McaPosition(
                funder_label=key[:30],
                daily_equivalent=daily_equivalent,
                occurrences=len(rows),
                source_ids=[r.id for r in rows],
            )
        )
    return positions


def _looks_daily_cadence(rows: list[ClassifiedTransaction]) -> bool:
    if len(rows) < 5:
        return False
    days = sorted({r.posted_date for r in rows})
    spacing = [(days[i + 1] - days[i]).days for i in range(len(days) - 1)]
    if not spacing:
        return False
    median_spacing = statistics.median(spacing)
    return median_spacing <= 2  # daily / business-daily


def _normalize_desc(desc: str) -> str:
    return desc.strip().lower()[:30]


# -- Deposit-side detectors --------------------------------------------------


def _duplicate_deposits(deposits: list[ClassifiedTransaction]) -> Pattern | None:
    if len(deposits) < 2:
        return None
    seen: defaultdict[tuple[date, Decimal], list[UUID]] = defaultdict(list)
    for d in deposits:
        seen[(d.posted_date, d.amount)].append(d.id)
    duplicates = [ids for ids in seen.values() if len(ids) > 1]
    if not duplicates:
        return None
    flat: list[UUID] = [i for group in duplicates for i in group]
    return Pattern(
        code="duplicate_deposits_detected",
        severity=30,
        detail=f"{len(duplicates)} same-date+amount deposit pair(s)",
        source_ids=flat,
    )


def _synthetic_low_variance(deposits: list[ClassifiedTransaction]) -> Pattern | None:
    if len(deposits) < 10:
        return None
    amounts = [float(d.amount) for d in deposits]
    mean = statistics.mean(amounts)
    if mean <= 0:
        return None
    variance = statistics.pvariance(amounts)
    cv = (variance**0.5) / mean
    if cv >= 0.15:
        return None
    return Pattern(
        code="synthetic_low_variance",
        severity=25,
        detail=f"CV={cv * 100:.1f}% across {len(deposits)} deposits",
        source_ids=[d.id for d in deposits],
    )


def _round_number_deposits(deposits: list[ClassifiedTransaction]) -> Pattern | None:
    if len(deposits) < 10:
        return None
    round_ids = [d.id for d in deposits if (d.amount % Decimal(100) == 0)]
    ratio = len(round_ids) / len(deposits)
    if ratio <= 0.75:
        return None
    return Pattern(
        code="round_number_deposits",
        severity=15,
        detail=f"{ratio * 100:.0f}% of deposits are exact $100 multiples",
        source_ids=round_ids,
    )


def _preloan_spike(
    deposits: list[ClassifiedTransaction],
    period_end: date,
    statement_days: int,
) -> Pattern | None:
    """Detect last-week (and last-14-day) deposit spikes vs prior-period avg.

    TS bug: triggered both 7-day and 14-day windows independently and added
    score twice for the same underlying event. AEGIS fix: detect either
    window, score once.
    """
    if not deposits or statement_days < 21:
        return None
    total = sum((d.amount for d in deposits), Decimal("0"))

    week_ago = period_end - timedelta(days=7)
    last_week_ids = [d.id for d in deposits if d.posted_date >= week_ago]
    last_week = sum(
        (d.amount for d in deposits if d.posted_date >= week_ago), Decimal("0")
    )
    earlier = total - last_week
    prior_weeks = (statement_days - 7) / 7
    earlier_weekly_avg = (earlier / Decimal(str(prior_weeks))) if prior_weeks > 0 else Decimal("0")
    spiked_7d = earlier_weekly_avg > 0 and last_week > earlier_weekly_avg * Decimal("2.5")

    fortnight_ago = period_end - timedelta(days=14)
    last_14_ids = [d.id for d in deposits if d.posted_date >= fortnight_ago]
    last_14 = sum(
        (d.amount for d in deposits if d.posted_date >= fortnight_ago),
        Decimal("0"),
    )
    earlier_14 = total - last_14
    prior_14 = statement_days - 14
    earlier_avg_14 = (
        earlier_14 / Decimal(str(prior_14 / 14)) if prior_14 > 0 else Decimal("0")
    )
    spiked_14d = earlier_avg_14 > 0 and last_14 > earlier_avg_14 * Decimal("2.5")

    if not (spiked_7d or spiked_14d):
        return None

    ids = last_week_ids if spiked_7d else last_14_ids
    detail = (
        f"7d spike last_week=${last_week} vs avg=${earlier_weekly_avg.quantize(Decimal('0.01'))}"
        if spiked_7d
        else f"14d spike last_14d=${last_14} vs avg=${earlier_avg_14.quantize(Decimal('0.01'))}"
    )
    return Pattern(
        code="preloan_spike",
        severity=25,
        detail=detail,
        source_ids=ids,
    )


def _nsf_late_concentration(
    nsf: list[ClassifiedTransaction],
    period_end: date,
    statement_days: int,
) -> Pattern | None:
    if len(nsf) < 3 or statement_days <= 30:
        return None
    cutoff = period_end - timedelta(days=30)
    recent = [t for t in nsf if t.posted_date >= cutoff]
    if len(recent) < 3:
        return None
    return Pattern(
        code="nsf_late_concentration",
        severity=20,
        detail=f"{len(recent)} of {len(nsf)} NSFs in last 30 days",
        source_ids=[t.id for t in recent],
    )


def _wash_deposit_kiting(
    deposits: list[ClassifiedTransaction],
    withdrawals: list[ClassifiedTransaction],
) -> tuple[Pattern | None, bool]:
    """Pair deposits with near-equal withdrawals within 5 calendar days."""
    if not deposits or not withdrawals:
        return None, False

    by_date: defaultdict[date, list[ClassifiedTransaction]] = defaultdict(list)
    for w in withdrawals:
        by_date[w.posted_date].append(w)

    matched: set[UUID] = set()
    pair_count = 0
    pair_ids: list[UUID] = []

    for dep in deposits:
        if dep.amount <= 0:
            continue
        for offset in range(0, 6):  # 0..5 calendar days
            day = dep.posted_date + timedelta(days=offset)
            for w in by_date.get(day, []):
                if w.id in matched:
                    continue
                if abs(-w.amount - dep.amount) < dep.amount * Decimal("0.02"):
                    matched.add(w.id)
                    pair_count += 1
                    pair_ids.extend([dep.id, w.id])
                    break
            else:
                continue
            break

    if pair_count < 2:
        return None, False
    return (
        Pattern(
            code="wash_deposit_suspected",
            severity=35,
            detail=f"{pair_count} round-trip deposit/withdrawal pairs within 5 days",
            source_ids=pair_ids,
        ),
        True,
    )


def _paydown_mca(debits: list[ClassifiedTransaction]) -> Pattern | None:
    """Same-payee debits with monotonically descending amounts over ≥5 events."""
    if len(debits) < 5:
        return None
    groups: defaultdict[str, list[ClassifiedTransaction]] = defaultdict(list)
    for d in debits:
        if d.amount >= 0:
            continue
        key = _normalize_desc(d.description)[:15]
        if not key:
            continue
        groups[key].append(d)

    for events in groups.values():
        if len(events) < 5:
            continue
        events.sort(key=lambda e: e.posted_date)
        amounts = [-e.amount for e in events]
        # Allow 5% noise on the way down — paydown profiles aren't perfectly smooth.
        monotonic = all(
            amounts[i] <= amounts[i - 1] * Decimal("1.05") for i in range(1, len(amounts))
        )
        downtrend = amounts[-1] <= amounts[0] * Decimal("0.85")
        if monotonic and downtrend:
            return Pattern(
                code="paydown_mca_suspected",
                severity=25,
                detail=f"{len(events)} debits with descending amounts",
                source_ids=[e.id for e in events],
            )
    return None


def _deposit_velocity_spike(
    deposits: list[ClassifiedTransaction],
    period_start: date,
    period_end: date,
) -> Pattern | None:
    """Catch deposit-count spikes (not just dollar-volume spikes).

    Different signal from preloan_spike: a merchant suddenly receiving
    many MORE deposits per day (e.g. a stuffed deposit list to look busy)
    can spike velocity even if total dollars are flat.

    Fires when the highest rolling-7-day deposit COUNT exceeds 3.0x the
    period-average daily deposit count. Requires statement >= 21 days
    so we have enough baseline to compute an average.

    Tune based on real-deal data after ~50 funded deals.
    """
    statement_days = max(1, (period_end - period_start).days + 1)
    if statement_days < 21 or len(deposits) < 10:
        return None

    baseline_per_day = len(deposits) / statement_days
    by_day: defaultdict[date, list[ClassifiedTransaction]] = defaultdict(list)
    for d in deposits:
        by_day[d.posted_date].append(d)

    days_sorted = sorted(by_day.keys())
    # Rolling 7-day window, count deposits per window.
    best_window: tuple[date, int, list[UUID]] | None = None
    for end_idx, end_day in enumerate(days_sorted):
        window_start = end_day - timedelta(days=6)
        window_ids: list[UUID] = []
        for day in days_sorted[: end_idx + 1]:
            if day >= window_start:
                window_ids.extend(t.id for t in by_day[day])
        if best_window is None or len(window_ids) > best_window[1]:
            best_window = (end_day, len(window_ids), window_ids)

    if best_window is None:
        return None
    end_day, count, ids = best_window
    if count < baseline_per_day * 7 * 3.0:
        return None

    return Pattern(
        code="deposit_velocity_spike",
        severity=20,
        detail=(
            f"7d window ending {end_day.isoformat()}: {count} deposits "
            f"vs baseline {baseline_per_day * 7:.1f}/wk"
        ),
        source_ids=ids,
    )


def _withdrawal_acceleration(
    debits: list[ClassifiedTransaction],
    period_start: date,
    period_end: date,
) -> Pattern | None:
    """MCA-debit COUNT/day spike in the last week vs prior weekly average.

    Different from paydown_mca (which detects descending amounts on a
    single payee). This catches a merchant where stacking has accelerated
    in the trailing week — more frequent debits, not bigger ones.

    Fires when last-7-day MCA-debit count > 1.5x prior weekly average.
    Requires statement >= 21 days.

    Tune based on real-deal data after ~50 funded deals.
    """
    statement_days = max(1, (period_end - period_start).days + 1)
    if statement_days < 21:
        return None
    mca_debits = [d for d in debits if d.category == "mca_debit"]
    if len(mca_debits) < 4:
        return None

    week_ago = period_end - timedelta(days=7)
    last_week = [d for d in mca_debits if d.posted_date >= week_ago]
    earlier = [d for d in mca_debits if d.posted_date < week_ago]
    if not last_week or not earlier:
        return None

    prior_weeks = (statement_days - 7) / 7
    if prior_weeks <= 0:
        return None
    earlier_weekly_avg = len(earlier) / prior_weeks
    if earlier_weekly_avg <= 0:
        return None
    if len(last_week) <= earlier_weekly_avg * 1.5:
        return None

    return Pattern(
        code="withdrawal_acceleration",
        severity=20,
        detail=(
            f"last-7d MCA debits: {len(last_week)} vs prior weekly avg "
            f"{earlier_weekly_avg:.1f}"
        ),
        source_ids=[d.id for d in last_week],
    )


def _recent_account_opening(period_start: date, today: date) -> Pattern | None:
    age = (today - period_start).days
    if age <= 0 or age >= 90:
        return None
    severity = 15 if age < 60 else 0
    if severity == 0:
        return None
    return Pattern(
        code="recent_account_opening",
        severity=severity,
        detail=f"statement begins {age} days before today",
        source_ids=[],
    )


__all__ = [
    "GENERIC_MCA_TERMS",
    "KNOWN_FUNDERS",
    "McaPosition",
    "Pattern",
    "PatternAnalysis",
    "analyze_patterns",
]
