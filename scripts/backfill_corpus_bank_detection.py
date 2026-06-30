"""Re-detect bank names on existing ``corpus_documents`` rows.

Background
----------
The 2026-06-30 audit found 135 of 283 corpus_documents had
``bank_name = NULL`` (48% NULL rate). Commit ``0644182`` added 12
new bank regex patterns + an application-form skip heuristic, but
the corpus ingest is SHA-256 dedup-gated — re-running
``ingest_training_corpus.py --apply`` hits "skip (dedup)" on every
file because each hash is already in the table.

This backfill walks the existing NULL-bank rows, re-extracts the
first-page text from the source ZIP, and re-runs ``detect_bank_name``
with the latest pattern set. ONLY the ``bank_name`` column is
touched; all other columns (file_hash, original_path, page_count,
forensic flags, creator, producer) are preserved.

Safety:

  * ``WHERE bank_name IS NULL`` guard makes this idempotent — only
    NULL rows are read; only rows that gain a bank name are written.
  * Failures on a single file (missing in ZIP, corrupt PDF, pymupdf
    crash) are logged and skipped; never block the rest.
  * Dry-run support: pass ``--dry-run`` to report what WOULD be
    written without touching the DB.

Usage::

    # Dry-run:
    python scripts/backfill_corpus_bank_detection.py --dry-run

    # Apply (writes ``bank_name`` UPDATE for matched rows):
    python scripts/backfill_corpus_bank_detection.py --apply

    # Custom ZIP path:
    python scripts/backfill_corpus_bank_detection.py --zip /path/to/file.zip --apply
"""

from __future__ import annotations

import argparse
import io
import sys
import zipfile
from pathlib import Path
from typing import Any, cast

import pymupdf

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from aegis.db import get_supabase  # noqa: E402
from aegis.logger import get_logger  # noqa: E402

_log = get_logger(__name__)

_DEFAULT_ZIP = Path("/var/lib/aegis/corpus/Commera Lead Files.zip")
_HEADER_TEXT_BYTES: int = 2000


def detect_bank_name(text: str) -> str | None:
    """Delegate to the ingest script so the regex set stays the
    single source of truth (zero pattern drift between the script
    and this backfill)."""
    from scripts.ingest_training_corpus import detect_bank_name as _detect

    return _detect(text)


def _read_first_page_text(pdf_bytes: bytes) -> str:
    """Return up to ``_HEADER_TEXT_BYTES`` chars of first-page text.

    Best-effort: returns empty string on any pymupdf failure (image-
    only PDF, encrypted, corrupt). The caller treats empty as "no
    match" so the row simply stays NULL.
    """
    try:
        with pymupdf.open(stream=io.BytesIO(pdf_bytes), filetype="pdf") as doc:  # type: ignore[no-untyped-call]
            if doc.page_count == 0:
                return ""
            text = cast(str, doc.load_page(0).get_text("text") or "")
    except Exception as exc:
        _log.debug("backfill.first_page_text_failed err=%s", exc)
        return ""
    return text[:_HEADER_TEXT_BYTES]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Write UPDATEs to corpus_documents.")
    mode.add_argument("--dry-run", action="store_true", help="Default — report only.")
    parser.add_argument(
        "--zip",
        type=Path,
        default=_DEFAULT_ZIP,
        help=f"Path to the source corpus ZIP (default: {_DEFAULT_ZIP})",
    )
    args = parser.parse_args()
    apply = bool(args.apply)
    zip_path: Path = args.zip

    if not zip_path.exists():
        print(f"CONFIG ERROR: corpus ZIP not found at {zip_path}", file=sys.stderr)
        return 1

    sb = get_supabase()
    null_rows = (
        sb.table("corpus_documents").select("id,original_path").is_("bank_name", "null").execute()
    )
    raw_data = null_rows.data or []
    rows: list[dict[str, Any]] = [r for r in raw_data if isinstance(r, dict)]
    total = len(rows)
    print(f"# mode={'APPLY' if apply else 'DRY-RUN'}")
    print(f"# Source ZIP: {zip_path}")
    print(f"NULL-bank rows to process: {total}")

    if total == 0:
        print("Nothing to do.")
        return 0

    with zipfile.ZipFile(zip_path) as zf:
        # Index ZIP entries by basename (lowercase) for filename match.
        # original_path may be the full ZIP path; try full first, then basename.
        name_by_full: dict[str, str] = {n: n for n in zf.namelist()}
        name_by_basename: dict[str, str] = {Path(n).name.lower(): n for n in zf.namelist()}

        fixed = 0
        skipped_no_match = 0
        skipped_no_text = 0
        skipped_no_detection = 0
        errors = 0

        for row in rows:
            original_path = str(row.get("original_path") or "")
            if not original_path:
                skipped_no_match += 1
                continue

            zip_entry = name_by_full.get(original_path) or name_by_basename.get(
                Path(original_path).name.lower()
            )
            if zip_entry is None:
                skipped_no_match += 1
                continue

            try:
                pdf_bytes = zf.read(zip_entry)
            except Exception as exc:
                _log.warning("backfill.zip_read_failed entry=%s err=%s", zip_entry, exc)
                errors += 1
                continue

            text = _read_first_page_text(pdf_bytes)
            if not text:
                skipped_no_text += 1
                continue

            bank = detect_bank_name(text)
            if not bank:
                skipped_no_detection += 1
                continue

            display = Path(original_path).name[:80]
            print(f"  + {display} -> {bank}")
            fixed += 1

            if apply:
                try:
                    sb.table("corpus_documents").update({"bank_name": bank}).eq(
                        "id", row["id"]
                    ).execute()
                except Exception as exc:
                    _log.warning("backfill.update_failed id=%s err=%s", row.get("id"), exc)
                    errors += 1

        print()
        print(
            f"# RESULT mode={'APPLY' if apply else 'DRY-RUN'}\n"
            f"  fixed:                 {fixed}\n"
            f"  skipped_no_zip_match:  {skipped_no_match}\n"
            f"  skipped_no_text:       {skipped_no_text}\n"
            f"  skipped_no_detection:  {skipped_no_detection}\n"
            f"  errors:                {errors}\n"
            f"  total:                 {total}"
        )
    return 0 if errors == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
