"""Sprint 7 Track A — invariants for ``.github/workflows/deploy.yml``.

The auto-deploy workflow turns "merge to main" into "live on the box."
Several of its details are load-bearing in ways that don't surface
until they break in production:

* The trigger MUST be ``workflow_run`` watching the ``test`` workflow on
  ``main`` only — anything else would deploy on a failing pre-flight or
  from the wrong branch.
* The deploy job MUST gate on ``workflow_run.conclusion == 'success'``;
  ``workflow_run`` fires on every completion type (success, failure,
  cancelled, skipped), so an ungated job would deploy on red.
* The on-box restart MUST use the literal NOPASSWD form
  ``sudo -n /usr/bin/systemctl restart aegis-web aegis-worker``. The
  box's sudoers rule matches that exact argv; any rewording falls
  through to a password prompt and hangs (.claude/rules/deploy.md
  "sudo from non-TTY shells needs the literal NOPASSWD form").
* The SSH host MUST template from ``secrets.AEGIS_SERVER_IP`` as the
  ``root`` user. CF-Access hostname ``aegis-ssh.commerafunding.com``
  refuses GitHub Actions runner connections (no SSO cookie), so CI
  goes direct to the raw IP. The deploy keypair was provisioned
  into root's ``authorized_keys`` rather than ``~aegis/.ssh/...``,
  so the CI key cannot land as the aegis user; running as root
  makes ``sudo -n`` a no-op so the on-box command strings stay
  byte-identical to the manual ssh-as-aegis path.
* The deploy key MUST be stripped of CRLF before writing to
  ``~/.ssh/id_ed25519`` -- a Windows-edited paste lands in the
  GitHub secret store with ``\\r\\n`` and OpenSSH's libcrypto
  rejects the file silently with ``error in libcrypto``.
* The smoke endpoint MUST be ``http://127.0.0.1:5555/healthz`` — that's
  what the systemd unit exposes; anything else silently passes against
  the wrong service or doesn't pass at all.
* Concurrency MUST be set so two near-simultaneous merges queue rather
  than race for `/opt/aegis`.

This module asserts those invariants by parsing the workflow YAML and
dumping every step's `run:` block into a single string for substring
checks. It deliberately does not import any AEGIS code — it's a
workflow-only contract test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "deploy.yml"


def _load_workflow() -> dict[str, object]:
    """Return the parsed workflow YAML as a plain dict.

    PyYAML parses the YAML key ``on:`` as the boolean ``True`` (because
    YAML 1.1 treats ``on`` as a truthy alias). GitHub Actions reads it
    as a string. The tests below normalize by accepting either key.
    """
    assert _WORKFLOW_PATH.exists(), (
        f"deploy workflow missing at {_WORKFLOW_PATH}; Sprint 7 Track A "
        "shipped it — check the merge"
    )
    with _WORKFLOW_PATH.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    assert isinstance(loaded, dict), "deploy.yml must parse to a mapping"
    return loaded


def _trigger_block(workflow: dict[str, object]) -> dict[str, object]:
    # PyYAML quirk: ``on`` → True. Accept either. Cast through ``Any``
    # because dict[str, object].get(True) trips mypy --strict.
    trigger = workflow.get("on")
    if trigger is None:
        trigger = cast(dict[Any, object], workflow).get(True)
    assert isinstance(trigger, dict), (
        "deploy.yml must declare an `on:` block with a workflow_run trigger"
    )
    return trigger


def _deploy_job(workflow: dict[str, object]) -> dict[str, object]:
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), "workflow must declare a jobs block"
    job = jobs.get("deploy")
    assert isinstance(job, dict), "workflow must declare a `deploy` job"
    return job


def _flatten_run_steps(job: dict[str, object]) -> str:
    """Concatenate every step's name + run + env into one searchable string.

    Substring assertions below operate on this blob so they don't have
    to know which step a given command lives in — only that the
    workflow as a whole contains it.
    """
    steps = job.get("steps")
    assert isinstance(steps, list), "deploy job must declare steps"
    chunks: list[str] = []
    for step in steps:
        assert isinstance(step, dict), "each step must be a mapping"
        chunks.append(str(step.get("name", "")))
        chunks.append(str(step.get("run", "")))
        chunks.append(str(step.get("if", "")))
        env = step.get("env") or {}
        if isinstance(env, dict):
            for key, value in env.items():
                chunks.append(f"{key}={value}")
    return "\n".join(chunks)


# --- structure --------------------------------------------------------------


def test_deploy_workflow_file_exists() -> None:
    assert _WORKFLOW_PATH.exists(), f"missing {_WORKFLOW_PATH}"


def test_deploy_workflow_parses_as_yaml() -> None:
    # _load_workflow asserts the file exists and parses to a mapping.
    workflow = _load_workflow()
    assert workflow, "deploy.yml parsed empty"


# --- trigger ----------------------------------------------------------------


def test_trigger_is_workflow_run_on_test_main_only() -> None:
    workflow = _load_workflow()
    trigger = _trigger_block(workflow)
    assert "workflow_run" in trigger, (
        "deploy must be gated on the `test` workflow via workflow_run; "
        "a direct `push` trigger would skip the test gate entirely"
    )
    run_block = trigger["workflow_run"]
    assert isinstance(run_block, dict)
    assert run_block.get("workflows") == ["test"], (
        "workflow_run must observe the `test` workflow by name"
    )
    assert run_block.get("branches") == ["main"], (
        "workflow_run must restrict to main — feature branches must not auto-deploy"
    )
    # `types: [completed]` is the standard pattern for "wait for the
    # upstream to finish then check conclusion in the if: gate."
    assert run_block.get("types") == ["completed"], (
        "workflow_run must trigger on completed (success is checked in the job's if: gate)"
    )
    # Direct push/pr triggers would defeat the workflow_run gate.
    assert "push" not in trigger, "deploy must not auto-fire on push"
    assert "pull_request" not in trigger, (
        "deploy must not run on pull_request — only on the merge to main"
    )


def test_deploy_job_gates_on_test_success() -> None:
    workflow = _load_workflow()
    job = _deploy_job(workflow)
    if_clause = job.get("if")
    assert isinstance(if_clause, str), "deploy job must declare an `if:` gate"
    # Must check that the upstream test workflow concluded with success;
    # anything weaker (e.g. checking event_name only) would deploy on red.
    assert "workflow_run.conclusion" in if_clause, (
        "deploy job must check github.event.workflow_run.conclusion in its if:"
    )
    assert "'success'" in if_clause or '"success"' in if_clause, (
        "deploy job must require workflow_run.conclusion == 'success'"
    )


# --- concurrency + timeout --------------------------------------------------


def test_concurrency_serializes_deploys() -> None:
    workflow = _load_workflow()
    concurrency = workflow.get("concurrency")
    assert isinstance(concurrency, dict), (
        "deploy.yml must declare a concurrency block so two near-simultaneous "
        "merges queue rather than race for /opt/aegis"
    )
    assert concurrency.get("group"), "concurrency.group is required"
    # cancel-in-progress: false — a queued deploy must not be dropped.
    assert concurrency.get("cancel-in-progress") is False, (
        "cancel-in-progress must be false; dropping a queued deploy would skip the most recent code"
    )


def test_deploy_job_has_reasonable_timeout() -> None:
    workflow = _load_workflow()
    job = _deploy_job(workflow)
    timeout = job.get("timeout-minutes")
    assert isinstance(timeout, int), "deploy job must declare timeout-minutes"
    # Sprint 7 target was ~10 minutes; allow a small range.
    assert 5 <= timeout <= 20, f"deploy timeout should be ~10 minutes; got {timeout}"


# --- on-box invariants ------------------------------------------------------


def test_ssh_host_is_root_user_via_server_ip_secret() -> None:
    """SSH lands as ``root`` via the raw Hetzner IP held in the
    ``AEGIS_SERVER_IP`` secret -- NOT via the CF-Access-gated
    hostname ``aegis-ssh.commerafunding.com``, and NOT as the
    ``aegis`` user.

    Two routing decisions pinned here:

    1. **IP, not hostname.** Cloudflare Access refuses TCP from
       GitHub-hosted runners (no SSO cookie), so CI goes direct
       to the box's raw IP.
    2. **root, not aegis.** The deploy keypair was provisioned
       into root's authorized_keys, not ``~aegis/.ssh/authorized_keys``.
       Running as root makes ``sudo -n`` a no-op so the on-box
       command strings stay byte-identical to the manual
       ssh-as-aegis path.
    """
    workflow = _load_workflow()
    job = _deploy_job(workflow)
    blob = _flatten_run_steps(job)

    # Every ssh / ssh-keyscan must reference the secret directly --
    # no AEGIS_HOST / AEGIS_HOSTNAME indirection.
    assert "${{ secrets.AEGIS_SERVER_IP }}" in blob, (
        "deploy workflow must reference ${{ secrets.AEGIS_SERVER_IP }} "
        "directly in run: blocks -- no AEGIS_HOST / AEGIS_HOSTNAME "
        "indirection."
    )
    assert "root@${{ secrets.AEGIS_SERVER_IP }}" in blob, (
        "every SSH call must use root@${{ secrets.AEGIS_SERVER_IP }}; "
        "the CI deploy key is in root's authorized_keys, not aegis's."
    )
    # ssh-keyscan must target the same secret expression.
    keyscan_lines = [line for line in blob.splitlines() if "ssh-keyscan" in line]
    assert keyscan_lines, "deploy workflow must run ssh-keyscan"
    assert any("${{ secrets.AEGIS_SERVER_IP }}" in line for line in keyscan_lines), (
        "ssh-keyscan must reference ${{ secrets.AEGIS_SERVER_IP }} so the "
        "known_hosts pin matches the SSH target"
    )

    # Defense in depth: leftover $AEGIS_HOST / $AEGIS_HOSTNAME would
    # silently SSH to root@ with no host (env vars are gone) and fail
    # with a confusing "Could not resolve hostname".
    assert "$AEGIS_HOST" not in blob, (
        "stale $AEGIS_HOST reference; use ${{ secrets.AEGIS_SERVER_IP }} directly"
    )
    assert "$AEGIS_HOSTNAME" not in blob, (
        "stale $AEGIS_HOSTNAME reference; use ${{ secrets.AEGIS_SERVER_IP }} directly"
    )

    # And the job env must NOT declare the removed indirection vars.
    env = job.get("env")
    if isinstance(env, dict):
        assert "AEGIS_HOST" not in env, (
            "job env must NOT declare AEGIS_HOST -- secret-IP is referenced inline"
        )
        assert "AEGIS_HOSTNAME" not in env, (
            "job env must NOT declare AEGIS_HOSTNAME -- secret-IP is referenced inline"
        )


def test_ssh_key_strips_crlf_before_writing() -> None:
    """The deploy key's ``run:`` block MUST pipe the secret value
    through ``tr -d '\\r'`` before writing to ``~/.ssh/id_ed25519``.

    A secret value pasted from a Windows-edited buffer lands in the
    GH secret store with CRLF line endings. OpenSSH's libcrypto
    rejects such a key with ``error in libcrypto`` / ``invalid
    format`` even though the key looks fine in the GH UI -- the
    failure surfaces only on the runner. The CRLF strip is the
    one-line defense.
    """
    workflow = _load_workflow()
    job = _deploy_job(workflow)
    blob = _flatten_run_steps(job)
    assert "tr -d '\\r'" in blob or 'tr -d "\\r"' in blob, (
        "deploy workflow must pipe AEGIS_DEPLOY_SSH_KEY through `tr -d "
        "'\\r'` before writing ~/.ssh/id_ed25519. Without it, a CRLF-"
        "infected secret value crashes ssh with `error in libcrypto`."
    )


def test_nopasswd_restart_form_is_literal() -> None:
    workflow = _load_workflow()
    job = _deploy_job(workflow)
    blob = _flatten_run_steps(job)
    # The box's sudoers rule matches this exact argv. Any rewording
    # (e.g. dropping -n, using bare `sudo systemctl`, splitting into two
    # calls) falls through to a password prompt and hangs in CI.
    # See .claude/rules/deploy.md "sudo from non-TTY shells needs the
    # literal NOPASSWD form".
    assert "sudo -n /usr/bin/systemctl restart aegis-web aegis-worker" in blob, (
        "deploy must use the LITERAL form "
        "`sudo -n /usr/bin/systemctl restart aegis-web aegis-worker` — "
        "the box's sudoers rule matches verbatim"
    )


def test_healthz_smoke_targets_local_uvicorn_port() -> None:
    workflow = _load_workflow()
    job = _deploy_job(workflow)
    blob = _flatten_run_steps(job)
    blob += "\n" + str(job.get("env", {}))
    assert "http://127.0.0.1:5555/healthz" in blob, (
        "healthz smoke must hit the local uvicorn port via ssh — the "
        "systemd unit (deploy/aegis-web.service) binds 127.0.0.1:5555"
    )


def test_ssh_host_pinned_in_known_hosts_no_stricthostkeychecking_disable() -> None:
    """Pin the host key; never disable strict host key checking.

    `ssh-keyscan` is the right way to pin a host to known_hosts.
    `StrictHostKeyChecking=no` would short-circuit the pin and is a
    well-known smell in CI deploy pipelines.
    """
    workflow = _load_workflow()
    job = _deploy_job(workflow)
    blob = _flatten_run_steps(job)
    assert "ssh-keyscan" in blob, (
        "deploy must pin the host key via ssh-keyscan; otherwise the "
        "first SSH fails on the unknown-host prompt"
    )
    assert "StrictHostKeyChecking=no" not in blob, (
        "deploy must NOT disable StrictHostKeyChecking — that defeats "
        "the point of pinning known_hosts"
    )


# --- secrets ---------------------------------------------------------------


def test_aegis_deploy_ssh_key_secret_is_referenced() -> None:
    # Read the raw YAML so the secrets.X expression is visible (PyYAML
    # would keep it as a literal string anyway, but reading the source
    # is the clearest contract for the operator-setup doc).
    raw = _WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "secrets.AEGIS_DEPLOY_SSH_KEY" in raw, (
        "deploy must consume the AEGIS_DEPLOY_SSH_KEY repo secret — see "
        "CLAUDE.md § 'CI auto-deploy — operator one-time setup' for "
        "the provisioning procedure"
    )


def test_migrations_db_url_prod_secret_is_referenced() -> None:
    """Sprint 7 Track A picked migration option (b): runner-side apply.

    The prod box's `/etc/aegis/aegis.env` deliberately does NOT contain
    `MIGRATIONS_DB_URL_PROD` (the systemd units don't need DB-admin
    creds at runtime). So the workflow injects the DSN as a CI secret
    and runs `apply_migrations.py` on the runner — same posture as the
    existing `make deploy TARGET=prod` flow, which also drives
    migrations from the workstation rather than the box.
    """
    raw = _WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "secrets.MIGRATIONS_DB_URL_PROD" in raw, (
        "deploy must consume the MIGRATIONS_DB_URL_PROD repo secret — "
        "apply_migrations.py reads MIGRATIONS_DB_URL_PROD from the "
        "environment for --target prod"
    )


# --- migration step --------------------------------------------------------


def test_migration_step_invokes_apply_migrations_for_prod() -> None:
    workflow = _load_workflow()
    job = _deploy_job(workflow)
    blob = _flatten_run_steps(job)
    assert "scripts/apply_migrations.py" in blob, (
        "deploy must invoke scripts/apply_migrations.py; the runner is "
        "where migrations happen in Sprint 7 (option b)"
    )
    assert "--target prod" in blob, (
        "apply_migrations.py must be called with --target prod — its "
        "prod guard requires the flag when the DSN resolves to the "
        "prod project ref"
    )


# --- smoke step ------------------------------------------------------------


@pytest.mark.parametrize(
    "fragment",
    [
        "for attempt in 1 2 3 4 5",  # 5 retries
        "sleep 2",  # 2s gap between retries
        "--max-time 3",  # per-attempt curl timeout
    ],
)
def test_smoke_step_uses_retry_loop(fragment: str) -> None:
    workflow = _load_workflow()
    job = _deploy_job(workflow)
    blob = _flatten_run_steps(job)
    assert fragment in blob, (
        f"healthz smoke must include `{fragment}` so a slow restart "
        "doesn't fail the deploy on a transient curl miss"
    )


def test_failure_annotation_step_exists() -> None:
    workflow = _load_workflow()
    job = _deploy_job(workflow)
    steps = job["steps"]
    assert isinstance(steps, list)
    failure_steps = [
        s for s in steps if isinstance(s, dict) and "failure()" in str(s.get("if", ""))
    ]
    assert failure_steps, (
        "deploy must include a final `if: failure()` step that emits a "
        "::error:: annotation so the operator sees a clear failure summary"
    )
