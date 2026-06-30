"""Unit tests for ``scripts/sync_funders_from_folder.py``.

Covers the multi-file refactor:

* ``_get_all_supported_files`` — priority-sorted iteration, ``~$`` lock
  files filtered, hidden files filtered.
* ``_folder_hash`` — deterministic, sensitive to bytes + names, stable
  across iteration order.
* ``_merge_extractions`` — first-non-None scalar wins, list dedup-union,
  ``notes`` first-non-empty, empty-input passthrough, ``model_validate``
  contract preserved.
* ``_format_file_size`` / ``_content_label_for`` — dry-run helpers.

No Bedrock calls, no Supabase access. Pure-function tests + small
tmp_path fixtures.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from aegis.funders.guidelines_extract import FunderGuidelinesExtraction  # noqa: E402
from scripts import sync_funders_from_folder as sff  # noqa: E402

# ----------------------------------------------------------------------
# _get_all_supported_files
# ----------------------------------------------------------------------


def _write(folder: Path, name: str, payload: bytes = b"x") -> Path:
    p = folder / name
    p.write_bytes(payload)
    return p


def test_get_all_supported_files_returns_priority_sorted(tmp_path: Path) -> None:
    """PDFs first (largest first), then text-extracted (largest first),
    then images (largest first). Within each tier, size descending."""
    folder = tmp_path / "Funder A"
    folder.mkdir()
    small_pdf = _write(folder, "small.pdf", b"x" * 100)
    big_pdf = _write(folder, "big.pdf", b"x" * 5000)
    docx = _write(folder, "matrix.docx", b"x" * 1000)
    xlsx = _write(folder, "rates.xlsx", b"x" * 200)
    big_png = _write(folder, "shot.png", b"x" * 800)
    small_png = _write(folder, "thumb.png", b"x" * 50)

    out = sff._get_all_supported_files(folder)

    assert out == [big_pdf, small_pdf, docx, xlsx, big_png, small_png]


def test_get_all_supported_files_skips_lock_and_hidden(tmp_path: Path) -> None:
    """``~$lockfile.docx`` and ``.DS_Store`` are excluded."""
    folder = tmp_path / "Funder B"
    folder.mkdir()
    real_pdf = _write(folder, "guidelines.pdf")
    _write(folder, "~$MCA_Funder_Manual.docx")
    _write(folder, ".DS_Store")

    out = sff._get_all_supported_files(folder)

    assert out == [real_pdf]


def test_get_all_supported_files_ignores_subfolders(tmp_path: Path) -> None:
    """Sub-subfolders are walked over — only immediate children count."""
    folder = tmp_path / "Funder C"
    folder.mkdir()
    nested = folder / "old"
    nested.mkdir()
    _write(nested, "buried.pdf")
    top_pdf = _write(folder, "top.pdf")

    out = sff._get_all_supported_files(folder)

    assert out == [top_pdf]


def test_get_all_supported_files_empty_folder_returns_empty(tmp_path: Path) -> None:
    folder = tmp_path / "Empty"
    folder.mkdir()

    assert sff._get_all_supported_files(folder) == []


def test_get_all_supported_files_skips_unknown_extensions(tmp_path: Path) -> None:
    """``.zip``, ``.eml``, etc. should not appear in the returned list."""
    folder = tmp_path / "Funder D"
    folder.mkdir()
    real_pdf = _write(folder, "g.pdf")
    _write(folder, "junk.zip")
    _write(folder, "thread.eml")

    out = sff._get_all_supported_files(folder)

    assert out == [real_pdf]


# ----------------------------------------------------------------------
# _folder_hash
# ----------------------------------------------------------------------


def test_folder_hash_is_deterministic(tmp_path: Path) -> None:
    """Two calls on the same folder return the same hex digest."""
    folder = tmp_path / "Funder E"
    folder.mkdir()
    _write(folder, "a.pdf", b"alpha")
    _write(folder, "b.png", b"beta")

    h1 = sff._folder_hash(folder)
    h2 = sff._folder_hash(folder)

    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_folder_hash_changes_when_file_content_changes(tmp_path: Path) -> None:
    folder = tmp_path / "Funder F"
    folder.mkdir()
    f = _write(folder, "a.pdf", b"alpha")
    before = sff._folder_hash(folder)
    f.write_bytes(b"alpha-modified")
    after = sff._folder_hash(folder)

    assert before != after


def test_folder_hash_changes_when_file_added(tmp_path: Path) -> None:
    folder = tmp_path / "Funder G"
    folder.mkdir()
    _write(folder, "a.pdf", b"alpha")
    before = sff._folder_hash(folder)
    _write(folder, "b.docx", b"beta")
    after = sff._folder_hash(folder)

    assert before != after


def test_folder_hash_changes_when_file_renamed(tmp_path: Path) -> None:
    """Renaming changes the digest — the name is mixed in alongside bytes."""
    folder = tmp_path / "Funder H"
    folder.mkdir()
    f = _write(folder, "a.pdf", b"alpha")
    before = sff._folder_hash(folder)
    f.rename(folder / "renamed.pdf")
    after = sff._folder_hash(folder)

    assert before != after


def test_folder_hash_stable_regardless_of_creation_order(tmp_path: Path) -> None:
    """Creating files A then B vs B then A yields the same digest because
    the hash visits files in name-sorted order."""
    folder1 = tmp_path / "order1"
    folder1.mkdir()
    _write(folder1, "a.pdf", b"alpha")
    _write(folder1, "b.docx", b"beta")

    folder2 = tmp_path / "order2"
    folder2.mkdir()
    _write(folder2, "b.docx", b"beta")
    _write(folder2, "a.pdf", b"alpha")

    assert sff._folder_hash(folder1) == sff._folder_hash(folder2)


# ----------------------------------------------------------------------
# _merge_extractions
# ----------------------------------------------------------------------


def _ext(**kwargs: object) -> FunderGuidelinesExtraction:
    """Convenience constructor for test extractions. Empty defaults match
    the Pydantic model."""
    return FunderGuidelinesExtraction.model_validate(kwargs)


def test_merge_first_non_none_scalar_wins() -> None:
    """When file A has min_revenue and file B also has min_revenue,
    A's value wins (priority order from file sort = first in list)."""
    a = _ext(min_revenue="25000.00")
    b = _ext(min_revenue="50000.00")

    out = sff._merge_extractions([a, b])

    assert out is not None
    assert out.min_revenue == "25000.00"


def test_merge_fills_missing_scalar_from_later_extraction() -> None:
    """File A has only min_revenue; file B fills in min_fico."""
    a = _ext(min_revenue="25000.00")
    b = _ext(min_fico=650)

    out = sff._merge_extractions([a, b])

    assert out is not None
    assert out.min_revenue == "25000.00"
    assert out.min_fico == 650


def test_merge_lists_dedup_union() -> None:
    """excluded_states across multiple extractions dedup-union — order
    preserved by first appearance."""
    a = _ext(excluded_states=["CA", "NY"])
    b = _ext(excluded_states=["NY", "VT"])
    c = _ext(excluded_states=["CA", "AK"])

    out = sff._merge_extractions([a, b, c])

    assert out is not None
    assert out.excluded_states == ["CA", "NY", "VT", "AK"]


def test_merge_lists_with_empty_passthrough() -> None:
    """Empty list in one file shouldn't drop the values from another."""
    a = _ext(excluded_industries=[])
    b = _ext(excluded_industries=["cannabis", "adult"])

    out = sff._merge_extractions([a, b])

    assert out is not None
    assert out.excluded_industries == ["cannabis", "adult"]


def test_merge_notes_first_non_empty_wins() -> None:
    """Notes empty string is treated as 'absent' — first real text wins."""
    a = _ext(notes="")
    b = _ext(notes="real notes from PDF")
    c = _ext(notes="notes from screenshot")

    out = sff._merge_extractions([a, b, c])

    assert out is not None
    assert out.notes == "real notes from PDF"


def test_merge_all_none_returns_default_extraction() -> None:
    """All-empty inputs collapse to a default-shaped extraction
    (empty lists, empty notes, None scalars) — NOT None."""
    a = _ext()
    b = _ext()

    out = sff._merge_extractions([a, b])

    assert out is not None
    assert out.min_revenue is None
    assert out.min_fico is None
    assert out.excluded_states == []
    assert out.excluded_industries == []
    assert out.notes == ""


def test_merge_empty_input_returns_none() -> None:
    assert sff._merge_extractions([]) is None


def test_merge_round_trips_through_model_validate() -> None:
    """The merged result is a real FunderGuidelinesExtraction, so the
    same range / coercion contract holds (e.g. min_fico bounds)."""
    a = _ext(min_fico=650, min_tib_months=24)
    b = _ext(max_positions=2, stacking_policy="not_allowed")

    out = sff._merge_extractions([a, b])

    assert out is not None
    assert isinstance(out, FunderGuidelinesExtraction)
    assert out.min_fico == 650
    assert out.min_tib_months == 24
    assert out.max_positions == 2
    assert out.stacking_policy == "not_allowed"


def test_merge_single_extraction_is_identity() -> None:
    """One-file input round-trips through model_validate but preserves
    every field."""
    only = _ext(
        min_revenue="25000.00",
        min_fico=650,
        excluded_states=["CA"],
        notes="single source",
    )

    out = sff._merge_extractions([only])

    assert out is not None
    assert out.min_revenue == "25000.00"
    assert out.min_fico == 650
    assert out.excluded_states == ["CA"]
    assert out.notes == "single source"


# ----------------------------------------------------------------------
# Dry-run formatting helpers
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "size,expected",
    [
        (500, "500B"),
        (1024, "1K"),
        (96 * 1024, "96K"),
        (int(2.9 * 1024 * 1024), "2.9M"),
        (1024 * 1024 * 1024, "1.0G"),
    ],
)
def test_format_file_size(size: int, expected: str) -> None:
    assert sff._format_file_size(size) == expected


def test_content_label_for_extensions(tmp_path: Path) -> None:
    assert sff._content_label_for(tmp_path / "x.pdf") == "PDF"
    assert sff._content_label_for(tmp_path / "x.docx") == "DOCX"
    assert sff._content_label_for(tmp_path / "x.doc") == "DOC"
    assert sff._content_label_for(tmp_path / "x.xlsx") == "XLSX"
    assert sff._content_label_for(tmp_path / "x.xls") == "XLS"
    assert sff._content_label_for(tmp_path / "x.txt") == "TXT"
    assert sff._content_label_for(tmp_path / "x.png") == "PNG"
    assert sff._content_label_for(tmp_path / "x.jpg") == "JPG"
    assert sff._content_label_for(tmp_path / "x.jpeg") == "JPEG"
