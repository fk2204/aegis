"""Smoke tests for scripts/backup_supabase.py (mp Phase 11 task #4).

We don't exercise pg_dump or the cloud uploaders — those need real
external systems. The tests below pin the testable surfaces:

  * argparse + env handling: missing AEGIS_BACKUP_DB_URL → exit 2.
  * garbage_collect: only deletes dumps older than ``keep_days``.
  * _sha256: produces the expected hex digest.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from scripts.backup_supabase import _sha256, garbage_collect, main


def test_main_without_db_url_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("AEGIS_BACKUP_DB_URL", raising=False)
    assert main([]) == 2
    err = capsys.readouterr().err
    assert "AEGIS_BACKUP_DB_URL" in err


def test_sha256_matches_known_value(tmp_path: Path) -> None:
    p = tmp_path / "x"
    p.write_bytes(b"abc")
    # sha256("abc") = ba7816bf...
    assert _sha256(p).startswith("ba7816bf")


def test_garbage_collect_removes_only_old_dumps(tmp_path: Path) -> None:
    # Old dump (modified 30 days ago)
    old = tmp_path / "aegis-old.dump"
    old.write_bytes(b"old")
    old_time = time.time() - (30 * 86400)
    os.utime(old, (old_time, old_time))

    # Fresh dump (mtime now)
    fresh = tmp_path / "aegis-fresh.dump"
    fresh.write_bytes(b"fresh")

    deleted = garbage_collect(tmp_path, keep_days=7)
    assert deleted == 1
    assert not old.exists()
    assert fresh.exists()


def test_garbage_collect_no_directory_returns_zero(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    assert garbage_collect(missing, keep_days=7) == 0


def test_garbage_collect_ignores_non_dump_files(tmp_path: Path) -> None:
    other = tmp_path / "README.md"
    other.write_bytes(b"hello")
    old_time = time.time() - (30 * 86400)
    os.utime(other, (old_time, old_time))

    deleted = garbage_collect(tmp_path, keep_days=7)
    assert deleted == 0
    assert other.exists()
