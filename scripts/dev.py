#!/usr/bin/env python3
"""Local dev launcher — loads .env (BOM-aware) then runs uvicorn.

Why not ``uv run --env-file .env``:
    ``uv --env-file`` uses a strict dotenv parser that doesn't handle
    UTF-16-LE files (the default when a Windows operator has ever
    saved the file through PowerShell redirection or notepad's
    "Unicode" encoding). Same failure mode the operator hit five
    minutes ago.

What this does:
    1. Reads ``$REPO_ROOT/.env`` handling UTF-16-LE / UTF-16-BE /
       UTF-8-BOM / plain UTF-8 (same shape as
       ``scripts/narrator_backfill_local.py``).
    2. Sets env vars via ``os.environ.setdefault`` — existing shell
       values win, mirroring dotenv precedence.
    3. Boots uvicorn on ``AEGIS_DEV_HOST:AEGIS_DEV_PORT`` (defaults:
       127.0.0.1:8080) against ``aegis.api.app:app`` with --reload.

Usage:
    uv run python scripts/dev.py                     # 127.0.0.1:8080, reload on
    AEGIS_DEV_PORT=8000 uv run python scripts/dev.py # override port
    AEGIS_DEV_NORELOAD=1 uv run python scripts/dev.py  # disable auto-reload

Called by ``make dev`` and ``.\\dev.ps1``; both surfaces are one-shot
wrappers on top of this.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _REPO_ROOT / ".env"


def _load_env_file(path: Path) -> None:
    """Populate os.environ from ``path`` (BOM- and UTF-16-aware).

    Existing environment values win — matches the ``dotenv`` "shell
    wins" precedence. Silent no-op when the file doesn't exist so a
    minimal env-var-only setup still works.
    """
    if not path.exists():
        return
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe"):
        text = raw.decode("utf-16-le")
    elif raw.startswith(b"\xfe\xff"):
        text = raw.decode("utf-16-be")
    elif raw.startswith(b"\xef\xbb\xbf"):
        text = raw[3:].decode("utf-8")
    else:
        text = raw.decode("utf-8", errors="replace")
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("﻿")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(
            key.strip(),
            value.strip().strip('"').strip("'"),
        )


def main() -> int:
    _load_env_file(_ENV_FILE)

    # Local dev override. ``AEGIS_DATA_RESIDENCY_CONFIRMED`` is a
    # production boot guard that gates Bedrock calls on the operator's
    # conscious US-only acknowledgement. The workstation ``.env`` ships
    # it as ``false`` on purpose so a live-fire boot never runs by
    # accident. Local dev never routes Bedrock through anything (the
    # boot guard is the only blocker before the UI comes up), so we
    # flip it here — with a visible banner — rather than making every
    # operator edit ``.env`` before every ``make dev``.
    _previous = os.environ.get("AEGIS_DATA_RESIDENCY_CONFIRMED", "")
    if _previous.lower() != "true":
        os.environ["AEGIS_DATA_RESIDENCY_CONFIRMED"] = "true"
        print(
            f"  dev override: AEGIS_DATA_RESIDENCY_CONFIRMED=true (was {_previous or 'unset'!r})",
            flush=True,
        )

    # When Supabase creds aren't wired in ``.env`` (or the operator
    # explicitly wants a clean-slate boot), fall back to the in-memory
    # backend. That's what every unit test uses, so the same code
    # paths that exercise ``list_all()`` / ``get(merchant_id)`` /
    # ``list_documents`` in tests boot the app cleanly here too. To
    # hit the real prod Supabase from local dev, set both
    # ``SUPABASE_URL`` and ``SUPABASE_KEY`` (or the workspace
    # ``AEGIS_STORAGE_BACKEND=supabase`` explicitly).
    _sb_url = os.environ.get("SUPABASE_URL", "").strip()
    _sb_key = os.environ.get("SUPABASE_KEY", "").strip()
    if not _sb_url or not _sb_key:
        _existing_backend = os.environ.get("AEGIS_STORAGE_BACKEND", "").strip()
        if _existing_backend != "memory":
            os.environ["AEGIS_STORAGE_BACKEND"] = "memory"
            print(
                "  dev override: AEGIS_STORAGE_BACKEND=memory (SUPABASE_URL "
                "or SUPABASE_KEY is unset; set both to hit real Supabase)",
                flush=True,
            )

    host = os.environ.get("AEGIS_DEV_HOST", "127.0.0.1")
    port = int(os.environ.get("AEGIS_DEV_PORT", "8080"))
    no_reload = os.environ.get("AEGIS_DEV_NORELOAD") in {"1", "true", "yes"}

    banner = (
        f"\n  AEGIS local dev\n"
        f"  ---------------\n"
        f"  v2 UI:  http://{host}:{port}/v2/\n"
        f"  Legacy: http://{host}:{port}/ui/\n"
        f"  Health: http://{host}:{port}/healthz\n"
    )
    print(banner, flush=True)

    import uvicorn

    uvicorn.run(
        "aegis.api.app:app",
        host=host,
        port=port,
        reload=not no_reload,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
