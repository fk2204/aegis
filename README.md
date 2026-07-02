# AEGIS

[![test](https://github.com/fk2204/aegis/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/fk2204/aegis/actions/workflows/test.yml)

MCA underwriting brain for Commera Capital. Parses bank statements,
scores deals, generates state-compliant disclosures, syncs with Close.

Solo operator, ~100 deals/month. Internal-only. Python 3.12+.

## Where to start

1. **`CLAUDE.md`** — working agreement: architecture, non-negotiable rules,
   project structure. Read this before changing anything.
2. **`COMPLIANCE.md`** — quick-reference table for state regulation tiers
   and federal obligations. The detailed dossiers live under
   `docs/compliance/`.
3. **`CORPUS_FINDINGS.md`** — corpus failures and their fixes. Living log;
   add an entry every time a corpus run surfaces a parser bug.
4. **`docs/archive/REWRITE_PLAN.md`** — historical phased rewrite plan
   (archived; no longer maintained, kept for context only).

## Run locally

```bash
make install                 # uv sync
cp .env.example .env         # then fill in secrets and set
                             # AEGIS_DATA_RESIDENCY_CONFIRMED=true
make dev                     # uvicorn on 127.0.0.1:8080 with .env loaded
make worker                  # arq worker (separate terminal)
```

Once `make dev` is up:

- **v2 UI**: <http://127.0.0.1:8080/v2/>
- **Legacy UI**: <http://127.0.0.1:8080/ui/>

### Windows PowerShell shortcut

For operators on Windows without `make`:

```powershell
cd C:\Users\fkozi\aegis
.\dev.ps1                    # default: :8080 with --reload
.\dev.ps1 -Port 8000         # override port
.\dev.ps1 -NoReload          # disable auto-reload
```

Both `make dev` and `.\dev.ps1` delegate to `scripts/dev.py`, which:

* Reads `.env` handling UTF-16-LE / UTF-16-BE / UTF-8-BOM / UTF-8
  (Windows PowerShell redirect defaults break the strict dotenv
  parser `uv --env-file` uses).
* Sets `AEGIS_DATA_RESIDENCY_CONFIRMED=true` for the local process
  (dev-only — production still requires the operator to set it
  explicitly).
* Falls back to `AEGIS_STORAGE_BACKEND=memory` when `SUPABASE_URL`
  or `SUPABASE_KEY` is unset, so `/v2/` boots against the same
  in-memory repos the tests use. Set both to point local dev at
  real Supabase.

Each override prints a one-line banner at startup so it's obvious
which backend the process ended up on.

### Other one-shots

```bash
make shell                   # Python REPL with .env loaded
```

## Pre-commit gate

```bash
make check         # mypy --strict + ruff + pytest with corpus
make test-fast     # quick iteration without corpus
make install-hooks # one-time: install pre-commit (ruff + mypy + compliance-review)
```

`make check` is the only gate for the bulk of the codebase. There is no CI
by design — manual ssh + git is appropriate for a solo operator.

### Developer setup (pre-commit hooks)

Run `make install-hooks` once per clone. It installs the
[pre-commit](https://pre-commit.com/) framework via `uv tool install`
and wires three gates from `.pre-commit-config.yaml`:

- **ruff** + **ruff-format** on staged Python files (~50–150 ms).
- **mypy --strict** on `src/aegis/` (skipped when no `src/aegis/` file
  is staged).
- **compliance-review** annotation check on commits that touch
  `docs/compliance/states/**` (see "Narrow no-CI exception" below).

The compliance-review script lives at `.githooks/pre-commit` so it can
be invoked standalone (`bash .githooks/pre-commit`) for diagnosis. The
previous `core.hooksPath=.githooks` mechanism is gone; if you cloned
before this change, `make install-hooks` migrates you by unsetting it.

**Narrow no-CI exception:** commits that stage any file under
`docs/compliance/states/**` must include a `compliance-review:` annotation
(`approved by <name>`, `pending`, or `not-applicable`) in the commit
body. Background and rationale in `docs/AEGIS_MASTER_PLAN.md` §13.

## Generate the synthetic corpus

```bash
python -m scripts.generate_corpus            # write all PDFs + manifests
python -m scripts.generate_corpus --clean    # delete existing first
python -m scripts.generate_corpus --dry-run  # print plan, write nothing
```

PDFs land in `tests/fixtures/corpus/synthetic/`. Real-statement corpus
lives under `tests/fixtures/corpus/real/` — operator-supplied, gitignored.

## Deploy

Single Hetzner CPX21 box behind Cloudflare Tunnel + Access. See
`deploy/RUNBOOK.md` for operational procedures (ssh, logs, rollback,
key rotation). First-time setup: `deploy/install.sh`. Routine deploy:
`scripts/deploy.sh`.

## PDF dossier dev (Windows)

The dossier PDF export (`/ui/merchants/{id}/dossier.pdf`) uses
WeasyPrint, which needs native Pango/Cairo/HarfBuzz libs. Those ship
on the Hetzner box via `deploy/install.sh` but are absent on a stock
Windows dev box. Use WSL2 for local PDF rendering. The dossier route
returns a 503 with an explanatory `detail` field when the libs are
missing — the rest of the app is unaffected.

## License

Internal — not for redistribution.