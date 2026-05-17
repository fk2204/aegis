# AEGIS

MCA underwriting brain for Commera Capital. Parses bank statements,
scores deals, generates state-compliant disclosures, syncs with Zoho.

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
make install      # uv sync
cp .env.example .env   # then fill in secrets and set
                       # AEGIS_DATA_RESIDENCY_CONFIRMED=true
make dev          # uvicorn on :5555
make worker       # arq worker (separate terminal)
```

## Pre-commit gate

```bash
make check        # mypy --strict + ruff + pytest with corpus
make test-fast    # quick iteration without corpus
```

`make check` is the only gate. There is no CI by design — manual ssh + git
is appropriate for a solo operator.

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