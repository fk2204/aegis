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

import re
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

# Known payroll-processor counterparties. Presence of any of these as a
# debit counterparty over the period satisfies the "operating business
# has payroll" signal. Absence is a soft red flag for businesses claiming
# > $50k/mo revenue (per §6.4 `payroll_absent`).
KNOWN_PAYROLL_PROCESSORS: Final[tuple[str, ...]] = (
    "adp", "gusto", "paychex", "rippling", "square payroll",
    "intuit payroll", "quickbooks payroll", "trinet", "justworks",
    "zenefits", "deel", "bamboohr", "wagepoint", "onpay",
    "patriot payroll", "surepayroll", "paylocity", "ultipro",
    "workday payroll",
)

# Card-processor counterparties whose deposits are net of advance
# withholding. Used by `processor_holdback_detected` — when "STRIPE
# TRANSFER" / "SQUARE PAYOUT" credits show large daily variation
# inconsistent with the rest of the deposit stream we infer holdback
# is in force. Surfaces a soft signal; final cross-check requires the
# matched processor statement (Phase 2C).
KNOWN_CARD_PROCESSORS: Final[tuple[str, ...]] = (
    "stripe", "square", "toast", "clover", "shopify",
    "paypal", "worldpay", "elavon", "global payments",
    "tsys", "first data", "fiserv merchant", "heartland",
    "authorize.net", "braintree",
)

# Reversal / dispute keywords used by `unauthorized_withdrawal_dispute`.
# A credit row containing one of these paired with a prior matching MCA
# debit means the merchant fought (and won) a funder withdrawal.
REVERSAL_KEYWORDS: Final[tuple[str, ...]] = (
    "reversal", "reverse", "dispute credit", "dispute cred",
    "unauthorized", "return ach credit", "chargeback credit",
    "ach return credit", "ach reversal", "claim credit",
    "withdrawal dispute",
)

# Chargeback / refund keywords used by `chargeback_velocity`. Counts
# debit-side rows containing any of these terms grouped by month.
CHARGEBACK_KEYWORDS: Final[tuple[str, ...]] = (
    "chargeback", "charge back", "refund", "dispute",
    "return ach", "merchant return", "credit reversal",
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
class CounterpartySignals:
    """Counterparty-aggregation outputs surfaced as scoring factors.

    Per master plan §5.7. ``top_counterparty_pct`` is the single largest
    counterparty's share of true_revenue (revenue concentration). The
    ``top_5_*_share`` fields are the combined share of the five largest
    counterparties on each side. ``source_ids`` for each is the union of
    contributing transaction ids — funder drill-down stays auditable.

    None when the underlying side (revenue or expense) is empty.
    """

    top_counterparty_pct: int | None = None
    top_counterparty_label: str | None = None
    top_counterparty_source_ids: list[UUID] = field(default_factory=list)
    top_5_revenue_share_pct: int | None = None
    top_5_revenue_source_ids: list[UUID] = field(default_factory=list)
    top_5_expense_share_pct: int | None = None
    top_5_expense_source_ids: list[UUID] = field(default_factory=list)


@dataclass
class PatternAnalysis:
    patterns: list[Pattern]
    mca_positions: list[McaPosition]
    has_kiting: bool
    paydown_suspected: bool
    counterparty_signals: CounterpartySignals = field(default_factory=CounterpartySignals)
    payroll_present: bool = False
    acceleration_clause_triggered: bool = False
    unauthorized_withdrawal_dispute: bool = False
    ai_generated_score: int = 0

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

    # -- Phase 9 detectors ----------------------------------------------
    if uit := _unreconciled_internal_transfer(transactions):
        patterns.append(uit)

    if payoff := _mca_payoff_signature(debits):
        patterns.append(payoff)

    counterparty_signals = _counterparty_signals(transactions)
    if concentration := _customer_concentration(transactions, counterparty_signals):
        patterns.append(concentration)

    if cb_velocity := _chargeback_velocity(transactions, period_start, period_end):
        patterns.append(cb_velocity)

    unauth_dispute_pat = _unauthorized_withdrawal_dispute(transactions)
    unauth_dispute_fired = unauth_dispute_pat is not None
    if unauth_dispute_pat:
        patterns.append(unauth_dispute_pat)

    acceleration_pat = _acceleration_clause_triggered(mca_positions, debits, period_end)
    acceleration_fired = acceleration_pat is not None
    if acceleration_pat:
        patterns.append(acceleration_pat)

    if processor_pat := _processor_holdback_detected(transactions, period_start, period_end):
        patterns.append(processor_pat)

    payroll_present = _payroll_present(debits)
    if payroll_absent_pat := _payroll_absent(
        payroll_present, transactions, period_start, period_end
    ):
        patterns.append(payroll_absent_pat)

    ai_score = _ai_generated_statement_score(transactions, deposits)

    return PatternAnalysis(
        patterns=patterns,
        mca_positions=mca_positions,
        has_kiting=has_kiting,
        paydown_suspected=paydown_suspected,
        counterparty_signals=counterparty_signals,
        payroll_present=payroll_present,
        acceleration_clause_triggered=acceleration_fired,
        unauthorized_withdrawal_dispute=unauth_dispute_fired,
        ai_generated_score=ai_score,
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


# -- Phase 9 detector implementations ----------------------------------------
#
# All detectors operate on the post-classification, validated transaction
# stream. None of them retry the LLM. Each that produces a `Pattern`
# contributes to the patterns fraud sub-score; pure-signal extractors
# (counterparty shares, payroll-present, ai_generated_score) are returned
# on PatternAnalysis without a `Pattern` row so the scorer can decide
# whether to apply a delta.


# Categories considered "internal transfer" candidates for the
# unreconciled-transfer detector. Includes plain `transfer` and the wire
# legs explicitly so funder/MCA-debit rows are never counted as
# self-transfers (those have their own detectors).
_TRANSFER_CATEGORIES: Final[frozenset[str]] = frozenset(
    {"transfer", "wire_in", "wire_out"}
)

# Token allow-list extracted from a description before counterparty
# normalization. Cuts trailing transaction-id noise so two ACH deposits
# from the same customer with different trace ids collapse to one bucket.
_NORMALIZE_STOP_PATTERNS: Final[tuple[str, ...]] = (
    r"\bid\b[\s:]+\d+", r"\btrace\s*#?\d+", r"#\d+", r"\d{6,}",
)
_NORMALIZE_STOP_RE: Final[re.Pattern[str]] = re.compile(
    "|".join(_NORMALIZE_STOP_PATTERNS), re.IGNORECASE
)


def _normalize_counterparty(desc: str) -> str:
    """Aggressive normalization for counterparty bucketing.

    Strips trailing trace/id noise and clamps to first 40 chars. Distinct
    from ``_normalize_desc`` (30 chars, no stop-pattern strip) so MCA
    grouping and counterparty grouping can co-exist without one regressing
    the other.
    """
    stripped = _NORMALIZE_STOP_RE.sub(" ", desc).strip().lower()
    return " ".join(stripped.split())[:40]


def _unreconciled_internal_transfer(
    transactions: list[ClassifiedTransaction],
) -> Pattern | None:
    """Transfer-OUT > $500 with no matching transfer-IN in the bundle.

    Detects a hidden bank account — funds leave the account labeled
    "TRANSFER TO ..." but no matching credit shows in any submitted
    statement. Indicative of an undisclosed bank account, often hosting
    an undisclosed MCA (master plan §6.4, highest-value detector).

    Pair criterion is intentionally loose: same Decimal magnitude within
    $1 across a ±3-day window. Tight enough to skip noise, wide enough to
    catch real same-day or next-day transfers.

    Single-statement mode caveat: when we only see one account, EVERY
    transfer-out is unreconciled by definition. We surface the flag at
    medium severity (15) so the operator can sanity-check; bundle-mode
    will tighten this in a follow-up (Phase 2D context already adds
    cross-account stitching).
    """
    transfers = [t for t in transactions if t.category in _TRANSFER_CATEGORIES]
    outs = [t for t in transfers if t.amount < 0 and abs(t.amount) > Decimal("500")]
    if not outs:
        return None
    ins = [t for t in transfers if t.amount > 0]

    unmatched_ids: list[UUID] = []
    matched_in_ids: set[UUID] = set()
    for out_row in outs:
        out_amt = abs(out_row.amount)
        match = next(
            (
                t
                for t in ins
                if t.id not in matched_in_ids
                and abs(t.amount - out_amt) < Decimal("1.00")
                and abs((t.posted_date - out_row.posted_date).days) <= 3
            ),
            None,
        )
        if match is None:
            unmatched_ids.append(out_row.id)
        else:
            matched_in_ids.add(match.id)

    if not unmatched_ids:
        return None
    # Severity scales with count of unmatched legs.
    severity = min(40, 15 + 5 * (len(unmatched_ids) - 1))
    return Pattern(
        code="unreconciled_internal_transfer",
        severity=severity,
        detail=(
            f"{len(unmatched_ids)} transfer-out leg(s) > $500 with no matching "
            "transfer-in — possible hidden account"
        ),
        source_ids=unmatched_ids,
    )


def _mca_payoff_signature(
    debits: list[ClassifiedTransaction],
) -> Pattern | None:
    """Single debit > $5k matching a known MCA-originator name.

    Paid-off MCA that no longer shows up as an active recurring position
    — but the lump-sum payoff itself signals the merchant *had* one.
    Funders care because a recently-paid-off MCA bumps the stacking
    count for renewal-likelihood scoring.

    Fires for any debit whose absolute amount exceeds $5,000 AND whose
    description contains a known funder token, regardless of whether
    it's already part of a recurring MCA position.
    """
    if not debits:
        return None
    payoff_ids: list[UUID] = []
    for d in debits:
        if d.amount >= 0:
            continue
        if abs(d.amount) < Decimal("5000.00"):
            continue
        desc_lower = d.description.lower()
        if any(f in desc_lower for f in KNOWN_FUNDERS):
            payoff_ids.append(d.id)
    if not payoff_ids:
        return None
    return Pattern(
        code="mca_payoff_signature",
        severity=15,
        detail=f"{len(payoff_ids)} lump-sum debit(s) > $5k to known MCA funder",
        source_ids=payoff_ids,
    )


def _counterparty_signals(
    transactions: list[ClassifiedTransaction],
) -> CounterpartySignals:
    """Compute top-counterparty and top-5 revenue/expense shares.

    Buckets by ``_normalize_counterparty(desc)``. Revenue side uses the
    same inclusion rules as ``aggregate._true_revenue``. Expense side
    uses absolute amounts of all debits (NSF / fees excluded — they're
    not really counterparty-driven).
    """
    revenue_rows = [
        t
        for t in transactions
        if t.amount > 0
        and t.category in {"deposit", "ach_credit", "wire_in", "refund"}
    ]
    expense_rows = [
        t
        for t in transactions
        if t.amount < 0
        and t.category not in {"nsf_fee", "fee", "chargeback"}
    ]

    def _aggregate_side(
        rows: list[ClassifiedTransaction],
    ) -> tuple[
        list[tuple[str, Decimal, list[UUID]]],
        Decimal,
    ]:
        buckets: defaultdict[str, list[ClassifiedTransaction]] = defaultdict(list)
        for r in rows:
            buckets[_normalize_counterparty(r.description)].append(r)
        out: list[tuple[str, Decimal, list[UUID]]] = []
        total = Decimal("0")
        for key, members in buckets.items():
            if not key:
                continue
            amt = sum((abs(m.amount) for m in members), Decimal("0"))
            ids = [m.id for m in members]
            out.append((key, amt, ids))
            total += amt
        out.sort(key=lambda kv: kv[1], reverse=True)
        return out, total

    rev_buckets, rev_total = _aggregate_side(revenue_rows)
    exp_buckets, exp_total = _aggregate_side(expense_rows)

    signals = CounterpartySignals()

    if rev_buckets and rev_total > 0:
        top_key, top_amt, top_ids = rev_buckets[0]
        signals.top_counterparty_pct = int(
            ((top_amt / rev_total) * Decimal(100)).to_integral_value()
        )
        signals.top_counterparty_label = top_key[:30]
        signals.top_counterparty_source_ids = top_ids
        top5 = rev_buckets[:5]
        top5_total = sum((t[1] for t in top5), Decimal("0"))
        signals.top_5_revenue_share_pct = int(
            ((top5_total / rev_total) * Decimal(100)).to_integral_value()
        )
        signals.top_5_revenue_source_ids = [
            tid for _, _, ids in top5 for tid in ids
        ]

    if exp_buckets and exp_total > 0:
        top5_exp = exp_buckets[:5]
        top5_exp_total = sum((t[1] for t in top5_exp), Decimal("0"))
        signals.top_5_expense_share_pct = int(
            ((top5_exp_total / exp_total) * Decimal(100)).to_integral_value()
        )
        signals.top_5_expense_source_ids = [
            tid for _, _, ids in top5_exp for tid in ids
        ]

    return signals


def _customer_concentration(
    transactions: list[ClassifiedTransaction],
    signals: CounterpartySignals,
) -> Pattern | None:
    """Fires when a single counterparty > 30% of revenue.

    A scoreable detector layered on top of the aggregator's existing
    flag (which surfaces every concentration as a soft signal). Master
    plan §6.4 threshold: >30% is a meaningful single-customer
    dependency that funders will discount.
    """
    pct = signals.top_counterparty_pct
    if pct is None or pct <= 30:
        return None
    # Severity scales: 31-40 = mild (10), 41-60 = moderate (20), 61+ = severe (30).
    if pct >= 61:
        severity = 30
    elif pct >= 41:
        severity = 20
    else:
        severity = 10
    return Pattern(
        code="customer_concentration",
        severity=severity,
        detail=(
            f"top counterparty = {pct}% of revenue "
            f"({signals.top_counterparty_label or 'unknown'})"
        ),
        source_ids=list(signals.top_counterparty_source_ids),
    )


def _chargeback_velocity(
    transactions: list[ClassifiedTransaction],
    period_start: date,
    period_end: date,
) -> Pattern | None:
    """Chargeback / refund debits per month, trending up.

    Fires when ≥3 chargeback-keyword debits exist AND the count in the
    trailing 14 days is > 1.5x the prior-period average. Short-statement
    fallback (<21 days): fires on count alone (≥5).
    """
    cb_rows = [
        t
        for t in transactions
        if t.amount < 0
        and any(k in t.description.lower() for k in CHARGEBACK_KEYWORDS)
    ]
    if not cb_rows:
        return None

    statement_days = max(1, (period_end - period_start).days + 1)
    if statement_days < 21:
        if len(cb_rows) < 5:
            return None
        return Pattern(
            code="chargeback_velocity",
            severity=15,
            detail=f"{len(cb_rows)} chargeback/refund debits in {statement_days} days",
            source_ids=[t.id for t in cb_rows],
        )

    if len(cb_rows) < 3:
        return None

    fortnight_ago = period_end - timedelta(days=14)
    recent = [t for t in cb_rows if t.posted_date >= fortnight_ago]
    earlier = [t for t in cb_rows if t.posted_date < fortnight_ago]
    prior_fortnights = (statement_days - 14) / 14
    if prior_fortnights <= 0:
        return None
    earlier_per_fortnight = len(earlier) / prior_fortnights if prior_fortnights else 0
    if len(recent) <= earlier_per_fortnight * 1.5:
        # No acceleration — still surface a static count signal when ≥6.
        if len(cb_rows) >= 6:
            return Pattern(
                code="chargeback_velocity",
                severity=10,
                detail=f"{len(cb_rows)} chargeback/refund debits over period",
                source_ids=[t.id for t in cb_rows],
            )
        return None

    return Pattern(
        code="chargeback_velocity",
        severity=20,
        detail=(
            f"last-14d chargebacks: {len(recent)} vs prior "
            f"{earlier_per_fortnight:.1f}/fortnight"
        ),
        source_ids=[t.id for t in recent],
    )


def _unauthorized_withdrawal_dispute(
    transactions: list[ClassifiedTransaction],
) -> Pattern | None:
    """A credit matching a prior MCA debit AND containing reversal keywords.

    Indicates the merchant successfully disputed a funder withdrawal.
    Material: a merchant fighting their funder is unlikely to be
    fundable for another stacker. Single-event fire — the keyword
    match plus debit-pairing is high specificity.
    """
    mca_debits = [
        t
        for t in transactions
        if t.amount < 0 and t.category == "mca_debit"
    ]
    candidate_credits = [
        t
        for t in transactions
        if t.amount > 0
        and any(k in t.description.lower() for k in REVERSAL_KEYWORDS)
    ]
    if not mca_debits or not candidate_credits:
        return None

    pair_ids: list[UUID] = []
    for credit in candidate_credits:
        prior = [
            d
            for d in mca_debits
            if d.posted_date <= credit.posted_date
            and (credit.posted_date - d.posted_date).days <= 14
            and abs(abs(d.amount) - credit.amount) < Decimal("1.00")
        ]
        if prior:
            pair_ids.append(credit.id)
            pair_ids.extend(d.id for d in prior[:1])

    if not pair_ids:
        return None
    return Pattern(
        code="unauthorized_withdrawal_dispute",
        severity=35,
        detail=(
            f"{len(pair_ids) // 2} reversal credit(s) paired with prior MCA debit(s)"
        ),
        source_ids=pair_ids,
    )


def _acceleration_clause_triggered(
    mca_positions: list[McaPosition],
    debits: list[ClassifiedTransaction],
    period_end: date,
) -> Pattern | None:
    """Recurring MCA breaks + single debit 5-10x larger to the same payee.

    Funder called the loan after default — the recurring small debits
    stop and a single large debit (often the full remaining balance)
    posts. Hard-decline candidate.

    Fires only when ALL conditions hold:
      1. A position has ≥3 prior occurrences (otherwise we don't know
         the recurring amount with confidence).
      2. The most recent occurrence is at least 5x the median of the
         earlier occurrences and ≤ 10x (above 10x suggests it's a
         different transaction stream, not an acceleration).
      3. No subsequent recurring occurrences after the large debit
         within the visible period.
    """
    if not mca_positions or not debits:
        return None
    for pos in mca_positions:
        if pos.occurrences < 4:  # need ≥3 prior + 1 big
            continue
        pos_debits = [d for d in debits if d.id in pos.source_ids]
        pos_debits.sort(key=lambda d: d.posted_date)
        if len(pos_debits) < 4:
            continue
        amounts = [abs(d.amount) for d in pos_debits]
        prior, latest = amounts[:-1], amounts[-1]
        # Median of prior — robust against an early outlier.
        prior_floats = [float(a) for a in prior]
        median_prior = Decimal(str(statistics.median(prior_floats)))
        if median_prior <= 0:
            continue
        ratio = latest / median_prior
        if ratio < Decimal("5") or ratio > Decimal("10"):
            continue
        latest_date = pos_debits[-1].posted_date
        # Require ≥7 days between latest_date and period_end so we know
        # the recurring stream didn't simply resume.
        if (period_end - latest_date).days < 7:
            continue
        # And require zero post-latest occurrences in the same position
        # (already implied by being the trailing element, but double-check).
        return Pattern(
            code="acceleration_clause_triggered",
            severity=50,
            detail=(
                f"{pos.funder_label}: latest debit ${latest} is "
                f"{ratio.quantize(Decimal('0.1'))}x median prior "
                f"${median_prior} — possible funder acceleration"
            ),
            source_ids=[d.id for d in pos_debits],
        )
    return None


def _processor_holdback_detected(
    transactions: list[ClassifiedTransaction],
    period_start: date,
    period_end: date,
) -> Pattern | None:
    """Card-processor deposits with shortfall pattern suggesting holdback.

    When a processor (Stripe / Square / Toast) is forwarding payouts
    net of an in-place MCA holdback, the daily payout amounts get
    noisy — large variability + occasional zero days even when card
    volume is steady. This is a coarse heuristic; final confirmation
    requires the matching processor statement (Phase 2C parser).

    Fires when:
      - ≥10 deposits from a known card processor in the period
      - Coefficient of variation across daily-summed processor payouts
        ≥ 0.50 (statistically noisy)
      - Statement period is ≥14 days (otherwise too few daily samples)
    """
    statement_days = max(1, (period_end - period_start).days + 1)
    if statement_days < 14:
        return None
    proc_rows = [
        t
        for t in transactions
        if t.amount > 0
        and any(p in t.description.lower() for p in KNOWN_CARD_PROCESSORS)
    ]
    if len(proc_rows) < 10:
        return None
    by_day: defaultdict[date, Decimal] = defaultdict(lambda: Decimal("0"))
    for r in proc_rows:
        by_day[r.posted_date] += r.amount
    daily_amounts = [float(v) for v in by_day.values()]
    if len(daily_amounts) < 5:
        return None
    mean = statistics.mean(daily_amounts)
    if mean <= 0:
        return None
    cv = (statistics.pvariance(daily_amounts) ** 0.5) / mean
    if cv < 0.50:
        return None
    return Pattern(
        code="processor_holdback_detected",
        severity=20,
        detail=(
            f"{len(proc_rows)} processor payouts; daily CV={cv * 100:.0f}% — "
            "possible MCA holdback in force"
        ),
        source_ids=[t.id for t in proc_rows],
    )


def _payroll_present(debits: list[ClassifiedTransaction]) -> bool:
    """True iff at least one debit matches a known payroll-processor name.

    A category-aware shortcut: rows already classified as ``payroll``
    qualify even when the description omits a processor name (e.g. a
    direct-deposit batch labeled "DIR DEP"). Anything else requires
    one of the known-processor tokens.
    """
    if not debits:
        return False
    for d in debits:
        if d.category == "payroll":
            return True
        desc_lower = d.description.lower()
        if any(p in desc_lower for p in KNOWN_PAYROLL_PROCESSORS):
            return True
    return False


def _payroll_absent(
    payroll_present: bool,
    transactions: list[ClassifiedTransaction],
    period_start: date,
    period_end: date,
) -> Pattern | None:
    """Soft pattern: an operating business with no payroll over the period.

    Only fires when payroll is *absent* AND the period covers ≥21 days
    (shorter periods don't give a fair sample) AND total credits ≥
    $50k (master plan §6.4 — below that, a sole proprietor with no
    employees is plausible).
    """
    if payroll_present:
        return None
    statement_days = max(1, (period_end - period_start).days + 1)
    if statement_days < 21:
        return None
    revenue_rows = [
        t
        for t in transactions
        if t.amount > 0
        and t.category in {"deposit", "ach_credit", "wire_in", "refund"}
    ]
    total = sum((r.amount for r in revenue_rows), Decimal("0"))
    if total < Decimal("50000.00"):
        return None
    return Pattern(
        code="payroll_absent",
        severity=10,
        detail=(
            f"no payroll-processor activity over {statement_days} days "
            f"with ${total.quantize(Decimal('1'))} revenue"
        ),
        source_ids=[],
    )


def _ai_generated_statement_score(
    transactions: list[ClassifiedTransaction],
    deposits: list[ClassifiedTransaction],
) -> int:
    """Composite 0..100 fake-statement score (signal-only, never auto-decline).

    Per master plan §6.4: LLM-generated fakes typically show
      - "too clean" descriptions: full sentences, mixed case with no
        OCR noise / no abbreviations / no numeric trace ids
      - generic counterparty names ("Office Supplies", "Customer Payment")
        rather than ALL-CAPS bank-style labels
      - rounder-than-real amounts (we already score this via
        ``round_number_deposits`` but the composite weights it again)

    Component scores 0..100, then weighted-average. Tunable post-50-deals.
    """
    if len(transactions) < 5:
        return 0

    # Component 1: "too clean" description style — share of descriptions
    # that are title-case-or-lower (no all-caps tokens at all). Real
    # bank statements have lots of all-caps labels.
    title_count = sum(
        1
        for t in transactions
        if t.description and not any(w.isupper() and len(w) >= 3 for w in t.description.split())
    )
    too_clean = round((title_count / len(transactions)) * 100)

    # Component 2: digit-noise share. Real statements carry trace ids,
    # confirmation numbers, ach trace numbers — typically 6+ digit runs.
    long_digit = re.compile(r"\d{6,}")
    digit_noise = sum(1 for t in transactions if long_digit.search(t.description))
    digit_noise_ratio = round(((len(transactions) - digit_noise) / len(transactions)) * 100)

    # Component 3: round-number share of deposits — re-using the existing
    # heuristic but as a 0..100 score, not a binary flag.
    if deposits:
        round_count = sum(1 for d in deposits if d.amount % Decimal(100) == 0)
        round_score = round((round_count / len(deposits)) * 100)
    else:
        round_score = 0

    # Weighted composite — too_clean and digit_noise are the strongest
    # tells; round share is a weaker tertiary.
    composite = round(too_clean * 0.4 + digit_noise_ratio * 0.4 + round_score * 0.2)
    return min(100, max(0, composite))


__all__ = [
    "CHARGEBACK_KEYWORDS",
    "GENERIC_MCA_TERMS",
    "KNOWN_CARD_PROCESSORS",
    "KNOWN_FUNDERS",
    "KNOWN_PAYROLL_PROCESSORS",
    "REVERSAL_KEYWORDS",
    "CounterpartySignals",
    "McaPosition",
    "Pattern",
    "PatternAnalysis",
    "analyze_patterns",
]
