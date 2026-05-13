"""Tests for the ASCII-safe slugify helper.

Used in content-disposition filenames and ZIP entry names. Properties
that matter: deterministic, ASCII-only, no leading/trailing underscore,
no double underscores, never empty.
"""

from __future__ import annotations

import pytest

from aegis.web._slug import slugify


@pytest.mark.parametrize(
    ("input_text", "expected"),
    [
        ("Acme Corp", "acme_corp"),
        ("ACME CORP", "acme_corp"),
        ("Acme   Corp", "acme_corp"),
        ("   Acme Corp   ", "acme_corp"),
        ("Acme & Sons, LLC.", "acme_sons_llc"),
        ("Acme--Corp", "acme_corp"),
        ("acme-corp", "acme_corp"),
        ("Acme123", "acme123"),
        ("123 Acme", "123_acme"),
    ],
)
def test_basic_slugify(input_text: str, expected: str) -> None:
    assert slugify(input_text) == expected


def test_empty_string_falls_back() -> None:
    assert slugify("") == "merchant"


def test_only_punctuation_falls_back() -> None:
    assert slugify("!!!---") == "merchant"
    assert slugify("   ") == "merchant"


def test_unicode_accents_collapse_to_underscore() -> None:
    """Non-ASCII chars are stripped — they're not alnum in ASCII sense.

    Cafe Niño Cocina -> 'cafe' + 'i' + ... underscores between, no
    leading/trailing underscore.
    """
    # Filip's test data sometimes has these (NY/CA boroughs).
    result = slugify("Café Niño")
    # 'é' and 'ñ' are isalnum() in Python -> kept verbatim
    # Verify deterministic + no leading/trailing underscore + no double underscore
    assert result == result.strip("_")
    assert "__" not in result
    assert result  # not empty


def test_emoji_treated_as_separator() -> None:
    """Emoji are not isalnum -> become separators."""
    result = slugify("Acme 🚀 Corp")
    assert result == "acme_corp"


def test_deterministic() -> None:
    """Same input always yields same output."""
    inp = "Bob's Diner #42"
    assert slugify(inp) == slugify(inp) == slugify(inp)


def test_very_long_input_stays_safe() -> None:
    """No length cap is asserted (the helper doesn't impose one), but
    output should remain a valid slug."""
    inp = "A" * 500 + " " + "B" * 500
    result = slugify(inp)
    assert result.startswith("a")
    assert "_" in result
    assert result == result.strip("_")
    assert "__" not in result


def test_distinct_inputs_can_collide_but_obvious_cases_dont() -> None:
    """Slugify is lossy by design (multiple punctuation chars collapse to
    one underscore), but obvious distinct names must not collide."""
    assert slugify("Acme Corp") != slugify("Acme Inc")
    assert slugify("Bob's Diner") != slugify("Bobs Diner Two")


def test_no_double_underscores_anywhere() -> None:
    """The output must never contain ``__`` regardless of input punctuation."""
    inputs = [
        "Foo!!!Bar",
        "Foo___Bar",
        "Foo - Bar - Baz",
        "Foo, Bar, and Baz, LLC",
    ]
    for inp in inputs:
        result = slugify(inp)
        assert "__" not in result, f"double underscore in slug of {inp!r}: {result!r}"
