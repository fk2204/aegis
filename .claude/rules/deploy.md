---
paths:
  - "deploy/**"
  - "scripts/deploy.sh"
  - "scripts/cf_*.sh"
---

# AEGIS Deployment Rules

Auto-loads when editing deploy artifacts or deploy scripts. Full ops procedures are in `deploy/RUNBOOK.md` — this file is rules that shape Claude's behavior, not procedure documentation.

---

## Deployment topology

- **Host:** Single Hetzner CPX21 VM (Ubuntu 24 LTS). Both FastAPI web (uvicorn) and arq worker run via systemd, no Docker.
- **Reverse proxy:** Cloudflare Tunnel (`cloudflared`, tunnel name `aegis-prod`) exposing two hostnames behind Cloudflare Access SSO:
  - `aegis.commerafunding.com` → FastAPI on `localhost:5555`
  - `aegis-ssh.commerafunding.com` → SSH on `localhost:22`
- **Bearer token** is a second layer behind Cloudflare Access on the API path.
- **SSH hostname is single-level** (`aegis-ssh.…`, not `ssh.aegis.…`) — Cloudflare Universal SSL only covers one-level subdomains; two-level fails TLS (verified 2026-05-12).

---

## Deploy access

Routine deploys go through `aegis@aegis-ssh.commerafunding.com` via SSH-over-Cloudflare-Access.

- The `aegis` user on the box owns `/opt/aegis`, has the `aegis_ed25519` public key in `~aegis/.ssh/authorized_keys`, and has a narrow sudoers rule allowing ONLY `systemctl restart aegis-web aegis-worker`.
- `git pull` and `uv sync` preserve correct file ownership without `sudo` because the source tree is `aegis`-owned.
- Root SSH via the same key is available for ops admin (journalctl as root, sudoers edits, key rotation, ufw/cloudflared changes). NOT the normal path.
- Direct-IP `root@5.161.51.105` with the `$HOME/.ssh/aegis_ed25519` key is the Access-down escape hatch only.

---

## Deploy script defaults — DO NOT CHANGE

`scripts/deploy.sh` defaults to `aegis@aegis-ssh.commerafunding.com`. Don't change the default.

The pre-flight local checks (`make check`) need `make`, `uv`, `mypy`, `ruff`, and `pytest` on PATH — won't work from a raw Windows shell without WSL2. From a Windows workstation, either run from WSL2, or run the remote half directly:
ssh aegis@aegis-ssh.commerafunding.com 'cd /opt/aegis && git pull --ff-only && uv sync && sudo systemctl restart aegis-web aegis-worker'

---

## Deploy artifact rules

- `deploy/install.sh` is idempotent — first-time setup on a fresh box. Re-runnable for system dependency reinstalls (WeasyPrint native libs in particular).
- `deploy/aegis-web.service` and `deploy/aegis-worker.service` both run as user `aegis`, load `/etc/aegis/aegis.env`, restart on failure.
- `/etc/aegis/aegis.env` is NOT the repo `.env`. It's a separate ops-managed file with prod secrets. Never copy the repo `.env` to the box.
- `AEGIS_ALLOW_PRODUCTION_SEED` must NEVER appear in `/etc/aegis/aegis.env` or any committed file — it's a deliberate barrier against the `scripts/audit/*` writers.

---

## Smoke test rule

After deploy, smoke-test only with **synthetic** statements from `tests/fixtures/corpus/synthetic/`. NEVER use a real statement on the production box during smoke testing — the deployed environment must not see real PII until the operator chooses to run a real deal through it.

---

## Rotation pointer

For credential rotation procedures (bearer token, Cloudflare Tunnel credentials, AWS Bedrock IAM keys, Zoho refresh token), see `deploy/RUNBOOK.md` § Secrets + key rotation. Always log rotations in the runbook's credential rotation log.

---

## Box-side operations gotchas

Real failure modes learned from prod work. Each one cost time / a credential rotation / hours of thrashing — captured here so the next session doesn't re-derive them.

### sudo from non-TTY shells needs the literal NOPASSWD form

The narrow sudoers rule on the box matches the command verbatim. From non-TTY shells (the Claude Code Bash tool, CI, anything not a real interactive terminal), bare `sudo systemctl restart aegis-web aegis-worker` falls through to a password prompt and hangs, because sudo's matcher does NOT resolve `systemctl` against PATH the way the shell does — it treats the bare command name as different from `/usr/bin/systemctl`.

The form that actually works from any shell:

```
sudo -n /usr/bin/systemctl restart aegis-web aegis-worker
```

`-n` is "non-interactive" (fail loud instead of prompting); the absolute path matches the sudoers rule literally. Read-only checks (`is-active`, `status`) run unprivileged — no sudo needed.

(Cost: ~30 minutes of thrashing through `-tt`, command-wrap, and root-ssh attempts on 2026-06-10.)

### `systemctl status <svc>` leaks tokens from the unit's argv

`cloudflared` (and any other service that takes a token on its argv, e.g. `--token …`) renders the full command line in `systemctl status <svc>`'s output. A `systemctl status cloudflared` dump on the box prints the tunnel token in plaintext. If the output is shared (paste, screenshot, log capture), the token needs to be rotated.

Use targeted journal queries instead:

```
sudo systemctl is-active cloudflared          # boolean health
journalctl -u cloudflared --output=cat -n 50  # log lines only, no argv
```

`is-active` runs without sudo for the operator user.

(Cost: a Cloudflare tunnel-token rotation on 2026-06-10.)

### Install scripts often contain hardcoded tokens

Helper scripts that match `install-*.ps1`, `provision-*.sh`, or anything `.gitignore`-excluded under `.tmp/` commonly hardcode secrets the operator pasted in during initial setup. Before reading or displaying the contents of any such file, grep first for `eyJ`, `token`, `secret`, `key=`. If a match returns, treat the file as a credential blob: don't print its contents to chat, don't paste it into anywhere that gets captured.

(Cost: a second tunnel-token exposure on 2026-06-10 when reading `install-cloudflare-tunnel.ps1`.)

### Read `.claude/rules/*.md` BEFORE the task, not after the failure

Before any deploy / parser / compliance / scoring / testing work, read the matching `.claude/rules/<area>.md` file. They are not abstract — they answer most operational questions verbatim (including the sudo and systemctl gotchas above) and exist because someone already paid the cost of discovering the answer the hard way. Auto-load triggers on file edits but does not fire when the work is "investigate why X fails on the box" — explicitly Read the relevant rules file first in those cases.

(Cost: ~5 failed SSH attempts on 2026-06-10 before reading this very file's prior version.)