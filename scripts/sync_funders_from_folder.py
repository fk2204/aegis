"""Sync funder guidelines from a local folder into AEGIS.

Expected folder layout (default = the operator's Windows OneDrive path,
configured via ``settings.funders_folder_path``)::

    Funders/                          ← --folder / FUNDERS_FOLDER_PATH
      Highland Hill Capital/
        guidelines.pdf
      Logic Advance/
        underwriting_matrix.docx
      New Funder/                     ← auto-detected → INSERT
        criteria.png

Per immediate subfolder the script picks the newest supported file
(``.pdf .docx .doc .xlsx .xls .jpg .jpeg .png .txt``) by mtime, hashes
it with SHA-256, and runs Bedrock extraction unless the hash matches
``funders.guidelines_data._file_hash`` from a prior run (idempotent —
re-running the script with an unchanged folder is a no-op modulo the
audit row).

Bedrock routing per file type:

* PDF + images (PNG / JPG / JPEG) — the canonical AEGIS extractor
  ``aegis.funders.guidelines_extract.extract_guidelines_from_pdf``
  (and its sibling vision path for images). That extractor owns the
  sanitiser + per-field 0.5 confidence floor; we route through it so
  the script can't drift from the production policy.
* DOCX (mammoth → plaintext), XLSX (openpyxl → CSV-ish dump), TXT
  (utf-8) — a thin direct call to
  ``BedrockClient.classify_batch_json`` using the same prompt the
  canonical extractor uses. The same sanitiser is applied AEGIS-side
  before persistence so the confidence floor still holds.

Operator-confirmation discipline — CLAUDE.md "Extraction & automation
assists, never replaces judgment" — normally bans auto-writes to the
live FunderRow columns. This script is the explicit operator-approved
exception: the folder lives on the operator's own machine, contents are
their own funder criteria sheets (not Bedrock guessing from a stranger's
PDF), so per-funder INSERT seeds the live columns
(``min_revenue``, ``min_fico``, ``min_tib_months``, ``max_positions``,
``allows_stacking``, ``deal_types_accepted``, ``excluded_states``,
``excluded_industries``) directly. The structured JSONB
``guidelines_data`` blob remains the source of truth for the operator
to review; the live columns are a convenience pre-fill the operator can
still override via the funder edit UI.

Exit codes:

* ``0`` — clean run.
* ``1`` — config error (folder missing, supabase init failed).
* ``3`` — one or more per-funder errors (Bedrock failure, malformed
  file, Supabase write failure). Other funders still processed.

Usage::

    python scripts/sync_funders_from_folder.py                   # dry-run
    python scripts/sync_funders_from_folder.py --apply           # writes
    python scripts/sync_funders_from_folder.py --folder PATH     # override
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import os
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

# Make the script runnable directly from the repo root without a
# package install (matches sibling scripts).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))


EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_PER_FUNDER_ERRORS = 3


_SUPPORTED_EXTS: frozenset[str] = frozenset(
    {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".jpg", ".jpeg", ".png", ".txt"}
)
_PDF_EXTS: frozenset[str] = frozenset({".pdf"})
_IMAGE_EXTS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png"})
_DOCX_EXTS: frozenset[str] = frozenset({".docx", ".doc"})
_XLSX_EXTS: frozenset[str] = frozenset({".xlsx", ".xls"})
_TXT_EXTS: frozenset[str] = frozenset({".txt"})


def _load_dotenv() -> None:
    """Load ``.env`` from the repo root without overwriting existing env.

    Mirrors ``scripts/audit_funders_table.py``. The Settings class also
    auto-loads ``.env`` via pydantic-settings but does so RELATIVE to the
    process CWD, which fails when the script is invoked from a deeper
    directory. Explicit load keeps behavior stable.
    """
    for path in (_REPO_ROOT / ".env", _REPO_ROOT / ".env.local"):
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--folder",
        type=str,
        default=None,
        help=(
            "Override the funders folder path. Defaults to "
            "settings.funders_folder_path (see src/aegis/config.py)."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Actually write to Supabase. Default is dry-run.",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify each subfolder; no DB writes. Default.",
    )
    return parser.parse_args()


def _sha256_file(path: Path) -> str:
    """SHA-256 hex digest of a file's bytes, read in 64 KiB chunks."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _pick_latest_supported_file(folder: Path) -> Path | None:
    """Return the newest supported file in ``folder`` (immediate children
    only — sub-subfolders are ignored), or ``None`` when the folder has
    no supported file. Newness is defined by mtime, matching how the
    operator typically saves a fresh criteria sheet.
    """
    candidates = [
        p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in _SUPPORTED_EXTS
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _xlsx_to_text(path: Path) -> str:
    """Dump every sheet of an xlsx file as CSV-ish plaintext.

    openpyxl is already an AEGIS dependency (A/R aging parser). Read-only
    mode keeps memory bounded on large workbooks. Empty / formula-only
    cells become empty strings, matching the existing A/R extractor.
    """
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    buf = io.StringIO()
    for sheet_name in wb.sheetnames:
        buf.write(f"# SHEET: {sheet_name}\n")
        ws = wb[sheet_name]
        writer = csv.writer(buf)
        for row in ws.iter_rows(values_only=True):
            writer.writerow(["" if cell is None else str(cell) for cell in row])
        buf.write("\n")
    return buf.getvalue()


def _docx_to_text(path: Path) -> str:
    """Convert a docx file to plaintext via mammoth.

    Mammoth strips Word styling (which is what we want — Bedrock works
    better on clean text). ``.doc`` (binary legacy format) is NOT
    supported by mammoth; if the operator drops a ``.doc`` in we log it
    and skip rather than half-process.
    """
    import mammoth

    with path.open("rb") as fh:
        result = mammoth.extract_raw_text(fh)
    raw_value: object = result.value
    return raw_value if isinstance(raw_value, str) else str(raw_value)


def _extract_from_text(
    text: str,
    llm: Any,  # noqa: ANN401 — BedrockClient duck-typed for test stubs
) -> Any:  # noqa: ANN401 — returns aegis.funders.guidelines_extract.FunderGuidelinesExtraction
    """Extract guidelines from plain text via the canonical sanitiser.

    Reuses ``GUIDELINES_EXTRACTION_PROMPT`` + ``_sanitise_extraction`` +
    ``FunderGuidelinesExtraction`` from
    ``aegis.funders.guidelines_extract`` so the confidence floor and the
    coercion rules don't drift. The only thing this function does that
    the canonical PDF entry point doesn't is route text through
    ``classify_batch_json`` instead of the document-block call (Bedrock
    has no native "plain text" envelope for the document content type).
    """
    from pydantic import ValidationError

    from aegis.funders.guidelines_extract import (
        GUIDELINES_EXTRACTION_PROMPT,
        FunderGuidelinesExtraction,
        GuidelinesExtractionError,
        _sanitise_extraction,
    )

    full_prompt = f"{GUIDELINES_EXTRACTION_PROMPT}\n\nDocument text:\n{text}"
    try:
        raw = llm.classify_batch_json(full_prompt)
    except ValueError as exc:
        raise GuidelinesExtractionError(f"LLM returned malformed JSON: {exc}") from exc

    sanitised = _sanitise_extraction(raw)
    try:
        return FunderGuidelinesExtraction.model_validate(sanitised)
    except ValidationError as exc:
        raise GuidelinesExtractionError(f"FunderGuidelinesExtraction validation: {exc}") from exc


def _extract_from_image(
    image_bytes: bytes,
    media_subtype: str,
    llm: Any,  # noqa: ANN401 — BedrockClient duck-typed for test stubs
) -> Any:  # noqa: ANN401 — returns FunderGuidelinesExtraction
    """Extract guidelines from a single image via the vision path.

    Wraps ``BedrockClient.extract_raw_json_from_images`` and reuses the
    canonical sanitiser. ``media_subtype`` is informational — Bedrock
    accepts ``image/png``, ``image/jpeg``; PNG is the safe default for
    the streaming envelope (matches parser/vision usage). We re-encode
    JPEG as JPEG to avoid PIL roundtrips.
    """
    from pydantic import ValidationError

    from aegis.funders.guidelines_extract import (
        GUIDELINES_EXTRACTION_PROMPT,
        FunderGuidelinesExtraction,
        GuidelinesExtractionError,
        _sanitise_extraction,
    )

    # Vision envelope guesses media type from bytes; ``media_subtype`` is
    # a placeholder for future media-type-aware routing.
    del media_subtype

    try:
        raw, truncated = llm.extract_raw_json_from_images(
            [image_bytes], GUIDELINES_EXTRACTION_PROMPT
        )
    except ValueError as exc:
        raise GuidelinesExtractionError(f"LLM returned malformed JSON: {exc}") from exc

    if truncated:
        raise GuidelinesExtractionError(
            "LLM extraction was truncated at max_tokens — funder guideline "
            "image exceeds the model's output budget."
        )

    sanitised = _sanitise_extraction(raw)
    try:
        return FunderGuidelinesExtraction.model_validate(sanitised)
    except ValidationError as exc:
        raise GuidelinesExtractionError(f"FunderGuidelinesExtraction validation: {exc}") from exc


def _extract_for_file(
    file_path: Path,
    llm: Any,  # noqa: ANN401 — BedrockClient duck-typed
) -> Any:  # noqa: ANN401 — FunderGuidelinesExtraction
    """Dispatch to the right Bedrock entry based on the file extension.

    Raises ``GuidelinesExtractionError`` on extraction failure (caller
    catches and routes to the per-funder error bucket).
    """
    from aegis.funders.guidelines_extract import (
        GuidelinesExtractionError,
        extract_guidelines_from_pdf,
    )

    ext = file_path.suffix.lower()
    file_bytes = file_path.read_bytes()

    if ext in _PDF_EXTS:
        return extract_guidelines_from_pdf(file_bytes, llm)

    if ext in _IMAGE_EXTS:
        media_subtype = "jpeg" if ext in {".jpg", ".jpeg"} else "png"
        return _extract_from_image(file_bytes, media_subtype, llm)

    if ext in _DOCX_EXTS:
        if ext == ".doc":
            raise GuidelinesExtractionError(
                "legacy .doc binary format not supported — convert to .docx or .pdf"
            )
        text = _docx_to_text(file_path)
        if not text.strip():
            raise GuidelinesExtractionError("docx contained no extractable text")
        return _extract_from_text(text, llm)

    if ext in _XLSX_EXTS:
        if ext == ".xls":
            raise GuidelinesExtractionError(
                "legacy .xls binary format not supported — convert to .xlsx or .pdf"
            )
        text = _xlsx_to_text(file_path)
        if not text.strip():
            raise GuidelinesExtractionError("xlsx contained no extractable text")
        return _extract_from_text(text, llm)

    if ext in _TXT_EXTS:
        try:
            text = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise GuidelinesExtractionError(f"txt file not utf-8: {exc}") from exc
        if not text.strip():
            raise GuidelinesExtractionError("txt file was empty")
        return _extract_from_text(text, llm)

    raise GuidelinesExtractionError(f"unsupported file extension: {ext}")


def _stacking_to_bool(stacking_policy: str | None) -> bool | None:
    """Map the FunderGuidelinesExtraction stacking_policy Literal to the
    live FunderRow.accepts_stacking bool. ``case_by_case`` is left as
    ``None`` so the operator's review UI shows the field as "needs
    operator input" rather than defaulting to allow or deny.
    """
    if stacking_policy == "allowed":
        return True
    if stacking_policy == "not_allowed":
        return False
    return None


def _build_live_column_writes(extraction: Any) -> dict[str, Any]:  # noqa: ANN401 — FunderGuidelinesExtraction
    """Map the staging extraction to the live FunderRow columns.

    Per the operator-approved exception in the module docstring, the
    script writes these columns directly on INSERT / UPDATE. Fields the
    extractor dropped (confidence < 0.5) stay missing — the caller
    folds this dict into the row payload only for keys that ARE present.
    """
    out: dict[str, Any] = {}
    if extraction.min_revenue is not None:
        # Live column ``min_monthly_revenue`` is the deployed name; the
        # extraction model carries it under ``min_revenue`` because that
        # mirrors the operator-facing extraction prompt language.
        out["min_monthly_revenue"] = extraction.min_revenue
    if extraction.min_fico is not None:
        out["min_credit_score"] = extraction.min_fico
    if extraction.min_tib_months is not None:
        out["min_months_in_business"] = extraction.min_tib_months
    if extraction.max_positions is not None:
        out["max_positions"] = extraction.max_positions

    accepts_stacking = _stacking_to_bool(extraction.stacking_policy)
    if accepts_stacking is not None:
        out["accepts_stacking"] = accepts_stacking

    # excluded_* arrive as list[str]; the live FunderRow columns are
    # ARRAY<text> in Postgres — supabase-py round-trips a Python list
    # cleanly. Empty lists are NOT written (the operator's prior values
    # should not be wiped by a sparse extraction).
    if extraction.excluded_states:
        out["excluded_states"] = list(extraction.excluded_states)
    if extraction.excluded_industries:
        out["excluded_industries"] = list(extraction.excluded_industries)

    return out


def _normalise_subfolder_name(name: str) -> str:
    """Lower-case + strip — matches how the in-prod ``funders.name``
    lookup is compared (case-insensitive in the matcher; trailing
    whitespace would create false-misses against existing rows).
    """
    return name.strip().lower()


def _sync(
    *,
    folder_path: Path,
    apply_writes: bool,
) -> dict[str, list[str]]:
    """One-shot folder→AEGIS sync. Returns a bucket roll-up dict."""
    from aegis.db import get_supabase
    from aegis.funders.guidelines_extract import GuidelinesExtractionError
    from aegis.llm import BedrockClient

    if not folder_path.exists() or not folder_path.is_dir():
        print(
            f"CONFIG ERROR: funders folder not found or not a directory: {folder_path}",
            file=sys.stderr,
        )
        sys.exit(EXIT_CONFIG)

    print(f"# Reading funders from local folder: {folder_path}", file=sys.stderr)
    subfolders = sorted(
        (p for p in folder_path.iterdir() if p.is_dir()),
        key=lambda p: p.name.lower(),
    )
    print(f"# Found {len(subfolders)} funder subfolders\n", file=sys.stderr)

    try:
        sb = get_supabase()
    except Exception as exc:
        print(f"CONFIG ERROR: supabase init failed: {exc}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)

    existing_resp = (
        sb.table("funders").select("id,name,guidelines_data,guidelines_uploaded_at").execute()
    )
    existing_rows = cast(list[dict[str, Any]], existing_resp.data or [])
    existing_by_name: dict[str, dict[str, Any]] = {
        _normalise_subfolder_name(row["name"]): row for row in existing_rows
    }

    llm = BedrockClient()

    results: dict[str, list[str]] = {
        "added": [],
        "updated": [],
        "skipped_up_to_date": [],
        "skipped_no_file": [],
        "errors": [],
    }

    for sub in subfolders:
        name = sub.name.strip()
        latest = _pick_latest_supported_file(sub)
        if latest is None:
            print(f"  - {name}: no supported file in folder", file=sys.stderr)
            results["skipped_no_file"].append(name)
            continue

        file_hash = _sha256_file(latest)
        existing = existing_by_name.get(_normalise_subfolder_name(name))
        stored_hash = None
        if existing:
            stored_hash = (existing.get("guidelines_data") or {}).get("_file_hash")
        if existing and stored_hash == file_hash:
            print(f"  = {name}: up to date (hash unchanged)", file=sys.stderr)
            results["skipped_up_to_date"].append(name)
            continue

        action_verb = "UPDATE" if existing else "INSERT"
        if not apply_writes:
            print(
                f"  [{action_verb} dry-run] {name}: would extract {latest.name}",
                file=sys.stderr,
            )
            results["updated" if existing else "added"].append(name)
            continue

        try:
            extraction = _extract_for_file(latest, llm)
        except GuidelinesExtractionError as exc:
            print(f"  ! {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            results["errors"].append(f"{name}: {type(exc).__name__}")
            continue
        except Exception as exc:
            print(
                f"  ! {name}: unexpected error {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            traceback.print_exc(file=sys.stderr)
            results["errors"].append(f"{name}: {type(exc).__name__}")
            continue

        payload = extraction.model_dump(mode="json")
        payload["_file_hash"] = file_hash
        payload["_file_name"] = latest.name
        payload["_folder_name"] = name

        write_row: dict[str, Any] = {
            "guidelines_data": payload,
            "guidelines_uploaded_at": datetime.now(UTC).isoformat(),
        }
        write_row.update(_build_live_column_writes(extraction))

        try:
            if existing:
                sb.table("funders").update(cast(Any, write_row)).eq("id", existing["id"]).execute()
                results["updated"].append(name)
                print(
                    f"  ↑ {name}: updated (hash={file_hash[:12]}…, file={latest.name})",
                    file=sys.stderr,
                )
            else:
                insert_row: dict[str, Any] = {
                    "name": name,
                    "operator_status": "active",
                    **write_row,
                }
                sb.table("funders").insert(cast(Any, insert_row)).execute()
                # Audit-log row for the auto-add — operator-visible
                # surface so a surprise insert is traceable to this run.
                sb.table("audit_log").insert(
                    cast(
                        Any,
                        {
                            "actor": "system:folder_sync",
                            "action": "funder.auto_added_from_folder",
                            "subject_type": "funder",
                            "details": {
                                "name": name,
                                "file": latest.name,
                                "folder": str(sub),
                            },
                        },
                    )
                ).execute()
                results["added"].append(name)
                print(f"  + {name}: added (file={latest.name})", file=sys.stderr)
        except Exception as exc:
            print(f"  ! {name}: persist failed: {exc}", file=sys.stderr)
            results["errors"].append(f"{name}: persist_{type(exc).__name__}")

    return results


def main() -> int:
    _load_dotenv()
    args = _parse_args()

    # Late import — settings construction triggers the data-residency
    # boot guard, which we want to honor before walking the filesystem.
    from aegis.config import get_settings

    settings = get_settings()
    folder_str = args.folder or settings.funders_folder_path
    folder_path = Path(folder_str)

    apply_writes = bool(args.apply)
    mode = "APPLY" if apply_writes else "DRY-RUN"
    print(f"# mode={mode}", file=sys.stderr)

    results = _sync(folder_path=folder_path, apply_writes=apply_writes)
    print("", file=sys.stderr)
    print(f"# RESULT mode={mode}", file=sys.stderr)
    for bucket in (
        "added",
        "updated",
        "skipped_up_to_date",
        "skipped_no_file",
        "errors",
    ):
        print(f"  {bucket}: {len(results[bucket])}", file=sys.stderr)
    if results["errors"]:
        for err in results["errors"]:
            print(f"    - {err}", file=sys.stderr)
        return EXIT_PER_FUNDER_ERRORS
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
