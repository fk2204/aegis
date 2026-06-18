"""Merge a freshly-extracted funder preview onto the existing AEGIS DB row.

Distinct from ``aegis.funders.extract.merge_extractions`` — that merges
multiple per-document extractions into a single preview *before* DB
write. This module merges one preview against the *existing* DB row,
so re-extracting against a fresh guidelines doc never silently
overwrites operator-curated content.

Policy
------

* ``id`` and ``name`` from existing always win — id keys the upsert,
  name is the operator's display string and can differ from the legal
  name the LLM picks up.
* ``notes`` and ``operator_notes`` from existing always win when set —
  they're operator-write-only fields.
* ``PRESERVE_IF_POPULATED`` lists fields the operator curates by hand;
  the existing value wins whenever it's non-empty, no matter what the
  fresh extract has. Take the new value only when existing is empty.
* ``notes_residual``: concatenated with a date-stamped separator when
  both sides have content (existing alone if new is empty).
* Every other scalar / list field: take new when populated, fall back
  to existing when new is empty. (The "never regress on a fill" half
  of the merge.)
* Provenance fields (``guidelines_extracted_at``,
  ``guidelines_source_pdf_hash``): always take new — they track the
  current extraction.

Returns a deep-copied preview dict — never mutates the input.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any, Final

# Operator-curated fields. The merge MUST NOT silently overwrite these.
# Take new only when existing is null/empty. Grew out of the 2026-06-18
# funder re-extraction pass:
#
#   - UCS (United Capital Source): accepts_stacking flipped True→False;
#     contact_phone "646-448-1711"→"855-WE-FUND-U"; both emails
#     downgraded from ISO-specific to generic info@; conditional_requirements
#     replaced operator's BK/judgments notes with a standard stip list.
#   - VCG (Velocity Capital Group): excluded_industries lost the
#     "construction-1st-position" qualifier.
#   - Logic Advance: accepts_stacking flipped; excluded_industries lost
#     the "only restricted if <24mo TIB OR <$100K revenue" qualifiers
#     on trucking/construction.
#
# All nine fields were rolled back manually with
# ``funder.field_rollback`` audit rows. This frozenset enforces the
# rule that produced the lesson.
PRESERVE_IF_POPULATED: Final[frozenset[str]] = frozenset(
    {
        "accepts_stacking",
        "contact_phone",
        "contact_email",
        "submission_email",
        "conditional_requirements",
        "excluded_industries",
        "operator_notes",
    }
)

# Fields the merge function never touches — id/name keys the upsert,
# notes/operator_notes are operator-only, notes_residual gets the
# concat-with-separator special case below, and the timestamps belong
# to whichever side is the source of truth for that column.
_MERGE_SKIP: Final[frozenset[str]] = frozenset(
    {
        "id",
        "name",
        "notes",
        "operator_notes",
        "notes_residual",
        "guidelines_extracted_at",
        "guidelines_source_pdf_hash",
        "created_at",
        "updated_at",
    }
)


def _is_empty(value: object) -> bool:
    """Treat None, empty strings, and empty containers as "no value"."""
    return value is None or value == "" or value == [] or value == {}


def _resolve_extracted_at(override: datetime | None, draft: dict[str, Any]) -> datetime:
    if override is not None:
        return override
    raw = draft.get("guidelines_extracted_at")
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(UTC)


def merge_preview_with_existing(
    existing: dict[str, Any],
    preview: dict[str, Any],
    *,
    extracted_at: datetime | None = None,
) -> dict[str, Any]:
    """Return a new preview dict merging ``preview`` onto ``existing``.

    ``existing`` is a full funder row as returned by Supabase (one row
    of ``funders`` table, dict-shaped with all FunderRow columns).
    ``preview`` is the ``FunderGuidelineExtraction``-shaped dict from
    ``add_funder.py extract --output`` (has ``draft``, ``confidence_by_field``,
    etc.).

    ``extracted_at`` overrides the timestamp stamped into the
    ``notes_residual`` separator when both sides have content. When
    omitted, falls back to ``preview['draft']['guidelines_extracted_at']``
    (string or datetime), else UTC now.
    """
    merged = copy.deepcopy(preview)
    draft = merged["draft"]

    # 1) id and name always come from existing.
    draft["id"] = existing["id"]
    draft["name"] = existing["name"]

    # 2) notes / operator_notes: existing wins when populated.
    for key in ("notes", "operator_notes"):
        existing_value = existing.get(key)
        if not _is_empty(existing_value):
            draft[key] = existing_value

    # 3) notes_residual: concat when both populated, existing alone otherwise.
    existing_residual = (existing.get("notes_residual") or "").strip()
    new_residual = (draft.get("notes_residual") or "").strip()
    if existing_residual and new_residual:
        date_str = _resolve_extracted_at(extracted_at, draft).date().isoformat()
        draft["notes_residual"] = (
            f"{existing_residual} | GUIDELINES ({date_str} extract): {new_residual}"
        )
    elif existing_residual and not new_residual:
        draft["notes_residual"] = existing_residual

    # 4) Field-by-field merge.
    for key, existing_value in existing.items():
        if key in _MERGE_SKIP:
            continue
        if key in PRESERVE_IF_POPULATED and not _is_empty(existing_value):
            # Operator-curated: keep existing no matter what.
            draft[key] = existing_value
            continue
        # Default: take new when populated, fall back to existing.
        new_value = draft.get(key)
        if _is_empty(new_value) and not _is_empty(existing_value):
            draft[key] = existing_value

    return merged


__all__ = [
    "PRESERVE_IF_POPULATED",
    "merge_preview_with_existing",
]
