# ruff: noqa: RUF001, RUF002, RUF003 — file intentionally tests with
# Cyrillic/Greek lookalike glyphs (homograph evasion attempts).
"""Tests for OFAC homograph normalization.

Sanctions evaders can substitute visually-identical glyphs from other
scripts (Cyrillic 'а' for Latin 'a', etc.) to defeat naive substring
matching. The OFAC client now NFKC-normalizes and folds a limited table
of Cyrillic/Greek lookalikes for Latin before token-set matching.

These tests pin the most common evasion patterns. If a real evasion
attempt surfaces a glyph not yet in CONFUSABLE_MAP, extend the map and
add a row here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from aegis.scoring.ofac import OFACClient


def _write_cache(path: Path, entries: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "entries": entries,
                "refreshed_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )


def _client(tmp_path: Path, sdn_name: str) -> OFACClient:
    cache = tmp_path / "sdn.json"
    _write_cache(cache, [{"primary_name": sdn_name, "aliases": []}])
    return OFACClient(cache_path=cache, fetcher=lambda: b"")


def test_cyrillic_a_in_input_still_matches_latin_sdn_entry(tmp_path: Path) -> None:
    """Input contains Cyrillic 'а' (U+0430). SDN entry uses Latin 'a'."""
    client = _client(tmp_path, "Putin, Vladimir")
    cyrillic_a = "а"
    sneaky = f"Vl{cyrillic_a}dimir Putin"  # "Vlаdimir Putin"
    assert client.is_match(sneaky), "homograph evasion must be caught"


def test_greek_omicron_in_input_still_matches(tmp_path: Path) -> None:
    """Greek 'ο' (U+03BF) substituted for Latin 'o'."""
    client = _client(tmp_path, "Soto, Carlos")
    greek_o = "ο"
    sneaky = f"Carl{greek_o}s S{greek_o}t{greek_o}"
    assert client.is_match(sneaky)


def test_mixed_homograph_substitutions_still_match(tmp_path: Path) -> None:
    """Mix Cyrillic 'е' (U+0435) and Latin glyphs."""
    client = _client(tmp_path, "Eve, Sample")
    cyr_e = "е"
    sneaky = f"Sampl{cyr_e} {cyr_e}v{cyr_e}"  # all "e"s swapped to Cyrillic
    assert client.is_match(sneaky)


def test_legitimate_unrelated_name_does_not_match(tmp_path: Path) -> None:
    """Sanity: an honest name with no overlap doesn't false-positive."""
    client = _client(tmp_path, "Putin, Vladimir")
    assert not client.is_match("Jane Doe Bakery LLC")


def test_fullwidth_compat_chars_normalize_via_nfkc(tmp_path: Path) -> None:
    """Fullwidth digits/letters (NFKC compat decomposition) match too."""
    client = _client(tmp_path, "Acme Holdings")
    sneaky = "Ａｃｍｅ Ｈｏｌｄｉｎｇｓ"
    # "Ａｃｍｅ Ｈｏｌｄｉｎｇｓ" — fullwidth Latin.
    assert client.is_match(sneaky)
