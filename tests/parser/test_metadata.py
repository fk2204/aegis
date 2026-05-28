"""Targeted unit tests for ``aegis.parser.metadata``.

Covers the EOF-marker regex (T5) and the synthetic-corpus tampered fixtures
that exercise the incremental-saves signal end-to-end.

Phase 9 adds forensic detectors (font_inconsistency, page_layer_anomaly)
that are exercised below against the synthetic-corpus PDFs and against
the clean-fixture baseline.
"""

from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest

from aegis.parser.metadata import (
    _EOF_PATTERN,
    _count_eof_markers,
    _font_inconsistency,
    _has_pdf_signature,
    _page_layer_anomaly,
    analyze_metadata,
)

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


# -- Phase 9: forensic detectors --------------------------------------------


def _clean_synthetic_pdf() -> Path:
    """Pick any clean synthetic-corpus PDF — used as a "no anomalies" baseline."""
    candidates = sorted(_CORPUS_DIR.glob("clean_profitable_*.pdf"))
    if not candidates:
        pytest.skip("no clean_profitable_*.pdf fixture present")
    return candidates[0]


def test_font_inconsistency_returns_none_for_single_page_pdf() -> None:
    """Single-page PDFs cannot exhibit inter-page font inconsistency."""
    with pikepdf.Pdf.new() as pdf:
        pdf.add_blank_page(page_size=(612, 792))
        flag, score = _font_inconsistency(pdf)
    assert flag is None
    assert score == 0


def test_font_inconsistency_returns_none_for_synthetic_clean_pdf() -> None:
    """Synthetic-corpus clean PDFs use one font family — must not flag.

    The synthetic generator builds every page with the same reportlab
    Helvetica fallback. Any false positive here would flag the entire
    corpus as forensic-suspect, defeating the detector.
    """
    fixture = _clean_synthetic_pdf()
    with pikepdf.open(fixture) as pdf:
        flag, score = _font_inconsistency(pdf)
    assert flag is None, f"unexpected font_inconsistency flag on clean corpus: {flag}"
    assert score == 0


def test_page_layer_anomaly_returns_none_for_single_page_pdf() -> None:
    """No inter-page comparison possible on single-page PDFs."""
    with pikepdf.Pdf.new() as pdf:
        pdf.add_blank_page(page_size=(612, 792))
        flag, score = _page_layer_anomaly(pdf)
    assert flag is None
    assert score == 0


def test_page_layer_anomaly_returns_none_for_homogeneous_pages() -> None:
    """A multi-page PDF where every page has the same /Contents shape."""
    with pikepdf.Pdf.new() as pdf:
        pdf.add_blank_page(page_size=(612, 792))
        pdf.add_blank_page(page_size=(612, 792))
        pdf.add_blank_page(page_size=(612, 792))
        flag, score = _page_layer_anomaly(pdf)
    assert flag is None
    assert score == 0


# -- EOF false-positive fix: signature-aware incremental_saves --------------


def _build_test_pdf(
    tmp_path: Path,
    *,
    name: str,
    with_signature: bool,
    extra_eof: bool,
) -> Path:
    """Build a minimal PDF for EOF false-positive detector testing.

    ``with_signature`` adds an /AcroForm with a /Sig field structurally
    valid for presence-detection (no cryptographic content — v1 of the
    fix is presence-based). ``extra_eof`` appends a second %%EOF trailer
    to simulate the kind of incremental save that historically fired the
    incremental_saves flag on every digitally-signed bank export.
    """
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    if with_signature:
        sig_value = pdf.make_indirect(
            pikepdf.Dictionary(
                {
                    "/Type": pikepdf.Name("/Sig"),
                    "/Filter": pikepdf.Name("/Adobe.PPKLite"),
                    "/SubFilter": pikepdf.Name("/adbe.pkcs7.detached"),
                    "/ByteRange": pikepdf.Array([0, 0, 0, 0]),
                    "/Contents": pikepdf.String(""),
                }
            )
        )
        sig_field = pdf.make_indirect(
            pikepdf.Dictionary(
                {
                    "/FT": pikepdf.Name("/Sig"),
                    "/T": pikepdf.String("Signature1"),
                    "/V": sig_value,
                }
            )
        )
        pdf.Root["/AcroForm"] = pikepdf.Dictionary(
            {"/Fields": pikepdf.Array([sig_field])}
        )
    p = tmp_path / name
    pdf.save(str(p))
    if extra_eof:
        raw = p.read_bytes()
        if not raw.endswith(b"\n"):
            raw += b"\n"
        raw += b"%%EOF\n"
        p.write_bytes(raw)
    return p


def test_has_pdf_signature_returns_false_for_unsigned_pdf(tmp_path: Path) -> None:
    p = _build_test_pdf(
        tmp_path, name="unsigned.pdf", with_signature=False, extra_eof=False
    )
    with pikepdf.open(p) as pdf:
        assert _has_pdf_signature(pdf) is False


def test_has_pdf_signature_returns_true_for_signed_pdf(tmp_path: Path) -> None:
    p = _build_test_pdf(
        tmp_path, name="signed.pdf", with_signature=True, extra_eof=False
    )
    with pikepdf.open(p) as pdf:
        assert _has_pdf_signature(pdf) is True


def test_incremental_saves_flag_suppressed_when_signed(tmp_path: Path) -> None:
    """Signed PDFs naturally carry ≥2 EOFs (each signature is an
    incremental update). The fix suppresses incremental_saves on them so
    legitimate signed bank exports stop dominating the manual_review queue.
    """
    p = _build_test_pdf(
        tmp_path,
        name="signed_multi_eof.pdf",
        with_signature=True,
        extra_eof=True,
    )
    raw = p.read_bytes()
    assert _count_eof_markers(raw) >= 2, "test fixture must carry ≥2 EOF markers"
    result = analyze_metadata(p)
    assert not any(
        f.startswith("incremental_saves") for f in result.flags
    ), f"signed multi-EOF PDF should not flag incremental_saves; got {result.flags}"


def test_incremental_saves_flag_fires_when_unsigned(tmp_path: Path) -> None:
    """Unsigned PDFs with multi-EOF still trigger the tampering flag.

    Confirms the fix didn't over-correct — without a /Sig object, the
    multi-EOF signal is still suspicious and contributes the original +40
    to the metadata fraud score.
    """
    p = _build_test_pdf(
        tmp_path,
        name="unsigned_multi_eof.pdf",
        with_signature=False,
        extra_eof=True,
    )
    raw = p.read_bytes()
    assert _count_eof_markers(raw) >= 2, "test fixture must carry ≥2 EOF markers"
    result = analyze_metadata(p)
    matching = [f for f in result.flags if f.startswith("incremental_saves")]
    assert matching, (
        f"unsigned multi-EOF PDF should flag incremental_saves; got {result.flags}"
    )
