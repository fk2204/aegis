"""Google Drive integration for AEGIS funder-guidelines sync.

Reads the operator-curated ``Funders/`` Drive folder where each
subfolder is one funder (subfolder name = funder display name in
AEGIS) and contains that funder's most recent guidelines PDF.

Required env vars (set in ``/etc/aegis/aegis.env``):

* ``GOOGLE_DRIVE_CREDENTIALS_JSON`` — service-account JSON. EITHER the
  raw JSON as a single-line string (suitable for env files), OR a
  path to a JSON file on disk.
* ``GOOGLE_DRIVE_FUNDERS_FOLDER_ID`` — the Drive folder ID that
  contains the per-funder subfolders (extracted from the Drive URL).

The integration is read-only — scope ``drive.readonly``. AEGIS never
writes anything back to Drive.

Pure helpers — no Supabase, no Bedrock. ``scripts/sync_funders_from_folder.py``
is the orchestration script that wires this together with the
``aegis.funders.guidelines_extract`` Bedrock extractor and the
``funders`` table upserts.
"""

from __future__ import annotations

import io
import json
import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DriveFunderFolder:
    """One funder subfolder discovered in the Drive funders root.

    ``latest_pdf_id`` / ``latest_pdf_name`` / ``latest_pdf_modified``
    are ``None`` when the subfolder exists in Drive but has no PDF
    inside (operator created the folder but hasn't uploaded the
    guidelines doc yet). The sync script renders this as a
    skip-with-warning, not an error.
    """

    funder_name: str
    folder_id: str
    latest_pdf_id: str | None
    latest_pdf_name: str | None
    latest_pdf_modified: str | None


class GoogleDriveConfigError(RuntimeError):
    """Operator config issue — missing credentials or folder ID."""


class GoogleDriveAPIError(RuntimeError):
    """Wrapper for ``HttpError`` so callers don't depend on the
    Google API client class hierarchy."""


def _load_service_account_info() -> dict[str, Any]:
    """Return the service-account JSON dict from env.

    Accepts EITHER the raw JSON string OR a path to a JSON file. The
    file path is the operator-friendly form for local dev; the raw
    JSON form is what fits in ``/etc/aegis/aegis.env`` on the box.
    """
    raw = os.environ.get("GOOGLE_DRIVE_CREDENTIALS_JSON", "").strip()
    if not raw:
        raise GoogleDriveConfigError(
            "GOOGLE_DRIVE_CREDENTIALS_JSON not set. Add service-account "
            "JSON to /etc/aegis/aegis.env (raw JSON or file path)."
        )
    if raw.startswith("{"):
        try:
            return json.loads(raw)  # type: ignore[no-any-return]
        except json.JSONDecodeError as exc:
            raise GoogleDriveConfigError(
                f"GOOGLE_DRIVE_CREDENTIALS_JSON is not valid JSON: {exc}"
            ) from exc
    # Treat as a path
    try:
        with open(raw, encoding="utf-8") as fh:
            return json.load(fh)  # type: ignore[no-any-return]
    except OSError as exc:
        raise GoogleDriveConfigError(
            f"GOOGLE_DRIVE_CREDENTIALS_JSON points at {raw!r} but the file cannot be opened: {exc}"
        ) from exc


def _get_drive_service() -> Any:  # noqa: ANN401 — googleapiclient is untyped
    """Build the Google Drive v3 service object.

    Imports the Google API client libraries lazily so missing deps
    surface as a clear ``GoogleDriveConfigError`` rather than an
    ImportError at module load.
    """
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build  # type: ignore[import-untyped]
    except ImportError as exc:
        raise GoogleDriveConfigError(
            "Google API client not installed. Run: uv add google-api-python-client google-auth"
        ) from exc

    info = _load_service_account_info()
    creds = service_account.Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
        info,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def get_funders_folder_id() -> str:
    folder_id = os.environ.get("GOOGLE_DRIVE_FUNDERS_FOLDER_ID", "").strip()
    if not folder_id:
        raise GoogleDriveConfigError(
            "GOOGLE_DRIVE_FUNDERS_FOLDER_ID not set. Take the folder ID "
            "from the Drive URL (the path component after /folders/)."
        )
    return folder_id


def list_funder_folders(folder_id: str) -> list[DriveFunderFolder]:
    """List every funder subfolder + its most-recent PDF.

    Returns one ``DriveFunderFolder`` per immediate subfolder of
    ``folder_id``. Subfolders with no PDF are still returned —
    ``latest_pdf_id`` will be ``None`` so the sync script can audit
    the empty-folder case.
    """
    service = _get_drive_service()
    try:
        folders_resp = (
            service.files()
            .list(
                q=(
                    f"'{folder_id}' in parents and "
                    "mimeType='application/vnd.google-apps.folder' and "
                    "trashed=false"
                ),
                fields="files(id,name)",
                orderBy="name",
                pageSize=200,
            )
            .execute()
        )
    except Exception as exc:
        raise GoogleDriveAPIError(f"Drive folder list failed: {exc}") from exc

    out: list[DriveFunderFolder] = []
    for folder in folders_resp.get("files", []):
        try:
            pdfs_resp = (
                service.files()
                .list(
                    q=(
                        f"'{folder['id']}' in parents and "
                        "mimeType='application/pdf' and trashed=false"
                    ),
                    fields="files(id,name,modifiedTime)",
                    orderBy="modifiedTime desc",
                    pageSize=1,
                )
                .execute()
            )
        except Exception as exc:
            raise GoogleDriveAPIError(
                f"Drive PDF list failed for {folder.get('name')}: {exc}"
            ) from exc
        pdfs = pdfs_resp.get("files", [])
        latest = pdfs[0] if pdfs else None
        out.append(
            DriveFunderFolder(
                funder_name=folder["name"],
                folder_id=folder["id"],
                latest_pdf_id=latest["id"] if latest else None,
                latest_pdf_name=latest["name"] if latest else None,
                latest_pdf_modified=latest["modifiedTime"] if latest else None,
            )
        )
    return out


def download_pdf(file_id: str) -> bytes:
    """Download one PDF from Drive by file id. Returns raw bytes."""
    try:
        from googleapiclient.http import MediaIoBaseDownload  # type: ignore[import-untyped]
    except ImportError as exc:
        raise GoogleDriveConfigError(
            "googleapiclient not installed. Run: uv add google-api-python-client google-auth"
        ) from exc

    service = _get_drive_service()
    request = service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        try:
            _status, done = downloader.next_chunk()
        except Exception as exc:
            raise GoogleDriveAPIError(f"Drive PDF download failed for {file_id}: {exc}") from exc
    return buffer.getvalue()


__all__ = [
    "DriveFunderFolder",
    "GoogleDriveAPIError",
    "GoogleDriveConfigError",
    "download_pdf",
    "get_funders_folder_id",
    "list_funder_folders",
]
