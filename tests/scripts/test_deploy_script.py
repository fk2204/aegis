"""Unit tests for ``scripts/deploy.sh`` and ``scripts/rollback.sh``.

These tests exercise the DRY_RUN path of both scripts, which prints
the resolved command sequence without executing ssh, ``make migrate``,
``systemctl``, or anything that would touch the live box. CI on Linux
runs them end-to-end; on a raw Windows host (no POSIX ``bash``) the
module auto-skips.

Sprint 5 Track A coverage:

* ``deploy.sh DRY_RUN=1`` parses + emits the full planned sequence and
  resolves the prod defaults (host, remote path, healthz url).
* ``rollback.sh DRY_RUN=1`` prints ``git reset --hard HEAD~1``.
* The defensive on-box ``chown`` line is present in the deploy phase A
  block so a stray root-owned file under ``/opt/aegis`` cannot block
  ``git pull`` again (Sprint 3 partial-pull failure mode).
* The sudoers-literal restart form (``sudo -n /usr/bin/systemctl restart
  aegis-web aegis-worker``) is the EXACT string emitted by both
  scripts — the box's NOPASSWD rule is matched verbatim, and any
  rewording would silently fall through to a password prompt.

Tests deliberately operate on script stdout/stderr via ``subprocess.run``
so the contract under test is "what the script tells the operator it
will do," not the internal shell helpers.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEPLOY_SH = _REPO_ROOT / "scripts" / "deploy.sh"
_ROLLBACK_SH = _REPO_ROOT / "scripts" / "rollback.sh"


def _resolved_bash_is_usable() -> bool:
    """``shutil.which('bash')`` on a vanilla Windows host resolves to
    ``C:\\Windows\\System32\\bash.exe`` -- the WSL launcher. If WSL
    isn't installed (no enabled distro) that launcher exits 1 before
    running the script, which would surface as a confusing "exited 1"
    failure here. Detect the launcher path and skip the module in
    that case; Git-Bash, MSYS2, WSL2-on-Linux, and real Linux all
    expose a usable bash elsewhere on PATH (or as the only candidate).
    """
    bash_path = shutil.which("bash")
    if bash_path is None:
        return False
    resolved = Path(bash_path).resolve()
    # Case-insensitive comparison: the WSL launcher path is always
    # under %SystemRoot%\System32\bash.exe on Windows.
    parts_lower = [p.lower() for p in resolved.parts]
    return not ("system32" in parts_lower and resolved.name.lower() == "bash.exe")


# Skip the whole module on hosts without a POSIX bash (e.g. raw Windows
# CMD) OR with only the broken WSL launcher. MSYS / Git-Bash / WSL2 /
# Linux all expose a usable bash. CI runs Linux so the gates exercise
# there.
pytestmark = pytest.mark.skipif(
    not _resolved_bash_is_usable(),
    reason="No usable POSIX bash on PATH (WSL stub or absent); deploy "
    "script tests need a real Unix shell",
)


def _run_dry(script: Path, **extra_env: str) -> subprocess.CompletedProcess[str]:
    """Run ``bash <script>`` with DRY_RUN=1 and capture stdout+stderr.

    The deploy and rollback scripts log progress to stderr (the
    ``[deploy]`` / ``[rollback]`` lines), so assertions check ``stderr``
    by default. ``stdout`` should be empty in dry-run.

    Resolves bash by absolute path so a Windows host with the WSL
    ``bash.exe`` launcher in ``C:/Windows/System32`` cannot intercept
    the call. Git-Bash, MSYS, WSL2-on-Linux, and native Linux all
    expose a POSIX bash that ``shutil.which`` finds.
    """

    bash_path = shutil.which("bash")
    if bash_path is None:  # pragma: no cover - guarded by module-level skip
        pytest.skip("bash not on PATH")

    env = {
        "DRY_RUN": "1",
        # Belt-and-suspenders: pre-flight env var so the script never
        # bails on the residency check even if a future refactor reads
        # it before the dry-run shortcut.
        "AEGIS_DATA_RESIDENCY_CONFIRMED": "true",
        # Keep parent PATH so the resolved bash can find /usr/bin
        # tooling (cat, grep) it shells out to internally.
        "PATH": str(_REPO_ROOT),
        **extra_env,
    }
    # Inherit SystemRoot on Windows so subprocess can spawn at all.
    import os as _os

    for _k in ("SystemRoot", "WINDIR", "TEMP", "TMP", "PATHEXT"):
        if _k in _os.environ and _k not in env:
            env[_k] = _os.environ[_k]
    # Preserve PATH from parent (prepended with bash's dir) so
    # /usr/bin/cat etc. resolve. shutil.which already found bash there.
    bash_dir = str(Path(bash_path).parent)
    env["PATH"] = bash_dir + _os.pathsep + _os.environ.get("PATH", "")

    # S603: argv-list, no shell=True, args are not user-controlled (bash
    # resolved by shutil.which, script is a repo path constant). Same
    # pattern as scripts/**/* operator scripts (see pyproject ignores).
    return subprocess.run(  # noqa: S603
        [bash_path, str(script)],
        capture_output=True,
        text=True,
        env=env,
        cwd=_REPO_ROOT,
        check=False,
    )


# ---------------------------------------------------------------------------
# deploy.sh
# ---------------------------------------------------------------------------


def test_deploy_dry_run_exits_zero() -> None:
    """DRY_RUN=1 short-circuits before any pre-flight failure mode."""
    result = _run_dry(_DEPLOY_SH, TARGET="prod")
    assert result.returncode == 0, (
        f"deploy.sh DRY_RUN exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_deploy_dry_run_resolves_prod_defaults() -> None:
    """The default host / path / healthz are the production values."""
    result = _run_dry(_DEPLOY_SH, TARGET="prod")
    combined = result.stdout + result.stderr
    assert "AEGIS_HOST=aegis@aegis-ssh.commerafunding.com" in combined
    assert "AEGIS_REMOTE_PATH=/opt/aegis" in combined
    assert "AEGIS_HEALTH_URL=http://127.0.0.1:5555/healthz" in combined


def test_deploy_dry_run_includes_make_migrate_step() -> None:
    """``make migrate TARGET=<TARGET>`` runs locally, between pull + restart."""
    result = _run_dry(_DEPLOY_SH, TARGET="prod")
    combined = result.stdout + result.stderr
    assert "make migrate TARGET=prod" in combined


def test_deploy_dry_run_includes_literal_sudo_restart_form() -> None:
    """The box's NOPASSWD rule matches argv literally; emitted form MUST be exact."""
    result = _run_dry(_DEPLOY_SH, TARGET="prod")
    combined = result.stdout + result.stderr
    assert "sudo -n /usr/bin/systemctl restart aegis-web aegis-worker" in combined


def test_deploy_dry_run_includes_is_active_verification() -> None:
    """systemctl is-active is part of the deploy contract, not just restart."""
    result = _run_dry(_DEPLOY_SH, TARGET="prod")
    combined = result.stdout + result.stderr
    assert "systemctl is-active aegis-web aegis-worker" in combined


def test_deploy_dry_run_includes_defensive_chown_in_phase_a() -> None:
    """The chown runs BEFORE git pull as preventative cleanup."""
    result = _run_dry(_DEPLOY_SH, TARGET="prod")
    combined = result.stdout + result.stderr
    # The chown line is present...
    assert "chown -R aegis:aegis" in combined
    # ...and the script labels it as part of phase A (pull).
    phase_a_marker = "on-box phase A (pull)"
    assert phase_a_marker in combined
    pre_pull = combined.split(phase_a_marker, 1)[1].split("step 3", 1)[0]
    assert "chown -R aegis:aegis" in pre_pull, (
        "chown must appear in phase A block before git pull, not later"
    )
    assert "git pull --ff-only origin main" in pre_pull
    # And the chown precedes the pull within phase A.
    assert pre_pull.index("chown -R aegis:aegis") < pre_pull.index("git pull")


def test_deploy_dry_run_does_not_invoke_ssh() -> None:
    """DRY_RUN is a preview; no live ssh, no live migrate.

    Asserts on the ASCII-only substring of the completion line so the
    test is stable across shells that round-trip the em dash as a
    different code point.
    """
    result = _run_dry(_DEPLOY_SH, TARGET="prod")
    combined = result.stdout + result.stderr
    assert "dry-run complete" in combined
    assert "no ssh, no migrate, no restart executed." in combined


def test_deploy_dry_run_respects_target_override() -> None:
    """TARGET=staging propagates into the make migrate step."""
    result = _run_dry(_DEPLOY_SH, TARGET="staging")
    combined = result.stdout + result.stderr
    assert "TARGET=staging" in combined
    assert "make migrate TARGET=staging" in combined


def test_deploy_dry_run_respects_host_override() -> None:
    """AEGIS_HOST override flows into the ssh-target preview lines."""
    result = _run_dry(
        _DEPLOY_SH,
        TARGET="prod",
        AEGIS_HOST="ops@example.internal",
    )
    combined = result.stdout + result.stderr
    assert "AEGIS_HOST=ops@example.internal" in combined
    assert "aegis@aegis-ssh.commerafunding.com" not in combined


# ---------------------------------------------------------------------------
# rollback.sh
# ---------------------------------------------------------------------------


def test_rollback_dry_run_exits_zero() -> None:
    result = _run_dry(_ROLLBACK_SH, TARGET="prod")
    assert result.returncode == 0, (
        f"rollback.sh DRY_RUN exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_rollback_dry_run_includes_git_reset_hard_head_minus_one() -> None:
    """The core safety-net action: revert exactly one commit on the box."""
    result = _run_dry(_ROLLBACK_SH, TARGET="prod")
    combined = result.stdout + result.stderr
    assert "git reset --hard HEAD~1" in combined


def test_rollback_dry_run_includes_literal_sudo_restart_form() -> None:
    """Same literal NOPASSWD form as deploy — must be byte-identical."""
    result = _run_dry(_ROLLBACK_SH, TARGET="prod")
    combined = result.stdout + result.stderr
    assert "sudo -n /usr/bin/systemctl restart aegis-web aegis-worker" in combined


def test_rollback_dry_run_includes_is_active_verification() -> None:
    result = _run_dry(_ROLLBACK_SH, TARGET="prod")
    combined = result.stdout + result.stderr
    assert "systemctl is-active aegis-web aegis-worker" in combined


def test_rollback_dry_run_resolves_prod_defaults() -> None:
    result = _run_dry(_ROLLBACK_SH, TARGET="prod")
    combined = result.stdout + result.stderr
    assert "AEGIS_HOST=aegis@aegis-ssh.commerafunding.com" in combined
    assert "AEGIS_REMOTE_PATH=/opt/aegis" in combined
    assert "AEGIS_HEALTH_URL=http://127.0.0.1:5555/healthz" in combined


def test_rollback_dry_run_does_not_invoke_ssh() -> None:
    """Stable across em-dash code-page round-tripping; see deploy variant."""
    result = _run_dry(_ROLLBACK_SH, TARGET="prod")
    combined = result.stdout + result.stderr
    assert "dry-run complete" in combined
    assert "no ssh executed." in combined
