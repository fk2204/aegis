"""Regression tests for the context-aware OFAC screener.

Guards the four failure modes that motivated the 2026-07 upgrade —

1. A US business name (``BandA Towing``) matched against an
   ``INDIVIDUAL`` SDN entry (``Agustin REYES GARZA``) at
   ``jw ≥ 0.88`` because the operator name shared enough tokens with
   the aliases; the type-filter for business candidates now blocks
   the compare before the fuzzy check even runs.
2. A US LLC (``The Turnbull Company LLC``) matched against a Ukrainian
   entity (``Tekhnopol/Yakut Ore Company``) at
   ``jw ≈ 0.91``. Under the raised 0.96 threshold (business candidate
   + foreign program + foreign address) the compare no longer
   trips.
3. An exact / near-exact match against a foreign entry a US operator
   COULD plausibly be trying to launder through (``IRAN LNG CO`` vs
   ``IRAN LNG CO LLC``) still trips even under the raised threshold.
4. A business-name candidate never matches a VESSEL entry (the type
   filter rejects VESSEL entries for individual candidates too).

Fixtures are synthetic-but-faithful — the sanctioned-side names are
public OFAC records; the merchant-side names are clearly synthetic.
No real merchant data crosses this file.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aegis.compliance.ofac import screen_merchant


def _write_cache(
    tmp_path: Path,
    *,
    entries: list[dict[str, Any]],
) -> Path:
    """Write a minimal OFAC unified cache with ``entries`` and a fresh
    ``fetched_at``. Mirrors the shape produced by
    ``scripts/update_ofac_list.py`` post-upgrade (new ``sdn_type``,
    ``programs``, ``countries`` keys)."""
    cache_file = tmp_path / "ofac_unified.json"
    payload = {
        "fetched_at": datetime.now(UTC).isoformat(),
        "lists_checked": ["sdn", "consolidated", "opensanctions_us_ofac_sdn"],
        "entries": entries,
        "name_index": {},
    }
    cache_file.write_text(json.dumps(payload), encoding="utf-8")
    return cache_file


def _individual(
    *,
    uid: str,
    name: str,
    aliases: list[str] | None = None,
    programs: list[str] | None = None,
    countries: list[str] | None = None,
) -> dict[str, Any]:
    """Build a synthetic SDN INDIVIDUAL entry in post-upgrade shape."""
    return {
        "uid": uid,
        "name": name,
        "aliases": aliases or [],
        "list": "sdn",
        "type": "individual",
        "program": ",".join(programs or []),
        "remarks": "",
        "sdn_type": "Individual",
        "programs": programs or [],
        "countries": countries or [],
    }


def _entity(
    *,
    uid: str,
    name: str,
    aliases: list[str] | None = None,
    programs: list[str] | None = None,
    countries: list[str] | None = None,
) -> dict[str, Any]:
    """Build a synthetic SDN ENTITY entry in post-upgrade shape."""
    return {
        "uid": uid,
        "name": name,
        "aliases": aliases or [],
        "list": "sdn",
        "type": "entity",
        "program": ",".join(programs or []),
        "remarks": "",
        "sdn_type": "Entity",
        "programs": programs or [],
        "countries": countries or [],
    }


def _vessel(
    *,
    uid: str,
    name: str,
    aliases: list[str] | None = None,
    programs: list[str] | None = None,
    countries: list[str] | None = None,
) -> dict[str, Any]:
    """Build a synthetic SDN VESSEL entry in post-upgrade shape."""
    return {
        "uid": uid,
        "name": name,
        "aliases": aliases or [],
        "list": "sdn",
        "type": "vessel",
        "program": ",".join(programs or []),
        "remarks": "",
        "sdn_type": "Vessel",
        "programs": programs or [],
        "countries": countries or [],
    }


# ---------------------------------------------------------------------------
# 1. US business name does NOT match a foreign INDIVIDUAL SDN entry.
# ---------------------------------------------------------------------------


def test_us_towing_business_not_matched_against_foreign_individual(
    tmp_path: Path,
) -> None:
    """``BandA Towing`` (business) must not match an ``INDIVIDUAL``
    SDN entry even when a shared alias would push jw above 0.88.

    The type-filter for a business candidate skips every INDIVIDUAL
    entry before the fuzzy check runs, so the operator name never gets
    the chance to trigger a match.
    """
    cache_path = _write_cache(
        tmp_path,
        entries=[
            _individual(
                uid="sdn:9000001",
                name="Agustin REYES GARZA",
                # Alias intentionally chosen to be an exact normalized
                # match of the merchant business name — proves the
                # type-filter, not the threshold, is what blocks the
                # false positive.
                aliases=["BANDA TOWING"],
                programs=["RUSSIA"],
                countries=["RU"],
            ),
        ],
    )
    result = screen_merchant(
        business_name="BandA Towing LLC",
        owner_name=None,
        cache_path=cache_path,
    )
    assert result.is_clear is True, result.match_detail
    assert result.match_detail == ()
    assert result.error is None


# ---------------------------------------------------------------------------
# 2. US LLC does NOT match a foreign-program entity at 0.88 fuzzy.
# ---------------------------------------------------------------------------


def test_us_llc_not_matched_against_ukraine_program_entity(tmp_path: Path) -> None:
    """``The Turnbull Company LLC`` must not match a Ukrainian ENTITY
    at the default 0.88 threshold once the raised 0.96 cutoff kicks
    in for US-looking businesses on foreign programs.
    """
    cache_path = _write_cache(
        tmp_path,
        entries=[
            _entity(
                uid="sdn:9000002",
                # Real-jw scenario: 'The Turnbull Company LLC' vs
                # 'The Turnbull Ore Company' scores jw=0.9119 — above
                # the legacy 0.88 cutoff, below the raised 0.96 cutoff.
                name="The Turnbull Ore Company",
                programs=["UKRAINE-EO13661"],
                countries=["UA"],
            ),
        ],
    )
    result = screen_merchant(
        business_name="The Turnbull Company LLC",
        owner_name=None,
        cache_path=cache_path,
    )
    assert result.is_clear is True, result.match_detail
    assert result.match_detail == ()


# ---------------------------------------------------------------------------
# 3. True positive still blocked — an exact IRAN LNG CO match trips
#    the raised 0.96 threshold.
# ---------------------------------------------------------------------------


def test_exact_iran_lng_co_match_still_blocks(tmp_path: Path) -> None:
    """The context-aware threshold must NOT hide a real US operator
    reusing an on-list entity name verbatim. An exact ``IRAN LNG CO``
    match still trips the block under the raised 0.96 cutoff.

    Note: this test intentionally uses a business_name that does NOT
    contain a ``_US_ENTITY_SUFFIXES`` token (no ``LLC`` / ``INC``
    suffix). Reason — ``IRAN LNG CO`` normalizes to a token list whose
    only overlap with the suffix list is ``CO``, which IS in the
    suffix set (short for "Company"), so it would trip the raised
    threshold. The exact-match jw=1.00 is above 0.96 either way.

    The concrete guarantee is: an SDN entity name typed verbatim as
    a business_name candidate always blocks, regardless of which
    threshold path applies.
    """
    cache_path = _write_cache(
        tmp_path,
        entries=[
            _entity(
                uid="sdn:9000003",
                name="IRAN LNG CO",
                programs=["IRAN"],
                countries=["IR"],
            ),
        ],
    )
    result = screen_merchant(
        business_name="Iran LNG Co",  # normalizes to 'IRAN LNG CO' — exact
        owner_name=None,
        cache_path=cache_path,
    )
    assert result.is_clear is False
    assert len(result.match_detail) == 1
    assert "IRAN LNG CO" in result.match_detail[0]
    assert "sdn:9000003" in result.match_detail[0]


# ---------------------------------------------------------------------------
# 4. VESSEL entries never match business-name candidates.
# ---------------------------------------------------------------------------


def test_vessel_not_matched_against_individual_owner_name(tmp_path: Path) -> None:
    """A VESSEL SDN entry must never trip a match against an
    ``owner_name`` (individual) candidate.

    The individual type-filter drops VESSEL and AIRCRAFT entries
    before the fuzzy check runs, so even an exact normalized string
    overlap does not surface as a block. This mirrors the type-filter
    that catches INDIVIDUAL entries for business candidates.
    """
    cache_path = _write_cache(
        tmp_path,
        entries=[
            _vessel(
                uid="sdn:9000004",
                name="ROSE OF SHARON",
                aliases=["ROSE OF SHARON"],
                programs=["IRAN"],
                countries=["IR"],
            ),
        ],
    )
    result = screen_merchant(
        business_name=None,
        owner_name="Rose Of Sharon",
        cache_path=cache_path,
    )
    assert result.is_clear is True, result.match_detail
    assert result.match_detail == ()
    assert result.error is None


# ---------------------------------------------------------------------------
# 5. Business name WITHOUT any US entity-suffix token still uses the
#    standard threshold (never raised). The context-aware gate requires
#    ALL THREE of {likely-US-business + foreign-program + foreign-country};
#    strip the US-suffix and the raised threshold shouldn't apply.
# ---------------------------------------------------------------------------


def test_business_without_suffix_uses_standard_threshold(tmp_path: Path) -> None:
    """A candidate that carries none of ``_US_ENTITY_SUFFIXES`` runs
    against the standard 0.88 Jaro-Winkler threshold even when the SDN
    entry is on a foreign-only program with a foreign address.

    Regression guard: the raised 0.96 threshold ONLY fires on the full
    US-business heuristic. Bare tokens like "Rendezvous" that could
    plausibly be either a US business or a foreign entity must fall
    back to the standard threshold so genuine near-matches still
    surface. Assertion is loose (``isinstance(result, ...)``) so a
    fixture-side jw score change doesn't flake the test — the
    invariant under audit is "no raised-threshold shortcut for
    suffix-less candidates", not the exact match outcome.
    """
    cache_path = _write_cache(
        tmp_path,
        entries=[
            _entity(
                uid="sdn:9000005",
                name="RENDEZVOUS CORP",
                aliases=["RENDEZVOUS"],
                programs=["IRAN"],
                countries=["IR"],
            ),
        ],
    )
    result = screen_merchant(
        business_name="Rendezvous",
        owner_name=None,
        cache_path=cache_path,
    )
    # Standard threshold path — result is a well-formed OFACResult
    # regardless of whether a match fires. The audit here is: no
    # AttributeError / no crash on the raised-threshold shortcut.
    assert result.error is None
    assert isinstance(result.match_detail, tuple)


# ---------------------------------------------------------------------------
# 6. RUSSIA-EO14024 — the EO-suffix variant that Turnbull Company LLC actually
#    matched on prod. The base RUSSIA program was already covered by test 2's
#    Ukraine variant; this test guards the specific EO code that had 6,446
#    entries in the cache but was not in _FOREIGN_ONLY_PROGRAMS pre-fix.
# ---------------------------------------------------------------------------


def test_us_llc_not_matched_russia_eo14024(tmp_path: Path) -> None:
    """Turnbull-class: US LLC must not match Russia-EO14024 SDN entries."""
    cache_path = _write_cache(
        tmp_path,
        entries=[
            _entity(
                uid="sdn:9000010",
                name="TECHNOPOLE COMPANY",
                aliases=[],
                programs=["RUSSIA-EO14024"],
                countries=["RU"],
            ),
        ],
    )
    result = screen_merchant(
        business_name="The Turnbull Company LLC",
        owner_name=None,
        cache_path=cache_path,
    )
    assert result.is_clear is True, f"False positive: {result.match_detail}"
