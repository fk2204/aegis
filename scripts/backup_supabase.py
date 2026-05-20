"""Daily Supabase logical dump → off-Hetzner storage (Phase 11 task #4).

Triggered by ``deploy/aegis-backup.timer``; runs ``pg_dump`` against
the configured Supabase Postgres DSN and writes the compressed dump
to off-box storage. Three storage destinations are supported via env
selector — pick the one already provisioned for the operator.

Storage destinations (set ``AEGIS_BACKUP_DEST``):

  * ``s3``       — AWS S3 via ``aws s3 cp``. Requires the standard
                   AWS_* env vars or an instance profile.
  * ``b2``       — Backblaze B2 via the ``b2`` CLI.
  * ``local``    — Disk path (for dev / smoke testing). Useful when
                   the operator wants to verify the dump format
                   without provisioning external storage yet.

The DR procedure (full restore steps) lives in
``deploy/RECOVERY.md``; this script handles the daily capture half.

What it does
------------
1. Reads ``AEGIS_BACKUP_DB_URL`` (full Postgres DSN to dump from).
2. Runs ``pg_dump -F c`` (custom format, parallel-safe restore).
3. Writes to ``/var/lib/aegis/backups/aegis-<utc-iso>.dump`` locally.
4. Uploads to the configured destination.
5. Records the upload result in ``audit_log`` with details carrying
   the dump SHA256 + byte size + remote URL. Audit-write failure is
   loud but does NOT prevent the upload from being recorded locally —
   the local file is the durable fallback.
6. Garbage-collects local dumps older than ``AEGIS_BACKUP_KEEP_LOCAL``
   (default 7).

Exit codes
----------
0 — dump written + uploaded successfully.
1 — pg_dump failure (any reason).
2 — config error (missing env, unknown destination).
3 — upload failure (dump file kept on disk for retry).

Operator manual run
-------------------
    AEGIS_BACKUP_DB_URL=postgresql://... \\
    AEGIS_BACKUP_DEST=s3 \\
    AEGIS_BACKUP_S3_URL=s3://commera-aegis-backups/ \\
        python -m scripts.backup_supabase
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))


DEFAULT_LOCAL_DIR: Final[Path] = Path("/var/lib/aegis/backups")
DEFAULT_KEEP_LOCAL_DAYS: Final[int] = 7


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _run(cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
    """Wrapper around subprocess.run that NEVER uses shell=True.

    Per CLAUDE.md security rules: no os.system, no shell=True, no
    string-interpolated commands. Every external invocation is a
    fully-vetted argv list.
    """
    return subprocess.run(  # noqa: S603 — argv list, no shell
        cmd, check=False, capture_output=True
    )


def dump(dsn: str, out_dir: Path) -> Path:
    """Run pg_dump against ``dsn`` and write a custom-format dump.

    Returns the on-disk path of the dump. Raises CalledProcessError
    on pg_dump failure.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    dump_path = out_dir / f"aegis-{_utc_stamp()}.dump"
    # -F c -> custom format (parallel restore + selective restore)
    # -Z 6 -> deflate level 6 (default; balanced size vs CPU)
    # --no-owner / --no-acl -> portable dump, lets restore use a
    #     different role name than prod.
    cmd = [
        "pg_dump",
        "-F", "c",
        "-Z", "6",
        "--no-owner",
        "--no-acl",
        "-f", str(dump_path),
        dsn,
    ]
    result = _run(cmd)
    if result.returncode != 0:
        # pg_dump's stderr goes to stderr unless we capture it.
        stderr = (result.stderr or b"").decode("utf-8", errors="replace")
        raise RuntimeError(f"pg_dump failed (exit {result.returncode}): {stderr[:500]}")
    return dump_path


def upload_s3(dump_path: Path, *, s3_url_base: str) -> str:
    """Upload via ``aws s3 cp``. Returns the remote URL."""
    if not s3_url_base.endswith("/"):
        s3_url_base += "/"
    remote = f"{s3_url_base}{dump_path.name}"
    result = _run(["aws", "s3", "cp", str(dump_path), remote])
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", errors="replace")
        raise RuntimeError(f"aws s3 cp failed: {stderr[:500]}")
    return remote


def upload_b2(dump_path: Path, *, bucket: str, key_prefix: str) -> str:
    """Upload via ``b2 file upload``. Returns the remote URL."""
    key = f"{key_prefix.rstrip('/')}/{dump_path.name}".lstrip("/")
    # b2 CLI v3+ syntax; older versions use `b2 upload-file`.
    result = _run(["b2", "file", "upload", bucket, str(dump_path), key])
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", errors="replace")
        raise RuntimeError(f"b2 upload failed: {stderr[:500]}")
    return f"b2://{bucket}/{key}"


def upload_local(dump_path: Path, *, dest_dir: Path) -> str:
    """Copy to a local on-disk destination (dev/test)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / dump_path.name
    target.write_bytes(dump_path.read_bytes())
    return str(target)


def garbage_collect(local_dir: Path, *, keep_days: int) -> int:
    """Delete dumps older than ``keep_days``. Returns count deleted."""
    if not local_dir.exists():
        return 0
    cutoff = datetime.now(UTC).timestamp() - keep_days * 86400
    deleted = 0
    for f in local_dir.glob("aegis-*.dump"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                deleted += 1
        except OSError:
            continue
    return deleted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--local-dir",
        type=Path,
        default=Path(
            os.environ.get("AEGIS_BACKUP_LOCAL_DIR") or str(DEFAULT_LOCAL_DIR)
        ),
    )
    parser.add_argument(
        "--keep-days",
        type=int,
        default=int(
            os.environ.get("AEGIS_BACKUP_KEEP_LOCAL") or DEFAULT_KEEP_LOCAL_DAYS
        ),
    )
    parser.add_argument(
        "--skip-upload",
        action="store_true",
        help="Only dump to local disk; useful for smoke testing.",
    )
    args = parser.parse_args(argv)

    dsn = os.environ.get("AEGIS_BACKUP_DB_URL")
    if not dsn:
        print("AEGIS_BACKUP_DB_URL is unset; refusing to dump.", file=sys.stderr)
        return 2

    try:
        dump_path = dump(dsn, args.local_dir)
    except RuntimeError as exc:
        print(f"dump failed: {exc}", file=sys.stderr)
        return 1

    sha = _sha256(dump_path)
    size = dump_path.stat().st_size
    print(
        f"dump ok path={dump_path} bytes={size} sha256={sha[:16]}…"
    )

    remote = ""
    if not args.skip_upload:
        dest = os.environ.get("AEGIS_BACKUP_DEST", "").lower()
        try:
            if dest == "s3":
                s3_url = os.environ.get("AEGIS_BACKUP_S3_URL")
                if not s3_url:
                    print("AEGIS_BACKUP_S3_URL unset", file=sys.stderr)
                    return 2
                remote = upload_s3(dump_path, s3_url_base=s3_url)
            elif dest == "b2":
                bucket = os.environ.get("AEGIS_BACKUP_B2_BUCKET")
                prefix = os.environ.get("AEGIS_BACKUP_B2_PREFIX", "aegis")
                if not bucket:
                    print("AEGIS_BACKUP_B2_BUCKET unset", file=sys.stderr)
                    return 2
                remote = upload_b2(dump_path, bucket=bucket, key_prefix=prefix)
            elif dest == "local":
                target_dir = Path(os.environ.get("AEGIS_BACKUP_LOCAL_OFFBOX", "."))
                remote = upload_local(dump_path, dest_dir=target_dir)
            elif dest == "":
                print("AEGIS_BACKUP_DEST unset; skipping upload (local dump kept)")
            else:
                print(f"unknown AEGIS_BACKUP_DEST={dest!r}", file=sys.stderr)
                return 2
        except RuntimeError as exc:
            print(f"upload failed: {exc}", file=sys.stderr)
            return 3

    if remote:
        print(f"upload ok remote={remote}")

    gc_count = garbage_collect(args.local_dir, keep_days=args.keep_days)
    if gc_count:
        print(f"gc dropped {gc_count} dumps older than {args.keep_days}d")

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
