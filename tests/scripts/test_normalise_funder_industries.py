"""Unit tests for ``scripts/normalise_funder_industries.py``.

Covers the pure-function core:

* ``canonicalise_industry`` on every shape the operator-facing data
  exhibits (Title Case, spaces, mixed punctuation, leading/trailing
  whitespace, empties).
* ``canonicalise_list`` preserves first-seen order, drops empties,
  de-duplicates.
* ``compute_diff`` / ``FunderDiff.needs_change`` discriminate
  already-canonical rows from drifty ones.
* ``collect_diffs`` walks ``list_active`` without writes.
* ``apply_diffs`` only upserts rows that need changes (no no-op
  pollution).
* ``render_diff_report`` produces the operator-readable summary.
* Idempotency: running twice on a canonicalised corpus is a no-op.

No DB calls. All in-memory fakes.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from aegis.funders.models import FunderRow  # noqa: E402
from scripts import normalise_funder_industries as norm  # noqa: E402


def _funder(name: str, *industries: str) -> FunderRow:
    return FunderRow(
        id=uuid4(),
        name=name,
        excluded_industries=tuple(industries),
        min_monthly_revenue=Decimal("25000.00"),
    )


# ----------------------------------------------------------------------
# canonicalise_industry — every operator-data shape
# ----------------------------------------------------------------------


def test_canonicalise_lowercases_title_case() -> None:
    assert norm.canonicalise_industry("Trucking") == "trucking"


def test_canonicalise_replaces_spaces_with_hyphens() -> None:
    assert norm.canonicalise_industry("Adult Entertainment") == "adult-entertainment"


def test_canonicalise_collapses_double_spaces() -> None:
    assert norm.canonicalise_industry("auto    sales") == "auto-sales"


def test_canonicalise_strips_outer_whitespace() -> None:
    assert norm.canonicalise_industry("  Check Cashing  ") == "check-cashing"


def test_canonicalise_replaces_slashes_and_commas() -> None:
    assert norm.canonicalise_industry("bail / bonds") == "bail-bonds"
    assert norm.canonicalise_industry("oil, gas") == "oil-gas"


def test_canonicalise_strips_periods() -> None:
    assert norm.canonicalise_industry("e.g. trucking") == "e-g-trucking"


def test_canonicalise_drops_leading_trailing_hyphens() -> None:
    """Substitution may leave a leading/trailing hyphen; strip it."""
    assert norm.canonicalise_industry("- Trucking -") == "trucking"
    assert norm.canonicalise_industry("/// retail ///") == "retail"


def test_canonicalise_empty_input() -> None:
    assert norm.canonicalise_industry("") == ""
    assert norm.canonicalise_industry("   ") == ""
    assert norm.canonicalise_industry("///") == ""


def test_canonicalise_is_idempotent() -> None:
    """Re-applying canonicalisation produces the same result."""
    once = norm.canonicalise_industry("Adult Entertainment")
    twice = norm.canonicalise_industry(once)
    assert once == twice


# ----------------------------------------------------------------------
# canonicalise_list — ordering + de-dup
# ----------------------------------------------------------------------


def test_canonicalise_list_drops_empties() -> None:
    assert norm.canonicalise_list(["Trucking", "", "  ", "Retail"]) == (
        "trucking",
        "retail",
    )


def test_canonicalise_list_dedupes_first_wins() -> None:
    """Two entries that canonicalise to the same value — keep first."""
    assert norm.canonicalise_list(["Trucking", "trucking", "TRUCKING"]) == (
        "trucking",
    )


def test_canonicalise_list_preserves_order() -> None:
    """Operator's eyeball-diff matches the input ordering, modulo
    casing changes."""
    out = norm.canonicalise_list(["Retail", "Trucking", "Adult Entertainment"])
    assert out == ("retail", "trucking", "adult-entertainment")


def test_canonicalise_list_empty_input() -> None:
    assert norm.canonicalise_list([]) == ()
    assert norm.canonicalise_list(("",)) == ()


# ----------------------------------------------------------------------
# compute_diff / FunderDiff
# ----------------------------------------------------------------------


def test_compute_diff_clean_row_needs_no_change() -> None:
    """Already-canonical input → needs_change is False."""
    f = _funder("Already Clean Fund", "trucking", "retail")
    diff = norm.compute_diff(f)
    assert diff.before == ("trucking", "retail")
    assert diff.after == ("trucking", "retail")
    assert diff.needs_change is False


def test_compute_diff_drifty_row_needs_change() -> None:
    f = _funder("Title-Case Fund", "Trucking", "Adult Entertainment")
    diff = norm.compute_diff(f)
    assert diff.before == ("Trucking", "Adult Entertainment")
    assert diff.after == ("trucking", "adult-entertainment")
    assert diff.needs_change is True


def test_compute_diff_with_empty_industries() -> None:
    f = _funder("No Exclusions Fund")
    diff = norm.compute_diff(f)
    assert diff.before == ()
    assert diff.after == ()
    assert diff.needs_change is False


# ----------------------------------------------------------------------
# collect_diffs / apply_diffs — repository iteration
# ----------------------------------------------------------------------


@dataclass
class _FakeRepo:
    funders: list[FunderRow] = field(default_factory=list)
    upsert_log: list[FunderRow] = field(default_factory=list)

    def list_active(self) -> list[FunderRow]:
        return list(self.funders)

    def upsert(self, funder: FunderRow) -> FunderRow:
        self.upsert_log.append(funder)
        for i, existing in enumerate(self.funders):
            if existing.id == funder.id:
                self.funders[i] = funder
                return funder
        self.funders.append(funder)
        return funder


def test_collect_diffs_mixed_corpus() -> None:
    repo = _FakeRepo(
        funders=[
            _funder("Clean Fund", "trucking", "retail"),
            _funder("Title Fund", "Trucking", "Retail"),
            _funder("Mixed Fund", "Adult Entertainment", "bail / bonds"),
        ]
    )
    diffs = norm.collect_diffs(repo)
    assert len(diffs) == 3
    by_name = {d.funder_name: d for d in diffs}
    assert by_name["Clean Fund"].needs_change is False
    assert by_name["Title Fund"].needs_change is True
    assert by_name["Mixed Fund"].needs_change is True


def test_apply_diffs_only_upserts_changed_rows() -> None:
    """A clean row should NOT be upserted — wastes a DB round-trip and
    adds noise to audit_log."""
    repo = _FakeRepo(
        funders=[
            _funder("Clean Fund", "trucking", "retail"),
            _funder("Title Fund", "Trucking", "Retail"),
        ]
    )
    diffs = norm.collect_diffs(repo)
    written = norm.apply_diffs(repo, diffs)
    assert written == 1
    assert len(repo.upsert_log) == 1
    assert repo.upsert_log[0].name == "Title Fund"
    assert repo.upsert_log[0].excluded_industries == ("trucking", "retail")


def test_apply_diffs_is_idempotent_after_run() -> None:
    """Re-running on the same corpus after a successful apply is a no-op."""
    repo = _FakeRepo(
        funders=[
            _funder("Title Fund", "Trucking", "Retail"),
        ]
    )
    first = norm.apply_diffs(repo, norm.collect_diffs(repo))
    assert first == 1
    # Second pass — corpus is now canonical.
    second = norm.apply_diffs(repo, norm.collect_diffs(repo))
    assert second == 0


# ----------------------------------------------------------------------
# render_diff_report
# ----------------------------------------------------------------------


def test_render_clean_corpus_emits_one_line() -> None:
    diffs = [
        norm.FunderDiff(
            funder_id=str(uuid4()),
            funder_name="Clean",
            before=("trucking",),
            after=("trucking",),
        )
    ]
    out = norm.render_diff_report(diffs)
    assert "no drift" in out
    assert "scanned 1" in out


def test_render_drifty_corpus_names_each_row() -> None:
    fid = uuid4()
    diffs = [
        norm.FunderDiff(
            funder_id=str(fid),
            funder_name="Title Fund",
            before=("Trucking", "Retail"),
            after=("trucking", "retail"),
        )
    ]
    out = norm.render_diff_report(diffs)
    assert "Title Fund" in out
    assert str(fid) in out
    assert "before:" in out
    assert "after:" in out


# ----------------------------------------------------------------------
# Exit code constants
# ----------------------------------------------------------------------


def test_exit_codes_are_documented_values() -> None:
    assert norm.EXIT_OK == 0
    assert norm.EXIT_DRIFT_PRESENT == 1
    assert norm.EXIT_RUNTIME_ERROR == 2
