"""Daily Hetzner Cloud snapshot of the AEGIS prod server.

Reads ``HETZNER_API_TOKEN`` and ``HETZNER_SERVER_ID`` from the env.
When either is absent the script exits 0 with a graceful-skip log
line — the arq cron fires globally on the box, but if the operator
hasn't provisioned a Hetzner API token yet the cron is a no-op.

Behavior when both env vars are set:

  1. POST /servers/{id}/actions/create_image with type=snapshot and
     a descriptive label + labels={"source": "aegis-auto"}.
  2. GET the list of snapshot images matching the same label
     selector, sort by created_at desc, delete anything past the
     KEEP threshold.

The script does NOT wait for the snapshot to complete — Hetzner queues
snapshots asynchronously and the create_image action typically takes
30-120 seconds. We fire-and-forget and rely on the next run to prune.
Errors are logged and surfaced via ``sys.exit(1)`` so the arq cron
wrapper writes an ``hetzner_snapshot_failed`` audit row.

Env vars:
    HETZNER_API_TOKEN   — Hetzner Cloud API bearer token.
    HETZNER_SERVER_ID   — numeric server id (from Hetzner console).

Never printed to stdout; captured to variables + used in the
Authorization header only.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

from aegis.logger import get_logger

_log = get_logger(__name__)

_API = "https://api.hetzner.cloud/v1"
_KEEP = 7  # keep 7 most recent auto-snapshots


def _request(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    token = os.environ.get("HETZNER_API_TOKEN", "")
    req = urllib.request.Request(  # noqa: S310 — API is https + explicit
        f"{_API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "aegis-hetzner-snapshot/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
            raw = r.read()
        if not raw:
            return {}
        return dict(json.loads(raw))
    except urllib.error.HTTPError as exc:
        _log.error(
            "hetzner_api.http_error method=%s path=%s status=%s",
            method,
            path,
            exc.code,
        )
        raise


def run() -> int:
    """Entry point. Returns process exit code (0 = success / skip; 1 = failure)."""
    token = os.environ.get("HETZNER_API_TOKEN", "").strip()
    server_id = os.environ.get("HETZNER_SERVER_ID", "").strip()

    if not token:
        _log.info("hetzner_snapshot.skipped reason=no_token")
        return 0
    if not server_id:
        _log.info("hetzner_snapshot.skipped reason=no_server_id")
        return 0

    try:
        label = f"aegis-auto-{datetime.now(UTC).strftime('%Y%m%d-%H%M')}"
        create_result = _request(
            "POST",
            f"/servers/{server_id}/actions/create_image",
            body={
                "description": label,
                "type": "snapshot",
                "labels": {"source": "aegis-auto"},
            },
        )
        action = create_result.get("action") or {}
        _log.info(
            "hetzner_snapshot.queued label=%s action_id=%s",
            label,
            action.get("id"),
        )

        # Prune older snapshots (keep the last _KEEP with our label).
        list_result = _request(
            "GET",
            "/images?type=snapshot&sort=created%3Adesc&label_selector=source%3Daegis-auto",
        )
        images = list_result.get("images") or []
        for img in list(images)[_KEEP:]:
            img_id = img.get("id")
            if img_id is None:
                continue
            try:
                _request("DELETE", f"/images/{img_id}")
                _log.info("hetzner_snapshot.deleted id=%s", img_id)
            except Exception as exc:
                _log.warning("hetzner_snapshot.delete_failed id=%s exc=%s", img_id, exc)
        return 0
    except Exception as exc:
        _log.error("hetzner_snapshot.failed exc=%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(run())
