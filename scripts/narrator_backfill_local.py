#!/usr/bin/env python3
"""Enqueue narrator-summary jobs for proceed docs missing summaries.

Run from ``C:\\Users\\fkozi\\aegis`` on the operator workstation::

    uv run python scripts/narrator_backfill_local.py

Requires ``.env`` at repo root with ``SUPABASE_URL``, ``SUPABASE_KEY``,
``AEGIS_DATA_RESIDENCY_CONFIRMED=true``, and a ``REDIS_URL`` that
resolves to the production Redis instance (e.g. via an SSH tunnel from
this workstation to the prod box on port 6379).

The script walks every ``documents`` row with ``parse_status='proceed'``,
finds the matching ``analyses`` row, and enqueues
``generate_narrator_summary`` when the existing ``narrator_summary`` is
NULL. The arq worker on the prod box drains the queue and writes the
summary back to ``analyses.narrator_summary``.

Idempotent. Re-running only enqueues jobs for analyses still missing a
summary at query time.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, cast

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

_env_file = _REPO_ROOT / ".env"
if _env_file.exists():
    _raw = _env_file.read_bytes()
    if _raw.startswith(b"\xff\xfe"):
        _text = _raw.decode("utf-16-le")
    elif _raw.startswith(b"\xfe\xff"):
        _text = _raw.decode("utf-16-be")
    elif _raw.startswith(b"\xef\xbb\xbf"):
        _text = _raw[3:].decode("utf-8")
    else:
        _text = _raw.decode("utf-8", errors="replace")
    for _line in _text.splitlines():
        _line = _line.strip().lstrip("﻿")
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from aegis.config import get_settings  # noqa: E402
from aegis.db import get_supabase  # noqa: E402


def _rows(result: object) -> list[dict[str, Any]]:
    data = cast(Any, result).data
    return cast(list[dict[str, Any]], data or [])


async def main() -> int:
    from arq import create_pool
    from arq.connections import RedisSettings

    settings = get_settings()
    redis_url = settings.redis_url
    print(f"Connecting to Redis at {redis_url}")

    try:
        pool = await create_pool(RedisSettings.from_dsn(redis_url))
    except Exception as exc:
        print(f"Redis connection failed: {exc}")
        print(
            "Hint: the prod Redis (6379) is bound to localhost on the Hetzner box. "
            "Open an SSH tunnel first, e.g.:\n"
            "  ssh -L 6379:127.0.0.1:6379 -i ~/.ssh/aegis_ci_deploy root@5.161.51.105\n"
            "then set REDIS_URL=redis://127.0.0.1:6379 in .env and re-run."
        )
        return 1

    sb = get_supabase()
    docs = _rows(
        sb.table("documents").select("id,merchant_id").eq("parse_status", "proceed").execute()
    )
    enqueued = 0
    skipped = 0
    for doc in docs:
        analysis = _rows(
            sb.table("analyses")
            .select("id,narrator_summary")
            .eq("document_id", doc["id"])
            .limit(1)
            .execute()
        )
        if not analysis:
            skipped += 1
            continue
        if analysis[0].get("narrator_summary"):
            skipped += 1
            continue
        await pool.enqueue_job(
            "generate_narrator_summary",
            str(doc["id"]),
            str(doc["merchant_id"]),
        )
        enqueued += 1

    print(f"Enqueued: {enqueued}  Skipped (no analysis OR already has summary): {skipped}")
    await pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
