"""Funder onboarding wrapper for Claude Code.

Hands a funder PDF/PNG to the existing extraction engine
(``aegis.funders.extract``), surfaces the draft + per-field confidence
for in-chat operator review, then upserts the (possibly edited) draft
via the existing ``FunderRepository`` once the operator has confirmed.

Two-phase by design — extraction is read-only and Bedrock-billed, save
is a single explicit write. **NOT autonomous**: per CLAUDE.md
"extraction assists, never replaces judgment", the save phase MUST be
gated on operator confirmation of the preview in chat. The CLI itself
doesn't enforce that contract — Claude Code does — but the two-phase
shape makes the human-in-the-loop step the natural sequence.

Subcommands::

    python scripts/add_funder.py extract <file>... [--output PATH]
    python scripts/add_funder.py merge --preview PATH [--by name|id]
    python scripts/add_funder.py save --from PATH [--dry-run]

The ``extract`` step writes a JSON preview to stdout (or ``--output``)
and a human-readable summary with low-confidence (<60) fields flagged
to stderr. The preview JSON is the operator's handle: edit it (or have
Claude Code edit it) before invoking ``save --from`` against it.

The ``merge`` step folds the preview onto the existing AEGIS funder
row in place — use it between ``extract`` and ``save`` whenever the
target funder already exists. Without it, ``save`` would replace
operator-curated content (contact info, conditional_requirements,
accepts_stacking, etc.). See ``aegis.funders.merge_existing`` for the
``PRESERVE_IF_POPULATED`` set that codifies the policy.

Reuses everything that already exists; no new write path, no new prompt.
The save call goes through the same ``FunderRepository.upsert`` the
``/ui/funders/import/save`` route uses, so persistence semantics are
identical. An ``audit_log`` row (``funder.imported``,
``actor="claude_code"``) is written after a successful upsert — audit
failures fail the operation per CLAUDE.md.

Exit codes:
  0 — success
  1 — runtime error (file missing, extraction failed, validation, write)
  2 — invalid CLI arguments
"""

from __future__ import annotations

import argparse
import sys
import traceback
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Final, Protocol, TextIO, cast

from pydantic import ValidationError

from aegis.audit import AuditLog
from aegis.funders.extract import (
    FunderExtractionError,
    extract_funder_guidelines,
    extract_funder_guidelines_from_image,
    merge_extractions,
)
from aegis.funders.merge_existing import (
    PRESERVE_IF_POPULATED,
    merge_preview_with_existing,
)
from aegis.funders.models import FunderGuidelineExtraction, FunderRow
from aegis.llm import LLMClient

EXIT_OK: Final[int] = 0
EXIT_RUNTIME_ERROR: Final[int] = 1
EXIT_INVALID_ARGS: Final[int] = 2

LOW_CONFIDENCE_THRESHOLD: Final[int] = 60

_PDF_SUFFIXES: Final[frozenset[str]] = frozenset({".pdf"})
_IMAGE_SUFFIXES: Final[frozenset[str]] = frozenset({".png", ".jpg", ".jpeg"})


# ─────────────────────────────────────────────────────────────────────
# Pure functions — no I/O, no DB, no LLM. Tested in isolation.
# ─────────────────────────────────────────────────────────────────────


def classify_media(path: Path) -> str:
    """Return ``"pdf"`` or ``"image"`` from a path's suffix.

    Returns ``""`` when neither classifier matches. Mirrors the route-side
    ``_classify_funder_import_media`` filename fallback (no content-type
    header on a local file).
    """
    suffix = path.suffix.lower()
    if suffix in _PDF_SUFFIXES:
        return "pdf"
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    return ""


def low_confidence_fields(
    extraction: FunderGuidelineExtraction,
    *,
    threshold: int = LOW_CONFIDENCE_THRESHOLD,
) -> tuple[str, ...]:
    """Return field names whose per-field confidence is below ``threshold``.

    Ordered by ascending confidence — the lowest-confidence field shows
    first when Claude Code presents the preview. Fields not present in
    ``confidence_by_field`` are treated as "no LLM opinion" and omitted.
    """
    pairs = sorted(
        extraction.confidence_by_field.items(),
        key=lambda kv: kv[1],
    )
    return tuple(name for name, conf in pairs if conf < threshold)


def summary_lines(
    extraction: FunderGuidelineExtraction,
    *,
    threshold: int = LOW_CONFIDENCE_THRESHOLD,
) -> list[str]:
    """Human-readable lines for stderr review.

    Renders: funder name, overall confidence, the low-confidence field
    list (always shown, even if empty, so the operator can verify the
    threshold ran), and any unparseable fragments the LLM flagged.
    """
    draft = extraction.draft
    low = low_confidence_fields(extraction, threshold=threshold)
    lines = [
        f"funder: {draft.name}",
        f"overall_confidence: {extraction.overall_confidence}",
        f"low_confidence_fields (<{threshold}): " + (", ".join(low) if low else "none"),
    ]
    if extraction.unparseable_fragments:
        lines.append("unparseable_fragments:")
        for fragment in extraction.unparseable_fragments:
            lines.append(f"  - {fragment}")
    return lines


def preview_to_json(extraction: FunderGuidelineExtraction) -> str:
    """Serialise the extraction to a JSON string suitable for round-trip.

    Uses Pydantic's ``model_dump_json(mode="json")`` semantics so
    ``Decimal`` / ``UUID`` / ``datetime`` survive the round-trip via their
    string representations.
    """
    return extraction.model_dump_json(indent=2)


def preview_from_json(blob: str) -> FunderGuidelineExtraction:
    """Reverse of ``preview_to_json``. Pydantic re-parses scalar coercions."""
    return FunderGuidelineExtraction.model_validate_json(blob)


# ─────────────────────────────────────────────────────────────────────
# Extraction wiring — IO injected via LLMClient + bytes
# ─────────────────────────────────────────────────────────────────────


def extract_one(path_bytes: bytes, kind: str, llm: LLMClient) -> FunderGuidelineExtraction:
    """Route one document through the existing extraction engine.

    ``kind`` is "pdf" or "image". Any other value raises ``ValueError`` —
    the CLI catches that upstream and exits with the runtime-error code.
    """
    if kind == "pdf":
        return extract_funder_guidelines(path_bytes, llm)
    if kind == "image":
        return extract_funder_guidelines_from_image(path_bytes, llm)
    raise ValueError(f"unknown media kind: {kind!r}")


def extract_many(
    items: Sequence[tuple[bytes, str]],
    llm: LLMClient,
) -> FunderGuidelineExtraction:
    """Extract each document, then ``merge_extractions``.

    Empty input is a programmer error — the CLI guards on it before
    calling. Single-item input returns the lone extraction unchanged
    (``merge_extractions`` on a 1-tuple is the identity).
    """
    if not items:
        raise ValueError("no documents to extract")
    parts = [extract_one(blob, kind, llm) for blob, kind in items]
    if len(parts) == 1:
        return parts[0]
    return merge_extractions(parts)


# ─────────────────────────────────────────────────────────────────────
# Save wiring — IO injected via FunderRepository
# ─────────────────────────────────────────────────────────────────────


class _FunderUpserter(Protocol):
    """Subset of ``FunderRepository`` this script touches.

    Both ``SupabaseFunderRepository`` and ``InMemoryFunderRepository``
    satisfy it via their ``upsert`` method.
    """

    def upsert(self, funder: FunderRow) -> FunderRow: ...


def save_extraction(
    extraction: FunderGuidelineExtraction,
    repo: _FunderUpserter,
) -> FunderRow:
    """Upsert the extraction's ``draft`` FunderRow.

    The draft is already a fully-validated FunderRow (Pydantic-strict
    via ``_StrictModel``), so this is a pass-through. Returning the
    saved row lets the CLI surface the canonical id.
    """
    return repo.upsert(extraction.draft)


# ─────────────────────────────────────────────────────────────────────
# CLI — orchestrates pure functions, owns all I/O
# ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="add_funder",
        description=(
            "Funder onboarding wrapper: extract via Bedrock, present "
            "in-chat for operator review, upsert via FunderRepository. "
            "Two-phase by design."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    p_extract = sub.add_parser(
        "extract",
        help="Run LLM extraction on PDF/PNG/JPEG files and print preview JSON.",
    )
    p_extract.add_argument(
        "files",
        nargs="+",
        type=Path,
        help="One or more guideline files (PDF, PNG, JPEG).",
    )
    p_extract.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "Path to write preview JSON. Default: stdout. "
            "Operator workflow: write to a tmp path, edit if needed, then "
            "pass the same path to `save --from`."
        ),
    )

    p_merge = sub.add_parser(
        "merge",
        help=(
            "Fold an extracted preview onto the existing AEGIS funder row. "
            "Run between extract and save when re-extracting a funder that "
            "already exists in AEGIS."
        ),
    )
    p_merge.add_argument(
        "--preview",
        dest="preview_path",
        type=Path,
        required=True,
        help="Path to preview JSON produced by `extract --output` (rewritten in place).",
    )
    p_merge.add_argument(
        "--by",
        choices=("name", "id"),
        default="name",
        help=(
            "How to look up the existing AEGIS row. 'name' (default) matches "
            "preview.draft.name; 'id' matches preview.draft.id."
        ),
    )

    p_save = sub.add_parser(
        "save",
        help="Upsert a previously-extracted (and operator-confirmed) preview.",
    )
    p_save.add_argument(
        "--from",
        dest="from_path",
        type=Path,
        required=True,
        help="Path to the preview JSON produced by `extract --output`.",
    )
    p_save.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Validate the preview round-trips through FunderRow but skip "
            "the repository write. Useful for sanity-checking operator "
            "edits before the live upsert."
        ),
    )

    return p.parse_args(argv)


def _load_llm() -> LLMClient:
    """Lazy import so unit tests don't need Bedrock creds present."""
    from aegis.llm import BedrockClient

    return BedrockClient()


def _load_repository() -> _FunderUpserter:
    """Lazy import so unit tests don't need Supabase creds present."""
    from aegis.funders.repository import SupabaseFunderRepository

    return SupabaseFunderRepository()


def _load_audit() -> AuditLog:
    """Lazy import so unit tests don't need Supabase creds present."""
    from aegis.audit import SupabaseAuditLog

    return SupabaseAuditLog()


def _load_existing_funder(by: str, key: str) -> dict[str, Any]:
    """Fetch one funder row by ``name`` or ``id`` for the merge step.

    Returns the raw Supabase dict (with all columns including
    ``created_at`` / ``updated_at``) — the merge function expects raw
    shape, not a Pydantic-coerced FunderRow. Lazy-imports so unit tests
    can inject a stub without Supabase creds present.
    """
    from aegis.db import get_supabase

    column = "id" if by == "id" else "name"
    result = get_supabase().table("funders").select("*").eq(column, key).execute()
    rows = cast(list[dict[str, Any]], result.data or [])
    if not rows:
        raise ValueError(f"no funder with {column}={key!r} in AEGIS")
    if len(rows) > 1:
        raise ValueError(f"multiple funders with {column}={key!r} — should be unique")
    return rows[0]


def _read_file_bytes(path: Path) -> tuple[bytes, str]:
    """Read ``path`` and return ``(bytes, kind)`` where kind is "pdf"/"image".

    Raises ``ValueError`` if the file is missing or the kind is unknown.
    """
    if not path.is_file():
        raise ValueError(f"not a file: {path}")
    kind = classify_media(path)
    if kind == "":
        raise ValueError(
            f"unsupported media for {path.name!r}: expected .pdf / .png / .jpg / .jpeg"
        )
    return path.read_bytes(), kind


def _default_text_reader(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def run_extract(
    args: argparse.Namespace,
    *,
    llm_factory: Callable[[], LLMClient] = _load_llm,
    bytes_reader: Callable[[Path], tuple[bytes, str]] = _read_file_bytes,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    """Extract subcommand body. IO factories injected for testability."""
    try:
        items: list[tuple[bytes, str]] = []
        for raw_path in args.files:
            path = Path(raw_path)
            file_bytes, kind = bytes_reader(path)
            items.append((file_bytes, kind))
    except ValueError as exc:
        print(f"ERROR: {exc}", file=stderr)
        return EXIT_RUNTIME_ERROR

    try:
        llm = llm_factory()
    except Exception as exc:
        print(f"ERROR: could not initialise LLM client: {exc}", file=stderr)
        traceback.print_exc(file=stderr)
        return EXIT_RUNTIME_ERROR

    try:
        extraction = extract_many(items, llm)
    except FunderExtractionError as exc:
        print(f"ERROR: extraction failed: {exc}", file=stderr)
        return EXIT_RUNTIME_ERROR

    blob = preview_to_json(extraction)
    if args.output is None:
        print(blob, file=stdout)
    else:
        Path(args.output).write_text(blob, encoding="utf-8")

    for line in summary_lines(extraction):
        print(line, file=stderr)
    return EXIT_OK


def run_merge(
    args: argparse.Namespace,
    *,
    existing_loader: Callable[[str, str], dict[str, Any]] = _load_existing_funder,
    text_reader: Callable[[Path], str] = _default_text_reader,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    """Merge subcommand body. Folds preview onto existing AEGIS row in place.

    Reads the preview JSON, looks up the matching DB row (by name or
    id), applies the ``PRESERVE_IF_POPULATED`` policy in
    ``aegis.funders.merge_existing``, and rewrites the preview file.
    Prints a per-field action summary to stderr so the operator sees
    what shifted before invoking ``save``.

    IO factories are injected so the unit tests can stub the DB lookup
    without Supabase creds present.
    """
    import json

    preview_path: Path = args.preview_path
    try:
        blob = text_reader(preview_path)
    except OSError as exc:
        print(f"ERROR: could not read preview: {exc}", file=stderr)
        return EXIT_RUNTIME_ERROR

    try:
        preview = cast(dict[str, Any], json.loads(blob))
    except ValueError as exc:
        print(f"ERROR: preview JSON malformed: {exc}", file=stderr)
        return EXIT_RUNTIME_ERROR

    draft = preview.get("draft")
    if not isinstance(draft, dict):
        print("ERROR: preview JSON missing 'draft' object", file=stderr)
        return EXIT_RUNTIME_ERROR

    by: str = args.by
    key_field = "id" if by == "id" else "name"
    key = draft.get(key_field)
    if not isinstance(key, str) or not key:
        print(
            f"ERROR: preview.draft.{key_field} missing or empty — can't look up existing funder",
            file=stderr,
        )
        return EXIT_RUNTIME_ERROR

    try:
        existing = existing_loader(by, key)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=stderr)
        return EXIT_RUNTIME_ERROR
    except Exception as exc:
        print(f"ERROR: existing-funder lookup failed: {exc}", file=stderr)
        traceback.print_exc(file=stderr)
        return EXIT_RUNTIME_ERROR

    merged = merge_preview_with_existing(existing, preview)
    merged_draft = merged["draft"]

    # Render a tight per-field action summary.
    actions = _summarise_merge_actions(existing, draft, merged_draft)
    print(f"merged {existing['name']!r} (id={existing['id']})", file=stderr)
    print(f"  preserved-by-policy: {len(actions['preserved_policy'])}", file=stderr)
    print(f"  preserved-fallback:  {len(actions['preserved_fallback'])}", file=stderr)
    print(f"  filled-from-new:     {len(actions['filled'])}", file=stderr)
    print(f"  taken-from-new:      {len(actions['changed'])}", file=stderr)
    for field in actions["preserved_policy"]:
        print(f"    [policy] {field}: kept existing", file=stderr)
    for field, ev, nv in actions["changed"]:
        print(f"    [updated] {field}: {ev!r} -> {nv!r}", file=stderr)

    out_blob = json.dumps(merged, indent=2, default=str)
    preview_path.write_text(out_blob, encoding="utf-8")
    print(f"wrote {preview_path}", file=stdout)
    return EXIT_OK


def _summarise_merge_actions(
    existing: dict[str, Any],
    new_draft: dict[str, Any],
    merged_draft: dict[str, Any],
) -> dict[str, Any]:
    """Classify each field in the merged draft for the stderr summary."""
    preserved_policy: list[str] = []
    preserved_fallback: list[str] = []
    filled: list[str] = []
    changed: list[tuple[str, Any, Any]] = []
    for field in merged_draft:
        if field in {
            "id",
            "name",
            "guidelines_extracted_at",
            "guidelines_source_pdf_hash",
            "notes_residual",
            "created_at",
            "updated_at",
        }:
            continue
        ev = existing.get(field)
        nv = new_draft.get(field)
        mv = merged_draft.get(field)
        if mv == ev and mv != nv:
            if field in PRESERVE_IF_POPULATED:
                preserved_policy.append(field)
            else:
                preserved_fallback.append(field)
        elif mv == nv and mv != ev and (ev is None or ev == "" or ev == []):
            filled.append(field)
        elif mv == nv and mv != ev:
            changed.append((field, ev, nv))
    return {
        "preserved_policy": preserved_policy,
        "preserved_fallback": preserved_fallback,
        "filled": filled,
        "changed": changed,
    }


def run_save(
    args: argparse.Namespace,
    *,
    repo_factory: Callable[[], _FunderUpserter] = _load_repository,
    audit_factory: Callable[[], AuditLog] = _load_audit,
    text_reader: Callable[[Path], str] = _default_text_reader,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    """Save subcommand body. IO factories injected for testability."""
    try:
        blob = text_reader(args.from_path)
    except OSError as exc:
        print(f"ERROR: could not read preview: {exc}", file=stderr)
        return EXIT_RUNTIME_ERROR

    try:
        extraction = preview_from_json(blob)
    except ValidationError as exc:
        print(f"ERROR: preview JSON failed validation: {exc}", file=stderr)
        return EXIT_RUNTIME_ERROR
    except ValueError as exc:
        print(f"ERROR: preview JSON malformed: {exc}", file=stderr)
        return EXIT_RUNTIME_ERROR

    if args.dry_run:
        # Round-trip already validated the FunderRow. Surface the canonical
        # id + name so the operator can verify what would have been written.
        print(
            f"DRY-RUN ok: would upsert funder name={extraction.draft.name!r} "
            f"id={extraction.draft.id}",
            file=stdout,
        )
        return EXIT_OK

    try:
        repo = repo_factory()
    except Exception as exc:
        print(f"ERROR: could not initialise funder repository: {exc}", file=stderr)
        traceback.print_exc(file=stderr)
        return EXIT_RUNTIME_ERROR

    try:
        saved = save_extraction(extraction, repo)
    except (ValueError, RuntimeError) as exc:
        print(f"ERROR: upsert failed: {exc}", file=stderr)
        return EXIT_RUNTIME_ERROR

    try:
        audit = audit_factory()
        audit.record(
            actor="claude_code",
            action="funder.imported",
            subject_type="funder",
            subject_id=saved.id,
            details={
                "funder_name": saved.name,
                "source": "scripts/add_funder.py",
            },
        )
    except Exception as exc:
        # Audit-write failure: per CLAUDE.md "Audit-write failures FAIL the
        # operation, never silently log-and-continue."
        print(
            f"ERROR: upsert succeeded but audit emit failed: {exc}",
            file=stderr,
        )
        return EXIT_RUNTIME_ERROR

    print(
        f"saved funder name={saved.name!r} id={saved.id}",
        file=stdout,
    )
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "extract":
        return run_extract(args)
    if args.command == "merge":
        return run_merge(args)
    if args.command == "save":
        return run_save(args)
    # argparse's required=True covers this; defensive return for mypy.
    return EXIT_INVALID_ARGS


if __name__ == "__main__":
    sys.exit(main())
