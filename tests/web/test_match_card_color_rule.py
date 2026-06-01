"""Tests for ``_match_card``'s color rule, with focus on the tier-F
false-disable regression.

``match_funder`` returns ``FunderMatch(match_score=0)`` in two distinct
scenarios:

  (a) The merchant fails one or more funder criteria (qualifies=False).
      In this case ``reasons=[]`` and ``soft_concerns`` is the union of
      hard-fail reasons + soft signals.
  (b) The merchant clears every funder criterion BUT the merchant's
      overall AEGIS tier is F. ``_likelihood`` returns 0 because the
      tier-F base is 0 (``max(0, 0 - 10*N) == 0``). In this case
      ``reasons=["tier_F"]`` (qualifies=True) and ``soft_concerns``
      holds soft signals only.

Prior to the fix, ``_match_card`` keyed off ``match.match_score == 0``,
which conflated (a) and (b) and rendered every funder card red+disabled
for any tier-F merchant — even funders the merchant qualified for on
every criterion. The fix splits the color rule onto
``bool(match.reasons)``, the true qualifies signal.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from aegis.funders.models import FunderRow
from aegis.scoring.models import FunderMatch
from aegis.web.router import _match_card


def _funder() -> FunderRow:
    """Minimal FunderRow stub for _match_card tests — only id + name
    + flag fields the card dict surfaces are exercised here."""
    return FunderRow(
        id=uuid4(),
        name="Test Funder",
        requires_coj=False,
        charges_merchant_advance_fees=False,
    )


# ---------------------------------------------------------------------------
# Pre-fix happy path — these were already correct, locking them down
# ---------------------------------------------------------------------------


def test_qualifies_no_soft_concerns_renders_green() -> None:
    """Merchant qualifies and has zero soft concerns → green card."""
    match = FunderMatch(
        funder_id=uuid4(),
        funder_name="Test Funder",
        match_score=75,
        reasons=["tier_B"],
        soft_concerns=[],
    )
    card = _match_card(_funder(), match)

    assert card["color"] == "green"
    assert card["hard_reasons"] == []
    assert card["soft_concerns"] == []


def test_qualifies_with_soft_concerns_renders_yellow() -> None:
    """Merchant qualifies but has soft concerns → yellow card. The
    soft_concerns list surfaces to the operator on the card."""
    match = FunderMatch(
        funder_id=uuid4(),
        funder_name="Test Funder",
        match_score=55,
        reasons=["tier_C"],
        soft_concerns=["adb_partial_coverage: 14 of 30 days"],
    )
    card = _match_card(_funder(), match)

    assert card["color"] == "yellow"
    assert card["hard_reasons"] == []
    assert card["soft_concerns"] == ["adb_partial_coverage: 14 of 30 days"]


def test_not_qualifies_real_hard_fail_renders_red() -> None:
    """Merchant fails a funder criterion → red card. match_funder
    unions hard + soft into soft_concerns, so the red branch surfaces
    that union as hard_reasons (current behavior; separate cleanup
    item to split them properly)."""
    match = FunderMatch(
        funder_id=uuid4(),
        funder_name="Test Funder",
        match_score=0,
        reasons=[],  # qualifies=False
        soft_concerns=["nsf 12 > max 5"],
    )
    card = _match_card(_funder(), match)

    assert card["color"] == "red"
    assert card["hard_reasons"] == ["nsf 12 > max 5"]
    assert card["soft_concerns"] == []


# ---------------------------------------------------------------------------
# Tier-F regression — the actual bug this commit fixes
# ---------------------------------------------------------------------------


def test_tier_f_qualifies_no_soft_concerns_renders_green_not_red() -> None:
    """Tier-F merchant who clears every funder criterion → green card.

    ``_likelihood`` returns 0 for tier F regardless of qualifies (base
    is 0). Pre-fix this rendered RED because the color rule keyed off
    match_score == 0. Fixed by keying off ``bool(match.reasons)``
    instead — the true qualifies signal.
    """
    match = FunderMatch(
        funder_id=uuid4(),
        funder_name="Test Funder",
        match_score=0,  # tier-F base → 0 even though qualifies
        reasons=["tier_F"],  # qualifies=True
        soft_concerns=[],
    )
    card = _match_card(_funder(), match)

    assert card["color"] == "green"
    assert card["hard_reasons"] == []
    assert card["soft_concerns"] == []


def test_tier_f_qualifies_with_soft_concerns_renders_yellow_not_red() -> None:
    """Tier-F merchant who qualifies but has soft concerns → yellow card."""
    match = FunderMatch(
        funder_id=uuid4(),
        funder_name="Test Funder",
        match_score=0,
        reasons=["tier_F"],
        soft_concerns=["payroll_cadence: irregular"],
    )
    card = _match_card(_funder(), match)

    assert card["color"] == "yellow"
    assert card["hard_reasons"] == []
    assert card["soft_concerns"] == ["payroll_cadence: irregular"]


def test_tier_f_with_real_hard_fail_still_renders_red() -> None:
    """Tier-F merchant who also fails a funder criterion → still red.
    The fix doesn't soften the red-on-real-hard-fail behavior; tier F
    + qualifies=False is still a real disqualification."""
    match = FunderMatch(
        funder_id=uuid4(),
        funder_name="Test Funder",
        match_score=0,
        reasons=[],  # qualifies=False
        soft_concerns=["industry_excluded: 51200"],
    )
    card = _match_card(_funder(), match)

    assert card["color"] == "red"
    assert card["hard_reasons"] == ["industry_excluded: 51200"]


# ---------------------------------------------------------------------------
# Card metadata sanity — these fields ride along the color dispatch and
# matter to the template even though they're not the bug being fixed
# ---------------------------------------------------------------------------


def test_card_dict_carries_funder_metadata_for_template() -> None:
    """The card dict surfaces funder_id, funder_name, match_score, the
    funder_requires_coj flag, and the funder_charges_merchant_advance_fees
    flag — the template needs them. Pinned so a refactor of the dict
    shape can't quietly drop a field."""
    funder = FunderRow(
        id=uuid4(),
        name="Real Funder Name",
        requires_coj=True,
        charges_merchant_advance_fees=True,
    )
    match = FunderMatch(
        funder_id=funder.id,
        funder_name=funder.name,
        match_score=42,
        reasons=["tier_D"],
        soft_concerns=[],
    )
    card = _match_card(funder, match)

    assert card["funder_id"] == str(funder.id)
    assert card["funder_name"] == "Real Funder Name"
    assert card["match_score"] == 42
    assert card["funder_requires_coj"] is True
    assert card["funder_charges_merchant_advance_fees"] is True
    assert card["criteria_comparison"] == []  # no score_input supplied
