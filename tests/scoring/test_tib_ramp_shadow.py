"""H8 — TIB ramp shadow flag annotates a graduated penalty.

Today: ``_score_tib`` applies -15 to <6mo (after the <3mo hard decline)
and -8 to 6-11mo. A 3.1-month merchant gets the same penalty as a
5.9-month merchant.

H8 adds a shadow flag annotating what a graduated penalty WOULD be:
  <6mo   → -15 (matches today)
  6-11   → -10
  12-17  → -5
  18-23  → -2
  >=24   → 0

Per CLAUDE.md "Decision-boundary changes — shadow-first": the existing
-15 / -8 deltas in ``_score_tib`` are NOT modified. The flag annotates
what live behavior WOULD be after a config flip; validate against the
corpus before flipping.

Tests:
- months=2 → still hard-declines (legacy threshold unchanged).
- months=3 → no hard decline, existing -15 still applied, shadow flag
  ``tib_ramp_shadow:months=3_current_delta=-15_graduated_delta=-15``.
- months=8 → existing -8 still applied, shadow flag says graduated
  would be -10 (TIGHTER than today, not looser — the 6-11mo band is the
  one place where shadow is more conservative than live).
- months=18 → existing 0 still applied (12-23 month bucket is even),
  shadow flag indicates graduated would be -2.
- months=30 → existing +5 still applied, no shadow flag (24+ converges
  to today's positive credits; nothing to ramp).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aegis.scoring.models import ScoreInput
from aegis.scoring.ofac import OFACClient
from aegis.scoring.score import score_deal


@pytest.fixture
def clean_ofac(tmp_path: Path) -> OFACClient:
    cache = tmp_path / "ofac.json"
    cache.write_text(
        json.dumps(
            {
                "entries": [{"primary_name": "ZZZ Should Not Match", "aliases": []}],
                "refreshed_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    def _now() -> datetime:
        return datetime.now(UTC)

    def _must_not_call() -> bytes:
        raise AssertionError("OFAC fetcher should not be called when cache is fresh")

    return OFACClient(cache_path=cache, fetcher=_must_not_call, now=_now)


def _tib_deltas_from_breakdown(
    breakdown: list[dict[str, object]],
) -> list[int]:
    """Pull every delta whose factor name starts with ``tib_``."""
    deltas: list[int] = []
    for row in breakdown:
        factor = row.get("factor")
        if not isinstance(factor, str) or not factor.startswith("tib_"):
            continue
        delta = row.get("delta")
        if isinstance(delta, int):
            deltas.append(delta)
    return deltas


def test_tib_under_3mo_still_hard_declines(
    clean_deal: ScoreInput, clean_ofac: OFACClient
) -> None:
    """Legacy hard-decline threshold (<3 months) is unchanged by H8."""
    deal = clean_deal.model_copy(update={"time_in_business_months": 2})
    result = score_deal(deal, ofac=clean_ofac)

    assert result.recommendation == "decline"
    assert "tib_under_3_months" in result.hard_decline_reasons
    # Hard-decline path skips soft-scoring AND skips the H8 helper
    # (which lives at the end of _soft_score). No shadow flag expected.
    assert not any(
        f.startswith("tib_ramp_shadow:") for f in result.shadow_flags
    )


def test_tib_3mo_shadow_matches_current_floor(
    clean_deal: ScoreInput, clean_ofac: OFACClient
) -> None:
    """3 months: no hard decline, current -15 still applied, shadow agrees.

    The <6mo band is where shadow == current. Annotation is preserved
    so the operator can see "no ramp benefit at this end of the band."
    """
    deal = clean_deal.model_copy(update={"time_in_business_months": 3})
    result = score_deal(deal, ofac=clean_ofac)

    assert "tib_under_3_months" not in result.hard_decline_reasons
    # Existing -15 deduction still fires.
    assert -15 in _tib_deltas_from_breakdown(result.breakdown)

    expected = "tib_ramp_shadow:months=3_current_delta=-15_graduated_delta=-15"
    assert expected in result.shadow_flags, (
        f"expected {expected}, got {result.shadow_flags}"
    )


def test_tib_8mo_shadow_proposes_tighter_band(
    clean_deal: ScoreInput, clean_ofac: OFACClient
) -> None:
    """8 months: today's -8 still applied; shadow says graduated -10.

    The 6-11mo band is the one place where the ramp is MORE
    conservative than live. Shadow lets the operator see this tradeoff
    before any config flip.
    """
    deal = clean_deal.model_copy(update={"time_in_business_months": 8})
    result = score_deal(deal, ofac=clean_ofac)

    # Existing -8 deduction still fires.
    assert -8 in _tib_deltas_from_breakdown(result.breakdown)

    expected = "tib_ramp_shadow:months=8_current_delta=-8_graduated_delta=-10"
    assert expected in result.shadow_flags, (
        f"expected {expected}, got {result.shadow_flags}"
    )


def test_tib_18mo_shadow_proposes_small_penalty(
    clean_deal: ScoreInput, clean_ofac: OFACClient
) -> None:
    """18 months: today's 0 still applied; shadow says graduated -2.

    The 12-23 month bucket sits at exactly zero today (no credit, no
    penalty). The shadow ramp suggests a small -2 penalty to surface
    "not yet 24mo / not seasoned." Live behavior unchanged.
    """
    deal = clean_deal.model_copy(update={"time_in_business_months": 18})
    result = score_deal(deal, ofac=clean_ofac)

    # Existing 0 delta still fires (factor is tib_1_2yr).
    deltas = _tib_deltas_from_breakdown(result.breakdown)
    assert 0 in deltas, f"expected 0 delta in {deltas}"

    expected = "tib_ramp_shadow:months=18_current_delta=0_graduated_delta=-2"
    assert expected in result.shadow_flags, (
        f"expected {expected}, got {result.shadow_flags}"
    )


def test_tib_30mo_no_shadow_flag(
    clean_deal: ScoreInput, clean_ofac: OFACClient
) -> None:
    """30 months: today's +5 still applied; no shadow flag (>=24 converges)."""
    deal = clean_deal.model_copy(update={"time_in_business_months": 30})
    result = score_deal(deal, ofac=clean_ofac)

    # Existing +5 deduction still fires (factor is tib_2_3yr).
    assert 5 in _tib_deltas_from_breakdown(result.breakdown)

    assert not any(
        f.startswith("tib_ramp_shadow:") for f in result.shadow_flags
    ), f"24+mo should not emit ramp shadow; got {result.shadow_flags}"
