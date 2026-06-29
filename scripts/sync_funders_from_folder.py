"""Sync funder guidelines from a Google Drive folder into AEGIS.

Expected Drive structure::

    Funders/                           ← GOOGLE_DRIVE_FUNDERS_FOLDER_ID
      Highland Hill Capital/
        guidelines.pdf
      Logic Advance/
        underwriting_matrix.pdf
      New Funder/                      ← auto-detected → inserted with status='active'
        guidelines.pdf

Required env (in ``/etc/aegis/aegis.env``):

* ``GOOGLE_DRIVE_CREDENTIALS_JSON`` — service-account JSON (string or path).
* ``GOOGLE_DRIVE_FUNDERS_FOLDER_ID`` — the folder ID from the Drive URL.

Behavior:

* Walks every immediate subfolder of the configured root.
* Picks the most-recent PDF in each subfolder.
* Skips when the PDF's Drive ``modifiedTime`` matches the previously-
  recorded ``_drive_modified`` in ``funders.guidelines_data`` — idempotent.
* Re-extracts when the PDF has changed since last sync.
* Inserts a new ``funders`` row when the subfolder name doesn't match
  any existing funder (case-insensitive name match).
* Operator-confirmation discipline (CLAUDE.md "Extraction & automation
  assists, never replaces judgment"): the JSONB ``guidelines_data``
  blob is updated automatically, but the live FunderRow columns are
  NEVER auto-overwritten by this script. The operator promotes
  individual fields through the funder edit UI.

Exit codes:

* ``0`` — clean run.
* ``1`` — config error (missing env, Drive API unreachable, etc.).
* ``3`` — one or more per-funder errors (Bedrock failure, malformed PDF,
  Supabase write failure). Other funders still processed.

Usage::

    python scripts/sync_funders_from_folder.py --dry-run   # default
    python scripts/sync_funders_from_folder.py --apply
"""

from __future__ import annotations

import argparse
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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Actually write to Supabase. Default is dry-run.",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify each Drive subfolder; no DB writes. Default.",
    )
    return parser.parse_args()


def _sync(*, apply_writes: bool) -> dict[str, list[str]]:
    """One-shot Drive→AEGIS sync. Returns a bucket roll-up dict."""
    from aegis.db import get_supabase
    from aegis.funders.guidelines_extract import (
        GuidelinesExtractionError,
        extract_guidelines_from_pdf,
    )
    from aegis.integrations.google_drive import (
        GoogleDriveAPIError,
        GoogleDriveConfigError,
        download_pdf,
        get_funders_folder_id,
        list_funder_folders,
    )
    from aegis.llm import BedrockClient

    try:
        folder_id = get_funders_folder_id()
    except GoogleDriveConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)

    print(f"# Reading funders from Google Drive folder: {folder_id}", file=sys.stderr)
    try:
        drive_folders = list_funder_folders(folder_id)
    except (GoogleDriveConfigError, GoogleDriveAPIError) as exc:
        print(f"DRIVE ERROR: {exc}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)

    print(
        f"# Found {len(drive_folders)} funder folders in Drive\n",
        file=sys.stderr,
    )

    sb = get_supabase()
    existing_resp = (
        sb.table("funders").select("id,name,guidelines_data,guidelines_uploaded_at").execute()
    )
    existing_rows = cast(list[dict[str, Any]], existing_resp.data or [])
    existing_by_name: dict[str, dict[str, Any]] = {
        row["name"].lower(): row for row in existing_rows
    }

    llm = BedrockClient()

    results: dict[str, list[str]] = {
        "added": [],
        "updated": [],
        "skipped_up_to_date": [],
        "skipped_no_pdf": [],
        "errors": [],
    }

    for folder in drive_folders:
        name = folder.funder_name
        if not folder.latest_pdf_id:
            print(f"  - {name}: no PDF in Drive folder", file=sys.stderr)
            results["skipped_no_pdf"].append(name)
            continue

        existing = existing_by_name.get(name.lower())
        stored_modified = None
        if existing:
            stored_modified = (existing.get("guidelines_data") or {}).get("_drive_modified")
        if existing and stored_modified == folder.latest_pdf_modified:
            print(f"  = {name}: up to date", file=sys.stderr)
            results["skipped_up_to_date"].append(name)
            continue

        action_verb = "UPDATE" if existing else "INSERT"
        if not apply_writes:
            print(
                f"  [{action_verb} dry-run] {name}: would extract {folder.latest_pdf_name}",
                file=sys.stderr,
            )
            results["updated" if existing else "added"].append(name)
            continue

        try:
            pdf_bytes = download_pdf(folder.latest_pdf_id)
            extraction = extract_guidelines_from_pdf(pdf_bytes, llm)
        except (GoogleDriveAPIError, GuidelinesExtractionError) as exc:
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
        # Drive metadata so the next sync can detect "no change since
        # last extraction" by comparing modifiedTime.
        payload["_drive_file_id"] = folder.latest_pdf_id
        payload["_drive_modified"] = folder.latest_pdf_modified
        payload["_pdf_filename"] = folder.latest_pdf_name

        write_row: dict[str, Any] = {
            "guidelines_data": payload,
            "guidelines_uploaded_at": datetime.now(UTC).isoformat(),
        }

        try:
            if existing:
                sb.table("funders").update(cast(Any, write_row)).eq("id", existing["id"]).execute()
                results["updated"].append(name)
                print(
                    f"  ↑ {name}: updated (drive_modified={folder.latest_pdf_modified})",
                    file=sys.stderr,
                )
            else:
                insert_row: dict[str, Any] = {
                    "name": name,
                    "operator_status": "active",
                    **write_row,
                }
                sb.table("funders").insert(cast(Any, insert_row)).execute()
                # Audit-log row for the auto-add — visible to the operator
                # on the next review session.
                sb.table("audit_log").insert(
                    cast(
                        Any,
                        {
                            "actor": "system:drive_sync",
                            "action": "funder.auto_added_from_drive",
                            "subject_type": "funder",
                            "details": {
                                "name": name,
                                "pdf_filename": folder.latest_pdf_name,
                                "drive_folder_id": folder.folder_id,
                            },
                        },
                    )
                ).execute()
                results["added"].append(name)
                print(f"  + {name}: added", file=sys.stderr)
        except Exception as exc:
            print(f"  ! {name}: persist failed: {exc}", file=sys.stderr)
            results["errors"].append(f"{name}: persist_{type(exc).__name__}")

    return results


def main() -> int:
    args = _parse_args()
    apply_writes = bool(args.apply)
    mode = "APPLY" if apply_writes else "DRY-RUN"
    print(f"# mode={mode}", file=sys.stderr)
    results = _sync(apply_writes=apply_writes)
    print("", file=sys.stderr)
    print(f"# RESULT mode={mode}", file=sys.stderr)
    for bucket in (
        "added",
        "updated",
        "skipped_up_to_date",
        "skipped_no_pdf",
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
