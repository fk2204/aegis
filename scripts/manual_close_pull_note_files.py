"""One-shot: pull every PDF attached to a Close Note/Email activity and
persist each through :func:`aegis.api.routes.upload.persist_pdf_upload`.

Why this exists
---------------
AEGIS's ``CloseClient.list_lead_attachments`` calls ``/api/v1/files/``,
which Close's API does not expose. Files in Close live ON activities
(Note, Email), not as standalone resources. The activity payload
exposes them as ``attachments[]`` with ``url`` pointing at
``app.close.com/go/file/persisted/...``. That URL refuses API-key auth
(returns 400 "use api.close.com"). Swapping the host to
``api.close.com`` works: 302 → S3 signed URL → PDF bytes.

This script does that swap + persist for one activity at a time.
Idempotent through ``persist_pdf_upload``'s SHA256 dedup.

Usage on prod box:

    set -a; source /etc/aegis/aegis.env; set +a
    uv run python scripts/manual_close_pull_note_files.py \\
        <close_lead_id> <activity_id> <merchant_id> <actor_email>
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any
from uuid import UUID

import httpx

from aegis.api.deps import get_audit, get_repository
from aegis.api.routes.upload import persist_pdf_upload
from aegis.config import get_settings
from aegis.workers import build_redis_settings


def _rewrite_to_api_host(url: str) -> str:
    """Close gives us app.close.com URLs that refuse Basic auth.
    Rewriting the host to api.close.com makes the same path valid."""
    return url.replace("https://app.close.com/", "https://api.close.com/", 1)


async def main(
    close_lead_id: str,
    activity_id: str,
    merchant_id_str: str,
    actor_email: str,
) -> int:
    merchant_id = UUID(merchant_id_str)
    settings = get_settings()
    api_key = settings.close_api_key.get_secret_value() if settings.close_api_key else ""
    if not api_key:
        print("CLOSE_API_KEY not configured", file=sys.stderr)
        return 2

    repository = get_repository()
    audit = get_audit()

    # Fetch the activity by id. Activity type is in the id prefix:
    # acti_... — but the endpoint /api/v1/activity/note/<id>/ works for
    # notes; for emails we'd use /api/v1/activity/email/<id>/. Try both.
    print(f"[1/3] Fetching activity {activity_id} ...")
    with httpx.Client(auth=(api_key, ""), timeout=20.0) as client:
        for kind in ("note", "email"):
            r = client.get(f"https://api.close.com/api/v1/activity/{kind}/{activity_id}/")
            if r.status_code == 200:
                activity = r.json()
                print(f"      fetched as activity/{kind}/")
                break
        else:
            print(f"      could not fetch activity {activity_id}", file=sys.stderr)
            return 1

        attachments: list[dict[str, Any]] = activity.get("attachments") or []
        print(f"      {len(attachments)} attachment(s) on the activity")

        # Filter to PDFs only
        pdf_atts = [a for a in attachments if a.get("content_type") == "application/pdf"]
        print(f"      {len(pdf_atts)} PDF(s)")

        # Enqueue pool — created once, reused
        from arq import create_pool

        pool = await create_pool(build_redis_settings())

        async def _enqueue(document_id: UUID, file_hash: str) -> None:
            await pool.enqueue_job("parse_document", str(document_id), file_hash)

        try:
            print(f"[2/3] Downloading + persisting {len(pdf_atts)} PDF(s) ...")
            for i, att in enumerate(pdf_atts, 1):
                filename = att.get("filename") or f"close_attachment_{i}.pdf"
                src_url = att.get("url")
                if not src_url:
                    print(f"      [{i}] SKIP — no url for {filename!r}")
                    continue
                dl_url = _rewrite_to_api_host(src_url)
                print(f"      [{i}/{len(pdf_atts)}] GET {filename} ...")
                dl = client.get(dl_url, follow_redirects=True)
                if dl.status_code != 200:
                    print(
                        f"          FAIL http={dl.status_code} "
                        f"body={dl.text[:200]!r}"
                    )
                    continue
                body = dl.content
                if not body.startswith(b"%PDF-"):
                    print(
                        f"          FAIL not-a-pdf head={body[:8]!r}"
                    )
                    continue
                print(f"          downloaded {len(body)} bytes")

                resp = await persist_pdf_upload(
                    enqueue_parse=_enqueue,
                    body=body,
                    original_filename=filename,
                    repository=repository,
                    audit=audit,
                    actor="manual_close_pull",
                    actor_email=actor_email,
                    merchant_id=merchant_id,
                    close_lead_id=close_lead_id,
                )
                tag = "DUP" if resp.duplicate_of_existing else "NEW"
                print(
                    f"          {tag} document_id={resp.document_id} "
                    f"parse_status={resp.parse_status}"
                )
        finally:
            await pool.close(close_connection_pool=True)

    print("[3/3] Done.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 5:
        print(
            "usage: manual_close_pull_note_files.py "
            "<close_lead_id> <activity_id> <merchant_id> <actor_email>",
            file=sys.stderr,
        )
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])))
