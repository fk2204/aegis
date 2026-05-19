"""Operator-zero-touch Bedrock corpus verification harness.

Runs the full real-LLM corpus on the Hetzner production box over SSH and
returns the result to the local caller. The operator never types ssh.

Flow:
  1. scp local copies of `run_corpus_bedrock.py` and `compare_corpus_runs.py`
     into a fresh remote temp directory (so the box does not need to be at a
     specific git commit for the harness scripts themselves).
  2. SSH in twice, running the corpus once with AEGIS_PARSER_PAGE_ROUTING=0
     (baseline) and once with =1 (page-routing). Output JSON files land in
     the remote temp dir.
  3. SSH in a third time to run the diff via `compare_corpus_runs.py`. Its
     stdout (the PASS / GATE FAILURES block) is streamed back to the
     operator.
  4. Remote temp dir is cleaned up on success. On failure the temp dir is
     left in place so the operator can inspect — the path is printed.

The SSH host defaults to `aegis@aegis-ssh.commerafunding.com`, matching
`scripts/deploy.sh`. Override with `--host` or `AEGIS_HOST`.

The remote box is expected to already have AWS creds and BEDROCK_MODEL_ID
in `/etc/aegis/aegis.env` (loaded by the aegis-web/worker systemd units).
Those values are sourced into the script invocation via the env-file flag
on the wrapping shell so the corpus runner can see them.

Exits with the underlying compare script's exit code (0 = gate pass,
1 = gate failure, 2 = setup/config error).
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOST = "aegis@aegis-ssh.commerafunding.com"
DEFAULT_REMOTE_REPO = "/opt/aegis"
ENV_FILE_ON_BOX = "/etc/aegis/aegis.env"


def _ssh(host: str, remote_cmd: str, *, stream: bool = True) -> int:
    """Run `remote_cmd` on `host` via ssh. Stream output to local tty."""
    print(f"[ssh] {host}: {remote_cmd}", file=sys.stderr)
    completed = subprocess.run(
        ["ssh", host, "bash", "-lc", remote_cmd],
        stdout=None if stream else subprocess.PIPE,
        stderr=None if stream else subprocess.PIPE,
        check=False,
    )
    return completed.returncode


def _scp(local: Path, host: str, remote_path: str) -> int:
    print(f"[scp] {local} -> {host}:{remote_path}", file=sys.stderr)
    completed = subprocess.run(
        ["scp", "-q", str(local), f"{host}:{remote_path}"],
        check=False,
    )
    return completed.returncode


def _build_corpus_invocation(
    remote_dir: str,
    page_routing: bool,
    out_filename: str,
    limit: int,
) -> str:
    flag = "1" if page_routing else "0"
    parts = [
        f"set -a && source {shlex.quote(ENV_FILE_ON_BOX)} && set +a",
        f"cd {shlex.quote(DEFAULT_REMOTE_REPO)}",
        (
            f"AEGIS_PARSER_PAGE_ROUTING={flag} "
            f"uv run python {shlex.quote(remote_dir + '/run_corpus_bedrock.py')} "
            f"--out {shlex.quote(remote_dir + '/' + out_filename)}"
            + (f" --limit {limit}" if limit else "")
        ),
    ]
    return " && ".join(parts)


def _build_compare_invocation(remote_dir: str) -> str:
    parts = [
        f"set -a && source {shlex.quote(ENV_FILE_ON_BOX)} && set +a",
        f"cd {shlex.quote(DEFAULT_REMOTE_REPO)}",
        (
            f"uv run python {shlex.quote(remote_dir + '/compare_corpus_runs.py')} "
            f"--baseline {shlex.quote(remote_dir + '/baseline.json')} "
            f"--page-routing {shlex.quote(remote_dir + '/pagerouting.json')}"
        ),
    ]
    return " && ".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        default=os.environ.get("AEGIS_HOST", DEFAULT_HOST),
        help=f"SSH target (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Pass-through to run_corpus_bedrock.py; runs only first N PDFs (smoke).",
    )
    parser.add_argument(
        "--keep-remote",
        action="store_true",
        help="Don't clean up the remote temp dir on success (for inspection).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print every ssh/scp command without executing.",
    )
    args = parser.parse_args()

    local_runner = REPO_ROOT / "scripts" / "run_corpus_bedrock.py"
    local_compare = REPO_ROOT / "scripts" / "compare_corpus_runs.py"
    for p in (local_runner, local_compare):
        if not p.exists():
            print(f"ERROR: missing local script {p}", file=sys.stderr)
            return 2

    remote_dir = f"/tmp/aegis-verify-{uuid.uuid4().hex[:12]}"
    host = args.host

    if args.dry_run:
        print(f"[dry-run] mkdir {remote_dir} on {host}")
        print(f"[dry-run] scp {local_runner} -> {host}:{remote_dir}/run_corpus_bedrock.py")
        print(f"[dry-run] scp {local_compare} -> {host}:{remote_dir}/compare_corpus_runs.py")
        print(f"[dry-run] ssh {host} '{_build_corpus_invocation(remote_dir, False, 'baseline.json', args.limit)}'")
        print(f"[dry-run] ssh {host} '{_build_corpus_invocation(remote_dir, True, 'pagerouting.json', args.limit)}'")
        print(f"[dry-run] ssh {host} '{_build_compare_invocation(remote_dir)}'")
        print(f"[dry-run] (cleanup) ssh {host} 'rm -rf {remote_dir}'")
        return 0

    if _ssh(host, f"mkdir -p {shlex.quote(remote_dir)}") != 0:
        print(f"ERROR: could not create remote temp dir {remote_dir}", file=sys.stderr)
        return 2

    if _scp(local_runner, host, f"{remote_dir}/run_corpus_bedrock.py") != 0:
        print("ERROR: scp of run_corpus_bedrock.py failed", file=sys.stderr)
        return 2
    if _scp(local_compare, host, f"{remote_dir}/compare_corpus_runs.py") != 0:
        print("ERROR: scp of compare_corpus_runs.py failed", file=sys.stderr)
        return 2

    print(f"\n=== baseline run (AEGIS_PARSER_PAGE_ROUTING=0) ===", file=sys.stderr)
    rc = _ssh(host, _build_corpus_invocation(remote_dir, False, "baseline.json", args.limit))
    if rc != 0:
        print(f"\nERROR: baseline corpus run exited {rc}. Remote dir kept: {remote_dir}", file=sys.stderr)
        return rc

    print(f"\n=== page-routing run (AEGIS_PARSER_PAGE_ROUTING=1) ===", file=sys.stderr)
    rc = _ssh(host, _build_corpus_invocation(remote_dir, True, "pagerouting.json", args.limit))
    if rc != 0:
        print(f"\nERROR: page-routing corpus run exited {rc}. Remote dir kept: {remote_dir}", file=sys.stderr)
        return rc

    print(f"\n=== compare ===", file=sys.stderr)
    rc = _ssh(host, _build_compare_invocation(remote_dir))

    if rc == 0 and not args.keep_remote:
        _ssh(host, f"rm -rf {shlex.quote(remote_dir)}", stream=False)
    else:
        print(f"\nRemote artifacts kept at: {host}:{remote_dir}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
