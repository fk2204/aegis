"""Targeted unit tests for ``aegis.parser.metadata``.

Covers the EOF-marker regex (T5) and the synthetic-corpus tampered fixtures
that exercise the incremental-saves signal end-to-end.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aegis.parser.metadata import _EOF_PATTERN, _count_eof_markers

_CORPUS_DIR = (
    Path(__file__).parent.parent / "fixtures" / "corpus" / "synthetic"
)
_TAMPERED_FIXTURES = sorted(_CORPUS_DIR.glob("metadata_tampered_*.pdf"))


def test_eof_regex_matches_lf_and_crlf_terminators() -> None:
    """Regex must accept LF, CRLF, CR, and end-of-file as terminators."""
    for trailer, expected in (
        (b"x %%EOF\n", 1),
        (b"x %%EOF\r\n", 1),
        (b"x %%EOF\r", 1),
        (b"x %%EOF", 1),  # end-of-file
        (b"x %%EOF\n%%EOF\n", 2),
    ):
        assert len(_EOF_PATTERN.findall(trailer)) == expected, trailer


def test_eof_regex_skips_inline_bytes() -> None:
    """%%EOF appearing mid-stream (no EOL after it) must NOT be counted.

    This is the exact false-positive class fixed in T5.
    """
    embedded = b"...content stream %%EOFinline_continuation more bytes..."
    assert len(_EOF_PATTERN.findall(embedded)) == 0


@pytest.mark.skipif(
    not _TAMPERED_FIXTURES,
    reason="no metadata_tampered_*.pdf fixtures present",
)
@pytest.mark.parametrize(
    "fixture", _TAMPERED_FIXTURES, ids=[p.name for p in _TAMPERED_FIXTURES]
)
def test_metadata_tampered_corpus_reports_two_eof_markers(fixture: Path) -> None:
    """Each ``metadata_tampered_*`` corpus PDF must still report 2 EOF markers
    after the regex change. Confirms the new check still catches the
    incremental-save signal these fixtures are designed to encode.
    """
    raw = fixture.read_bytes()
    assert _count_eof_markers(raw) == 2, (
        f"{fixture.name}: expected 2 EOF markers under the new regex"
    )
