"""Operator-zero-touch Bedrock corpus verification harness.

Runs the full real-LLM corpus on the Hetzner production box and returns
the gate verdict to the local caller. The operator never types ssh.

Flow (post-2026-05-19 transport-decoupled rewrite)
--------------------------------------------------
The SSH transport is on the critical path only briefly. The corpus run
itself is launched on the box with `nohup ... &`, fully detached from
the SSH session that spawned it. The harness then polls for a `.done`
marker via short-lived SSH calls — each well under any Cloudflare
Access tunnel session limit.

  1. Pre-flight (local): scripts present, corpus dir present + non-empty.
  2. SSH: mkdir /tmp/aegis-verify-XXX on the box.
  3. scp:
       - scripts/run_corpus_bedrock.py
       - scripts/compare_corpus_runs.py            (used only locally now)
       - scripts/_verify_leg_runner.sh             (remote wrapper)
       - tests/fixtures/corpus/synthetic/  (recursive)
  4. SSH (short): chmod +x the wrapper.
  5. SSH (short, ~2s): launch baseline leg detached —
       cd <dir> && nohup bash _verify_leg_runner.sh baseline 0 ... &
       echo $! > baseline.pid ; disown
       The ssh session exits as soon as the foreground statements end.
  6. POLL loop (short SSH every ~20s): does baseline.done exist?
       The wrapper writes baseline.exitcode BEFORE touching baseline.done,
       so if .done exists, .exitcode has the final exit code. On
       timeout (default 90 min/leg) the harness fetches the tail of
       the leg's stdout/stderr logs and surfaces — that timeout means
       the corpus genuinely hung, not a transport drop.
  7. Launch page-routing leg, poll again.
  8. scp FROM the box: baseline.json + pagerouting.json into a local
     tempdir. compare_corpus_runs.py then runs LOCALLY against those
     fixtures — no Bedrock cost, no remote dependency.
  9. On PASS: ssh rm -rf the remote dir. On FAIL: leave it.

Why nohup/poll instead of a single long SSH
-------------------------------------------
The 2026-05-19 16:28 + 19:02 UTC runs both died around the 21-32 min
mark with "Connection closed by remote host; client_loop: Broken pipe"
even with ServerAliveInterval=30. That points to a hard duration limit
on the Cloudflare Access tunnel that ferries SSH to the box, not an
idle-detection cut. A keepalive can't fix a hard timeout. Decoupling
the corpus run from the SSH session removes the transport from the
critical path entirely — the remote run keeps going whether SSH stays
up or not.

Exit codes
----------
  0 — gate PASS
  1 — gate FAIL or leg exited non-zero (real failure, surface to operator)
  2 — setup/config error (missing scripts, scp/mkdir failures)
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCAL_CORPUS_DIR = REPO_ROOT / "tests" / "fixtures" / "corpus" / "synthetic"
REMOTE_CORPUS_SUBDIR = "synthetic"
DEFAULT_HOST = "aegis@aegis-ssh.commerafunding.com"
DEFAULT_REMOTE_REPO = "/opt/aegis"
ENV_FILE_ON_BOX = "/etc/aegis/aegis.env"

# Keepalive options applied to every ssh/scp invocation. Less critical
# now that no SSH session needs to live for the whole corpus run, but
# still useful for the longer-running scp -r of the corpus dir (~1.5MB)
# and for keeping poll-interval SSH calls robust against any flaky
# intermediate hop.
SSH_KEEPALIVE_OPTS = [
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=10",
]

# Poll defaults.
DEFAULT_POLL_INTERVAL_SECONDS = 20
DEFAULT_TIMEOUT_MINUTES_PER_LEG = 90


# --------------------------------------------------------------------------
# SSH/SCP primitives
# --------------------------------------------------------------------------


def _ssh(host: str, remote_cmd: str, *, stream: bool = True) -> int:
    """Run `remote_cmd` on `host` via ssh, streaming output to the local tty."""
    print(f"[ssh] {host}: {remote_cmd}", file=sys.stderr)
    # `remote_cmd` is passed as a SINGLE argv element to ssh — ssh joins
    # post-host argv with spaces, so anything multi-arg here would be
    # re-split by the remote shell incorrectly. Same convention
    # scripts/deploy.sh uses.
    argv = ["ssh", *SSH_KEEPALIVE_OPTS, host, remote_cmd]
    completed = subprocess.run(
        argv,
        stdout=None if stream else subprocess.PIPE,
        stderr=None if stream else subprocess.PIPE,
        check=False,
    )
    return completed.returncode


def _ssh_capture(host: str, remote_cmd: str) -> tuple[int, str]:
    """Run `remote_cmd` on `host` and capture stdout. Used by the poll loop."""
    argv = ["ssh", *SSH_KEEPALIVE_OPTS, host, remote_cmd]
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, completed.stdout


def _scp(local: Path, host: str, remote_path: str) -> int:
    print(f"[scp] {local} -> {host}:{remote_path}", file=sys.stderr)
    completed = subprocess.run(
        ["scp", *SSH_KEEPALIVE_OPTS, "-q", str(local), f"{host}:{remote_path}"],
        check=False,
    )
    return completed.returncode


def _scp_dir(local_dir: Path, host: str, remote_parent: str) -> int:
    """Recursively scp `local_dir` so it appears as `remote_parent/<basename>`."""
    print(f"[scp -r] {local_dir} -> {host}:{remote_parent}/", file=sys.stderr)
    completed = subprocess.run(
        ["scp", *SSH_KEEPALIVE_OPTS, "-rq", str(local_dir), f"{host}:{remote_parent}/"],
        check=False,
    )
    return completed.returncode


def _scp_from(host: str, remote_path: str, local_path: Path) -> int:
    """Pull a file FROM the box into `local_path`."""
    print(f"[scp <-] {host}:{remote_path} -> {local_path}", file=sys.stderr)
    completed = subprocess.run(
        ["scp", *SSH_KEEPALIVE_OPTS, "-q", f"{host}:{remote_path}", str(local_path)],
        check=False,
    )
    return completed.returncode


def _scp_wrapper(local_wrapper: Path, host: str, remote_path: str) -> int:
    """SCP the leg wrapper shell script with normalized LF line endings.

    Defence-in-depth: even with the matching .gitattributes entry, an
    unusual checkout configuration could land CRLF line endings on the
    Windows working copy. bash on Linux rejects CRLF in the shebang
    (`/usr/bin/env bash\r: No such file or directory`), so we strip
    any \\r bytes locally before transfer.
    """
    print(f"[scp wrapper] {local_wrapper} -> {host}:{remote_path}", file=sys.stderr)
    content = local_wrapper.read_bytes().replace(b"\r\n", b"\n")
    with tempfile.NamedTemporaryFile(
        mode="wb", suffix=".sh", delete=False
    ) as tf:
        tf.write(content)
        tmp_path = Path(tf.name)
    try:
        return _scp(tmp_path, host, remote_path)
    finally:
        tmp_path.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Detached leg launch + poll
# --------------------------------------------------------------------------


def _launch_leg(
    host: str,
    remote_dir: str,
    leg_name: str,
    page_routing: bool,
    limit: int,
) -> int:
    """Launch a corpus leg detached. Returns 0 on successful launch."""
    out_filename = "baseline.json" if not page_routing else "pagerouting.json"
    out_json = f"{remote_dir}/{out_filename}"
    corpus_root = f"{remote_dir}/{REMOTE_CORPUS_SUBDIR}"
    pid_file = f"{remote_dir}/{leg_name}.pid"
    wrapper = f"{remote_dir}/_verify_leg_runner.sh"

    wrapper_args = [
        shlex.quote(leg_name),
        shlex.quote("1" if page_routing else "0"),
        shlex.quote(out_json),
        shlex.quote(corpus_root),
        shlex.quote(remote_dir),
    ]
    if limit:
        wrapper_args.append(shlex.quote(str(limit)))

    # The trailing `& PID=$!; echo $PID > pid_file; disown` chain makes
    # the spawned process fully independent of the ssh session:
    #   - nohup       — ignore SIGHUP when the SSH parent exits
    #   - </dev/null  — detach stdin so ssh doesn't keep the channel open
    #   - >/dev/null  — same for stdout (wrapper handles its own log files)
    #   - 2>&1        — same for stderr
    #   - &           — background; shell does not wait
    #   - disown      — drop from job control; even non-interactive sshd
    #                   won't try to wait for it
    launch_cmd = (
        f"cd {shlex.quote(remote_dir)} && "
        f"nohup bash {shlex.quote(wrapper)} {' '.join(wrapper_args)} "
        f"</dev/null >/dev/null 2>&1 & "
        f"PID=$! ; "
        f"echo $PID > {shlex.quote(pid_file)} ; "
        f"disown $PID 2>/dev/null || true"
    )

    print(f"\n=== launching {leg_name} leg detached ===", file=sys.stderr)
    rc = _ssh(host, launch_cmd)
    if rc != 0:
        print(f"ERROR: ssh-launch of {leg_name} returned {rc}", file=sys.stderr)
        return rc

    # Verify the PID file actually landed and contains an integer. A
    # transport drop between the launch and the response is possible
    # in principle (nohup makes the process survive, but the pid file
    # write happens in the parent shell which could die mid-write).
    rc, out = _ssh_capture(host, f"test -s {shlex.quote(pid_file)} && cat {shlex.quote(pid_file)}")
    pid_str = out.strip()
    if rc != 0 or not pid_str.isdigit():
        print(
            f"ERROR: {leg_name} pid file missing or empty after launch "
            f"(rc={rc}, out={out!r}). Remote dir kept: {remote_dir}",
            file=sys.stderr,
        )
        return 1
    print(f"[launch] {leg_name}: detached pid {pid_str}", file=sys.stderr)
    return 0


def _poll_leg(
    host: str,
    remote_dir: str,
    leg_name: str,
    timeout_minutes: int,
    poll_interval: int,
) -> int | None:
    """Poll for the leg's .done marker. Return its exit code, or None on timeout."""
    deadline = time.monotonic() + timeout_minutes * 60
    done_path = f"{remote_dir}/{leg_name}.done"
    exitcode_path = f"{remote_dir}/{leg_name}.exitcode"
    pid_path = f"{remote_dir}/{leg_name}.pid"
    poll_cmd = (
        f"if [ -f {shlex.quote(done_path)} ]; then "
        f"echo DONE; cat {shlex.quote(exitcode_path)}; "
        f"elif [ -f {shlex.quote(pid_path)} ] && "
        f"kill -0 \"$(cat {shlex.quote(pid_path)})\" 2>/dev/null; then "
        f"echo RUNNING; "
        f"else echo CRASHED; fi"
    )

    last_log_at = 0.0
    print(
        f"[poll] {leg_name}: polling every {poll_interval}s "
        f"(timeout {timeout_minutes} min)",
        file=sys.stderr,
    )
    while time.monotonic() < deadline:
        rc, out = _ssh_capture(host, poll_cmd)
        if rc != 0:
            # Transient SSH issue during a short poll call — log and retry.
            print(
                f"[poll] {leg_name}: ssh-capture rc={rc}; retrying in {poll_interval}s",
                file=sys.stderr,
            )
            time.sleep(poll_interval)
            continue
        lines = out.strip().splitlines()
        first = lines[0].strip() if lines else ""
        if first == "DONE":
            try:
                code = int(lines[1].strip())
            except (IndexError, ValueError):
                print(
                    f"[poll] {leg_name}: .done present but exitcode unreadable: {out!r}",
                    file=sys.stderr,
                )
                return 1
            elapsed = int(timeout_minutes * 60 - (deadline - time.monotonic()))
            print(
                f"[poll] {leg_name}: DONE (exit {code}, ~{elapsed}s elapsed)",
                file=sys.stderr,
            )
            return code
        if first == "RUNNING":
            now = time.monotonic()
            if now - last_log_at > 60:
                elapsed = int(timeout_minutes * 60 - (deadline - now))
                print(
                    f"[poll] {leg_name}: running (elapsed ~{elapsed}s)",
                    file=sys.stderr,
                )
                last_log_at = now
            time.sleep(poll_interval)
            continue
        if first == "CRASHED":
            print(
                f"[poll] {leg_name}: process gone without .done marker. "
                "Wrapper or kernel killed it before exitcode/done could land.",
                file=sys.stderr,
            )
            return 1
        # Unknown response — log, then retry.
        print(f"[poll] {leg_name}: unexpected poll output {out!r}", file=sys.stderr)
        time.sleep(poll_interval)
    return None  # signals timeout


def _print_log_tail(host: str, remote_dir: str, leg_name: str, n_lines: int) -> None:
    """Stream the last `n_lines` of stdout/stderr for `leg_name` to operator tty."""
    print(f"\n--- {leg_name}.stdout.log (last {n_lines} lines) ---", file=sys.stderr)
    _ssh(host, f"tail -n {n_lines} {shlex.quote(remote_dir)}/{leg_name}.stdout.log || true")
    print(f"\n--- {leg_name}.stderr.log (last {n_lines} lines) ---", file=sys.stderr)
    _ssh(host, f"tail -n {n_lines} {shlex.quote(remote_dir)}/{leg_name}.stderr.log || true")


# --------------------------------------------------------------------------
# Local compare
# --------------------------------------------------------------------------


def _run_compare_locally(
    local_compare: Path,
    local_baseline: Path,
    local_pageroute: Path,
) -> int:
    """Run compare_corpus_runs.py LOCALLY (it's a stdlib-only script, fast)."""
    print("\n=== compare (local) ===", file=sys.stderr)
    argv = [
        sys.executable,
        str(local_compare),
        "--baseline", str(local_baseline),
        "--page-routing", str(local_pageroute),
    ]
    completed = subprocess.run(argv, check=False)
    return completed.returncode


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


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
        help="Pass-through to run_corpus_bedrock.py; runs only first N PDFs.",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help=f"Seconds between poll-status SSH calls (default {DEFAULT_POLL_INTERVAL_SECONDS}).",
    )
    parser.add_argument(
        "--timeout-minutes",
        type=int,
        default=DEFAULT_TIMEOUT_MINUTES_PER_LEG,
        help=(
            f"Max wallclock per leg before declaring timeout "
            f"(default {DEFAULT_TIMEOUT_MINUTES_PER_LEG}). The corpus should finish "
            "in ~25-30 min/leg; this defends against a runaway hang."
        ),
    )
    parser.add_argument(
        "--keep-remote",
        action="store_true",
        help="Don't clean up the remote temp dir on success (for inspection).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan without executing.",
    )
    args = parser.parse_args()

    local_runner = REPO_ROOT / "scripts" / "run_corpus_bedrock.py"
    local_compare = REPO_ROOT / "scripts" / "compare_corpus_runs.py"
    local_wrapper = REPO_ROOT / "scripts" / "_verify_leg_runner.sh"
    for p in (local_runner, local_compare, local_wrapper):
        if not p.exists():
            print(f"ERROR: missing local script {p}", file=sys.stderr)
            return 2
    if not LOCAL_CORPUS_DIR.is_dir():
        print(
            f"ERROR: missing local corpus dir {LOCAL_CORPUS_DIR}. "
            "Expected synthetic/ to exist under tests/fixtures/corpus/.",
            file=sys.stderr,
        )
        return 2
    pdf_count = sum(1 for _ in LOCAL_CORPUS_DIR.glob("*.pdf"))
    if pdf_count == 0:
        print(f"ERROR: no *.pdf under {LOCAL_CORPUS_DIR}", file=sys.stderr)
        return 2

    remote_dir = f"/tmp/aegis-verify-{uuid.uuid4().hex[:12]}"
    host = args.host

    if args.dry_run:
        print("[dry-run] plan:", file=sys.stderr)
        print(f"  1. ssh    : mkdir -p {remote_dir}", file=sys.stderr)
        print(f"  2. scp    : run_corpus_bedrock.py + compare_corpus_runs.py + "
              f"_verify_leg_runner.sh -> {remote_dir}/", file=sys.stderr)
        print(f"  3. scp -r : {LOCAL_CORPUS_DIR} -> {remote_dir}/synthetic "
              f"({pdf_count} PDFs)", file=sys.stderr)
        print(f"  4. ssh    : chmod +x {remote_dir}/_verify_leg_runner.sh", file=sys.stderr)
        print("  5. ssh    : nohup launch baseline leg (detached)", file=sys.stderr)
        print(f"  6. poll   : ~{args.poll_interval}s/check until baseline.done "
              f"(timeout {args.timeout_minutes} min)", file=sys.stderr)
        print("  7. ssh    : nohup launch pageroute leg (detached)", file=sys.stderr)
        print("  8. poll   : same, pageroute.done", file=sys.stderr)
        print("  9. scp <- : baseline.json + pagerouting.json -> local tempdir", file=sys.stderr)
        print(" 10. local  : python compare_corpus_runs.py", file=sys.stderr)
        print(f" 11. ssh    : rm -rf {remote_dir}  (on PASS only)", file=sys.stderr)
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
    if _scp_wrapper(local_wrapper, host, f"{remote_dir}/_verify_leg_runner.sh") != 0:
        print("ERROR: scp of _verify_leg_runner.sh failed", file=sys.stderr)
        return 2
    print(f"[scp -r] shipping {pdf_count} synthetic PDFs + manifests", file=sys.stderr)
    if _scp_dir(LOCAL_CORPUS_DIR, host, remote_dir) != 0:
        print(
            f"ERROR: scp -r of {LOCAL_CORPUS_DIR} failed. Remote temp dir kept: "
            f"{remote_dir}",
            file=sys.stderr,
        )
        return 2
    if _ssh(host, f"chmod +x {shlex.quote(remote_dir)}/_verify_leg_runner.sh") != 0:
        print("ERROR: chmod +x of wrapper failed", file=sys.stderr)
        return 2

    # ----- BASELINE leg --------------------------------------------------
    if _launch_leg(host, remote_dir, "baseline", page_routing=False, limit=args.limit) != 0:
        return 2
    baseline_rc = _poll_leg(
        host, remote_dir, "baseline",
        timeout_minutes=args.timeout_minutes,
        poll_interval=args.poll_interval,
    )
    if baseline_rc is None:
        print(
            f"\nTIMEOUT: baseline did not finish in {args.timeout_minutes} min. "
            "This is a REAL failure (not transport) — the corpus run hung.",
            file=sys.stderr,
        )
        _print_log_tail(host, remote_dir, "baseline", 200)
        print(f"\nRemote artifacts kept at: {host}:{remote_dir}", file=sys.stderr)
        return 1
    if baseline_rc != 0:
        print(f"\nERROR: baseline leg exited {baseline_rc}", file=sys.stderr)
        _print_log_tail(host, remote_dir, "baseline", 200)
        print(f"\nRemote artifacts kept at: {host}:{remote_dir}", file=sys.stderr)
        return baseline_rc

    # ----- PAGE-ROUTING leg ----------------------------------------------
    if _launch_leg(host, remote_dir, "pageroute", page_routing=True, limit=args.limit) != 0:
        return 2
    pageroute_rc = _poll_leg(
        host, remote_dir, "pageroute",
        timeout_minutes=args.timeout_minutes,
        poll_interval=args.poll_interval,
    )
    if pageroute_rc is None:
        print(
            f"\nTIMEOUT: pageroute did not finish in {args.timeout_minutes} min.",
            file=sys.stderr,
        )
        _print_log_tail(host, remote_dir, "pageroute", 200)
        print(f"\nRemote artifacts kept at: {host}:{remote_dir}", file=sys.stderr)
        return 1
    if pageroute_rc != 0:
        print(f"\nERROR: pageroute leg exited {pageroute_rc}", file=sys.stderr)
        _print_log_tail(host, remote_dir, "pageroute", 200)
        print(f"\nRemote artifacts kept at: {host}:{remote_dir}", file=sys.stderr)
        return pageroute_rc

    # ----- FETCH + LOCAL COMPARE ----------------------------------------
    with tempfile.TemporaryDirectory(prefix="aegis-verify-local-") as td:
        td_path = Path(td)
        local_baseline = td_path / "baseline.json"
        local_pageroute = td_path / "pagerouting.json"
        if _scp_from(host, f"{remote_dir}/baseline.json", local_baseline) != 0:
            print("ERROR: scp pull of baseline.json failed", file=sys.stderr)
            print(f"Remote artifacts kept at: {host}:{remote_dir}", file=sys.stderr)
            return 2
        if _scp_from(host, f"{remote_dir}/pagerouting.json", local_pageroute) != 0:
            print("ERROR: scp pull of pagerouting.json failed", file=sys.stderr)
            print(f"Remote artifacts kept at: {host}:{remote_dir}", file=sys.stderr)
            return 2
        compare_rc = _run_compare_locally(local_compare, local_baseline, local_pageroute)

    if compare_rc == 0 and not args.keep_remote:
        _ssh(host, f"rm -rf {shlex.quote(remote_dir)}", stream=False)
    else:
        print(f"\nRemote artifacts kept at: {host}:{remote_dir}", file=sys.stderr)
    return compare_rc


if __name__ == "__main__":
    raise SystemExit(main())
