"""One-off normaliser — bring every ``funders.excluded_industries`` entry
to the canonical lowercase-hyphenated form the extraction prompt
documents (Rule 5 in ``aegis.funders.prompts``).

Plan 1.4. DEFAULT MODE: ``--dry-run`` (no writes). Walks every active
funder, computes the canonical form for each entry on
``excluded_industries``, and prints a diff for every funder whose
list isn't already canonical. Pass ``--apply`` to actually upsert
the normalised rows.

Why this is cosmetic, not load-bearing: the matcher in
``aegis.scoring.match_funders`` already lower-cases both sides before
comparing (``industries.py:339`` / line: ``naics = (deal.industry_naics
or "").lower()``). So mixed casing today is operator-facing chrome
inconsistency, not a behavioural defect. The script exists because
the operator's UI / detail page is easier to scan when every funder
follows the same convention.

Canonicalisation:
  * Lowercase every character.
  * Trim leading/trailing whitespace.
  * Collapse internal whitespace to single hyphens.
  * Strip any other punctuation (commas, slashes, periods) — replace
    with hyphens.
  * Drop entries that canonicalise to the empty string.
  * De-duplicate while preserving first-seen order.

Examples (verbatim, from the prompt's canonical glossary):

  ``"Trucking"``               → ``"trucking"``
  ``"Adult Entertainment"``    → ``"adult-entertainment"``
  ``"auto sales"``             → ``"auto-sales"``
  ``"bail / bonds"``           → ``"bail-bonds"``
  ``"  Check Cashing  "``      → ``"check-cashing"``

Idempotent: passing an already-canonical list through canonicalises
unchanged. Re-running the script on a fully-normalised corpus is a
no-op (no rows reported as needing changes).

Usage on the box, with ``/etc/aegis/aegis.env`` sourced::

    set -a; source /etc/aegis/aegis.env; set +a
    cd /opt/aegis
    .venv/bin/python scripts/normalise_funder_industries.py            # dry-run
    .venv/bin/python scripts/normalise_funder_industries.py --apply    # commits

Read-then-update via ``FunderRepository.upsert`` — the existing write
path used by the manual create form (``/ui/funders/new``) and by
seed scripts (``scripts/audit/seed_shor_capital.py``). No new DB
machinery.

Exit codes:
  0 — no work needed (corpus already canonical) OR ``--apply``
      succeeded across every changed row.
  1 — at least one row needed normalisation but ``--apply`` was not
      passed. Non-zero so CI / shell-chain detects "drift present".
  2 — runtime error (DB unreachable, upsert failure, etc.).
"""

from __future__ import annotations

import argparse
import re
import sys
import traceback
from dataclasses import dataclass
from typing import Final, Protocol

from aegis.funders.models import FunderRow

EXIT_OK: Final[int] = 0
EXIT_DRIFT_PRESENT: Final[int] = 1
EXIT_RUNTIME_ERROR: Final[int] = 2


# ─────────────────────────────────────────────────────────────────────
# Canonicalisation — pure helpers, fully testable
# ─────────────────────────────────────────────────────────────────────


# Non-alphanumeric run → single hyphen. Captures whitespace,
# punctuation, slashes, commas, etc. in one pass.
_NON_ALNUM_RUN: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")


def canonicalise_industry(raw: str) -> str:
    """Normalise one industry tag to the canonical lowercase-hyphenated
    form.

    Empty input → empty output (the caller drops empties).
    """
    if not raw:
        return ""
    lowered = raw.lower().strip()
    # Replace any run of non-alphanumeric chars with a single hyphen,
    # then trim any leading/trailing hyphen the substitution left.
    return _NON_ALNUM_RUN.sub("-", lowered).strip("-")


def canonicalise_list(raw: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Normalise every entry, drop empties, de-duplicate (first-wins).

    Order is preserved so the operator's eyeball-diff matches what
    they'd expect (no spooky reshuffles).
    """
    seen: set[str] = set()
    out: list[str] = []
    for entry in raw:
        canon = canonicalise_industry(entry)
        if not canon:
            continue
        if canon in seen:
            continue
        seen.add(canon)
        out.append(canon)
    return tuple(out)


# ─────────────────────────────────────────────────────────────────────
# Diff representation
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FunderDiff:
    """One row's before/after — empty ``after`` minus ``before`` set
    means no change."""

    funder_id: str
    funder_name: str
    before: tuple[str, ...]
    after: tuple[str, ...]

    @property
    def needs_change(self) -> bool:
        return self.before != self.after


def compute_diff(funder: FunderRow) -> FunderDiff:
    """Pure: compute the canonical form and the diff for one funder."""
    before = tuple(funder.excluded_industries or ())
    after = canonicalise_list(list(before))
    return FunderDiff(
        funder_id=str(funder.id),
        funder_name=funder.name,
        before=before,
        after=after,
    )


# ─────────────────────────────────────────────────────────────────────
# Repository adapter
# ─────────────────────────────────────────────────────────────────────


class _FunderSource(Protocol):
    """Minimal contract — both ``InMemoryFunderRepository`` and
    ``SupabaseFunderRepository`` satisfy via ``list_active`` and
    ``upsert``."""

    def list_active(self) -> list[FunderRow]: ...

    def upsert(self, funder: FunderRow) -> FunderRow: ...


def collect_diffs(source: _FunderSource) -> list[FunderDiff]:
    """Build the diff list for every active funder. Pure read; no
    writes."""
    return [compute_diff(f) for f in source.list_active()]


def apply_diffs(source: _FunderSource, diffs: list[FunderDiff]) -> int:
    """Upsert every changed row's normalised industries via the
    existing ``FunderRepository.upsert`` path. Returns the count of
    rows actually written. Rows that don't need a change are skipped
    (no-op upsert pollution).
    """
    written = 0
    for d in diffs:
        if not d.needs_change:
            continue
        existing = next(
            (f for f in source.list_active() if str(f.id) == d.funder_id),
            None,
        )
        if existing is None:
            # Should not happen — the diff was built from list_active —
            # but defend against a concurrent delete.
            continue
        updated = existing.model_copy(update={"excluded_industries": d.after})
        source.upsert(updated)
        written += 1
    return written


# ─────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────


def render_diff_report(diffs: list[FunderDiff]) -> str:
    """Operator-facing summary. Only renders the rows that changed —
    a clean corpus produces a one-line "no drift" report."""
    changes = [d for d in diffs if d.needs_change]
    if not changes:
        return f"# scanned {len(diffs)} funder(s); no drift — corpus is canonical.\n"

    lines: list[str] = []
    for d in changes:
        lines.append(f"## {d.funder_name}  ({d.funder_id})")
        lines.append(f"  before: {list(d.before)!r}")
        lines.append(f"  after:  {list(d.after)!r}")
        added = set(d.after) - set(d.before)
        removed = set(d.before) - set(d.after)
        canonicalised = set(d.before) & set(d.after)
        if removed:
            lines.append(f"  removed: {sorted(removed)!r}")
        if added:
            lines.append(f"  added:   {sorted(added)!r}")
        if canonicalised:
            lines.append(f"  kept:    {sorted(canonicalised)!r}")
        lines.append("")
    lines.append(
        f"# scanned {len(diffs)} funder(s); {len(changes)} need normalisation."
    )
    lines.append("")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Normalise every funder's excluded_industries list to the "
            "canonical lowercase-hyphenated form. Default mode is dry-run."
        )
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Actually upsert the normalised rows. Default is dry-run "
            "(report only). Per global rule 2 — production writes "
            "require explicit operator authorisation."
        ),
    )
    return p.parse_args()


def _load_repository() -> _FunderSource:
    """Lazy import so unit tests don't require Supabase env vars."""
    from aegis.funders.repository import SupabaseFunderRepository

    return SupabaseFunderRepository()


def main() -> int:
    args = _parse_args()
    try:
        repo = _load_repository()
    except Exception as exc:
        print(f"ERROR: could not initialise funder repository: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    try:
        diffs = collect_diffs(repo)
    except Exception as exc:
        print(f"ERROR: collect_diffs failed: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    print(render_diff_report(diffs))

    drifty = [d for d in diffs if d.needs_change]
    if not drifty:
        return EXIT_OK

    if not args.apply:
        print(
            f"# {len(drifty)} funder(s) need normalisation. "
            "Re-run with --apply to commit (writes to prod Supabase).",
            file=sys.stderr,
        )
        return EXIT_DRIFT_PRESENT

    try:
        written = apply_diffs(repo, drifty)
    except Exception as exc:
        print(f"ERROR: apply_diffs failed: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    print(f"# normalised {written} funder(s).", file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
