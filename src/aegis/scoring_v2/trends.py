"""Multi-month revenue trends — three-direction summary for the dossier.

Projects a sequence of monthly buckets to three trend tokens (revenue,
ADB, NSF) that the underwriter reads at a glance: is this merchant's
top-line growing, holding, or fading? Same question for cash buffer
(ADB) and statement quality (NSF).

* ``revenue_trend`` — derived from ``MonthBreakdown.deposits`` (gross
  monthly deposits, the operator-confirmed proxy for revenue).
* ``adb_trend`` — derived from ``MonthBreakdown.avg_balance``.
* ``nsf_trend`` — derived from ``MonthBreakdown.nsf_count`` (per-month
  count of transactions classified as ``nsf_fee``). Sprint 4 (2026-06-15)
  wired this end-to-end; the prior ``"flat"`` placeholder is gone.
  Reads ``growing`` when NSF count goes up between the prior and
  latest months (operator-bad — more cashflow distress) and
  ``declining`` when it goes down (operator-good — merchant is
  stabilising). 0-anchor handling is special-cased: 0→N counts as
  ``growing`` and N→0 counts as ``declining`` regardless of
  percentage, since "one NSF appearing" is a meaningful signal
  the percentage band would otherwise mis-classify.
* ``months_compared`` — number of buckets that participated. ``0`` for
  an empty list, ``1`` for a single bucket (fallback to all-flat),
  ``2+`` for the real comparison. Surfaced for audit per CLAUDE.md
  "every aggregate stores its sources" — the operator can re-derive
  the trend without re-running the function.

DIRECTION LOGIC
---------------
Need at least two buckets. Sort ascending by ``month`` string (ISO
``YYYY-MM`` sorts lexicographically). The operator's spec said
"compare trailing 2 months vs prior month" which is ambiguous; the
chosen interpretation is "the latest month vs the month immediately
before it" — i.e. ``anchor = buckets[-2]``, ``latest = buckets[-1]``.
If the operator meant a trailing-2-month AVERAGE vs the prior month
they can patch the comparator without changing the return shape.

* Zero anchor with non-zero latest → ``"growing"`` (any movement off
  zero is growth).
* Zero anchor with zero latest → ``"flat"`` (nothing happened).
* Else ``pct_change = (latest - anchor) / anchor * 100``:
  - ``>= +10`` → ``"growing"``
  - ``<= -10`` → ``"declining"``
  - otherwise → ``"flat"``

The ±10 thresholds are inclusive — exactly +10% is growth, exactly
-10% is decline. Decimal throughout; never float.

DECISION-BOUNDARY POSTURE — SHADOW ONLY
---------------------------------------
This module is operator-informational. Trend tokens do NOT feed
``aegis.scoring.score``'s decline path — the live decline gates
(``MAX_DEBT_TO_REVENUE``, ``MCA_POSITIONS_HARD_DECLINE``,
``DAYS_NEGATIVE_HARD_DECLINE``, etc.) continue to govern, and no
existing scoring field reads from ``RevenueTrends``. Per CLAUDE.md
"Decision-boundary changes — shadow-first": this surface adds
informational chips; any future flip to consume a trend token in the
decline path would be a config / env-var change, not a code deploy.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from aegis.scoring.models import MonthBreakdown

TrendDirection = Literal["growing", "flat", "declining"]

GROWING_THRESHOLD_PCT: Final[Decimal] = Decimal("10")
"""Lower bound (inclusive) of the ``growing`` band. ``pct_change >=
+10`` → growing."""

DECLINING_THRESHOLD_PCT: Final[Decimal] = Decimal("-10")
"""Upper bound (inclusive) of the ``declining`` band. ``pct_change <=
-10`` → declining."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
        frozen=False,
    )


class RevenueTrends(_StrictModel):
    """Three-direction trend summary for the dossier "trends" chip."""

    revenue_trend: TrendDirection = Field(
        description=(
            "Direction of ``deposits`` between the prior month and "
            "the latest month. ``flat`` when fewer than two buckets "
            "are available or the month-over-month change is within "
            "the ±10% band."
        ),
    )
    adb_trend: TrendDirection = Field(
        description=(
            "Direction of ``avg_balance`` between the prior month "
            "and the latest month. Same threshold band as "
            "``revenue_trend``."
        ),
    )
    nsf_trend: TrendDirection = Field(
        description=(
            "Direction of NSF-fee transaction count between the prior "
            "month and the latest month. growing means NSFs went up "
            "(operator-bad — more cashflow distress); declining means "
            "NSFs went down (operator-good — merchant stabilising). "
            "Same ±10% / count-based threshold band as ``revenue_trend``."
        ),
    )
    months_compared: int = Field(
        ge=0,
        description=(
            "Number of monthly buckets that participated. ``0`` for "
            "an empty input list, ``1`` for a single bucket (returns "
            "all-flat fallback), ``2+`` for the real anchor-vs-latest "
            "comparison. Surfaced so the operator can re-derive the "
            "trend without re-running the function."
        ),
    )


def compute_revenue_trends(monthly_buckets: list[MonthBreakdown]) -> RevenueTrends:
    """Project ``monthly_buckets`` to a three-direction trend summary.

    Parameters
    ----------
    monthly_buckets
        Per-month rollups from the parser. Each carries ``month``
        (``YYYY-MM``), ``deposits``, ``withdrawals``, ``avg_balance``.
        Sort order is normalized inside the function; callers do not
        need to pre-sort.

    Returns
    -------
    RevenueTrends
        Populated trend tokens plus the ``months_compared`` audit
        field. Never raises on empty / single-bucket input — returns
        the all-flat fallback with the appropriate ``months_compared``
        count instead.

    Notes
    -----
    ``nsf_trend`` reads ``MonthBreakdown.nsf_count`` (per-month NSF-fee
    transaction count). The ±10% threshold band applies on top of an
    absolute floor: a 0→1 jump is ``growing`` (the operator wants to
    see the first NSF surface), a 1→0 drop is ``declining`` (the
    merchant cleaned up), 0→0 is ``flat``. Above 1, the percentage
    band applies the same way as revenue / ADB.
    """
    months_compared = len(monthly_buckets)

    if months_compared < 2:
        return RevenueTrends(
            revenue_trend="flat",
            adb_trend="flat",
            nsf_trend="flat",
            months_compared=months_compared,
        )

    sorted_buckets = sorted(monthly_buckets, key=lambda bucket: bucket.month)
    anchor = sorted_buckets[-2]
    latest = sorted_buckets[-1]

    return RevenueTrends(
        revenue_trend=_classify(anchor.deposits, latest.deposits),
        adb_trend=_classify(anchor.avg_balance, latest.avg_balance),
        nsf_trend=_classify_count(anchor.nsf_count, latest.nsf_count),
        months_compared=months_compared,
    )


def _classify_count(anchor: int, latest: int) -> TrendDirection:
    """Map an anchor → latest pair of NSF counts to a trend direction.

    The percentage-band classifier degrades poorly at small integer
    counts (1 → 2 is +100%, but operationally still "one more
    incident"). Below an absolute floor of 1, the direction reads
    literally — any non-zero appearance is ``growing``, any drop to
    zero is ``declining``. Above 1, fall through to the same
    Decimal-percentage classifier the money fields use.
    """
    if anchor == 0 and latest == 0:
        return "flat"
    if anchor == 0:
        return "growing"
    if latest == 0:
        return "declining"
    return _classify(Decimal(anchor), Decimal(latest))


def _classify(anchor: Decimal, latest: Decimal) -> TrendDirection:
    """Map an anchor → latest pair to a trend direction.

    Zero-anchor special case avoids ``ZeroDivisionError`` and encodes
    the operator's intent: any movement off a zero baseline is growth,
    no movement is flat. Negative directions off a zero anchor are not
    representable (a deposit total can't go below zero), so the case
    isn't enumerated.
    """
    if anchor == 0:
        return "growing" if latest != 0 else "flat"

    pct_change = (latest - anchor) / anchor * Decimal("100")
    if pct_change >= GROWING_THRESHOLD_PCT:
        return "growing"
    if pct_change <= DECLINING_THRESHOLD_PCT:
        return "declining"
    return "flat"


__all__ = [
    "DECLINING_THRESHOLD_PCT",
    "GROWING_THRESHOLD_PCT",
    "RevenueTrends",
    "TrendDirection",
    "compute_revenue_trends",
]
