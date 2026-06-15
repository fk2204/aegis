"""Per-(funder, industry_tier, score_tier) approval-rate aggregation
from ``funder_note_submissions`` — feeds ``match_funder``'s Sprint-4
historical-boost.

The route layer loads a 90-day window of submissions ONCE and projects
them into a ``dict[funder_id, dict[(industry_tier, score_tier),
Decimal]]`` keyed lookup; each ``match_funder`` call then plucks the
right cell. This keeps the matcher pure (no DB / repo dependency) and
the route a thin I/O orchestrator.

Sample-size discipline
----------------------
Below ``MIN_SAMPLE_SIZE`` (5) the cell is omitted from the map so
``match_funder`` reads ``None`` and applies no adjustment. The
floor reflects the operator's intent: a single approval doesn't make
a track record, and basing a +5 / -10 swing on noise would push
the matcher toward bad calls during the funder's ramp.

Decided vs. total denominator
-----------------------------
Approval rate denominator = approved + declined + countered (i.e.
DECIDED submissions). Pending submissions don't dilute the rate —
they represent in-flight deals where neither approval nor decline
has been recorded yet, and including them would systematically
under-state every funder during a busy week. This matches the
denominator used by the funder-performance page so the two surfaces
read consistently.

Tier-pair keys
--------------
The lookup is keyed by ``(industry_tier, score_tier)`` where:

* ``industry_tier`` is ``aegis.scoring_v2.industry.IndustryTier`` —
  derived from each submission's merchant's ``industry_choice``.
* ``score_tier`` is the AEGIS score letter (``A``..``F``) — derived
  from the merchant's most-recent decision row.

Submissions whose merchant has neither tier resolvable land in an
``("unknown", "unknown")`` bucket that's never queried by the matcher
(the matcher passes the deal's actual tiers) — they just don't
contribute to any boost decision.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID

from aegis.funder_note_submissions.models import FunderNoteSubmissionRow
from aegis.scoring_v2.industry import IndustryTier, industry_risk_tier

LOOKBACK_DAYS: Final[int] = 90
MIN_SAMPLE_SIZE: Final[int] = 5
"""Below this number of similar prior submissions, the cell is dropped
from the map so the matcher applies no boost."""


def build_historical_approval_index(
    *,
    submissions: list[FunderNoteSubmissionRow],
    industry_choice_by_merchant: Mapping[UUID, str | None],
    score_tier_by_merchant: Mapping[UUID, str],
    now: datetime,
) -> dict[UUID, dict[tuple[IndustryTier, str], Decimal]]:
    """Aggregate per-funder approval rates by (industry_tier, score_tier).

    Parameters
    ----------
    submissions
        Full window of submissions (caller may pre-filter or pass the
        unfiltered list — this function applies the 90-day lookback
        itself so route-level changes don't need to update the
        function's contract).
    industry_choice_by_merchant
        Map from merchant_id to the merchant's ``industry_choice``
        string (em-dash Close form). Missing entries (merchant
        deleted, race) resolve to industry tier ``"moderate"`` via
        ``industry_risk_tier(None)``.
    score_tier_by_merchant
        Map from merchant_id to the merchant's latest decision tier
        letter (``"A"``..``"F"``). Missing entries fall into an
        ``"unknown"`` bucket that callers never query.
    now
        Injectable for deterministic tests. The 90-day lookback is
        anchored to this timestamp.

    Returns
    -------
    dict[UUID, dict[tuple[IndustryTier, str], Decimal]]
        Nested map: funder_id -> (industry_tier, score_tier) ->
        approval_rate (Decimal, 0.0-1.0, 4dp). Cells with
        fewer than ``MIN_SAMPLE_SIZE`` decided submissions are
        ABSENT from the inner dict so callers can use a plain
        ``dict.get`` and get ``None`` for the no-data path without
        an extra threshold check.
    """
    cutoff = now - timedelta(days=LOOKBACK_DAYS)
    # Two parallel counters keyed by (funder_id, industry_tier, score_tier):
    # the decided denominator and the approved numerator.
    decided: defaultdict[tuple[UUID, IndustryTier, str], int] = defaultdict(int)
    approved: defaultdict[tuple[UUID, IndustryTier, str], int] = defaultdict(int)
    for s in submissions:
        if s.submitted_at < cutoff:
            continue
        if s.status not in ("approved", "declined", "countered"):
            continue
        industry_tier = industry_risk_tier(industry_choice_by_merchant.get(s.merchant_id))
        score_tier = score_tier_by_merchant.get(s.merchant_id, "unknown")
        key = (s.funder_id, industry_tier, score_tier)
        decided[key] += 1
        if s.status == "approved":
            approved[key] += 1

    out: dict[UUID, dict[tuple[IndustryTier, str], Decimal]] = defaultdict(dict)
    for (funder_id, industry_tier, score_tier), total in decided.items():
        if total < MIN_SAMPLE_SIZE:
            continue
        approved_count = approved[(funder_id, industry_tier, score_tier)]
        rate = (Decimal(approved_count) / Decimal(total)).quantize(Decimal("0.0001"))
        out[funder_id][(industry_tier, score_tier)] = rate
    return dict(out)


def lookup_historical_approval_rate(
    index: Mapping[UUID, Mapping[tuple[IndustryTier, str], Decimal]],
    *,
    funder_id: UUID,
    industry_tier: IndustryTier,
    score_tier: str,
) -> Decimal | None:
    """One-liner lookup so route callers don't have to nest dict.get
    twice. Returns ``None`` when the funder has no row or no cell for
    the given tier pair."""
    funder_map = index.get(funder_id)
    if funder_map is None:
        return None
    return funder_map.get((industry_tier, score_tier))


__all__ = [
    "LOOKBACK_DAYS",
    "MIN_SAMPLE_SIZE",
    "build_historical_approval_index",
    "lookup_historical_approval_rate",
]
