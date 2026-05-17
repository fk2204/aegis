---
paths:
  - "tests/**"
  - "scripts/generate_corpus.py"
---

# AEGIS Testing Rules

Auto-loads when editing tests or the corpus generator.

---

## Test env vars: force-set, not setdefault

Tests that depend on env vars (`API_BEARER_TOKEN`, `AEGIS_STORAGE_BACKEND`, anything similar) MUST unconditionally write the test value in `tests/conftest.py`.

WRONG — silent no-op if env var is already set:
os.environ.setdefault("API_BEARER_TOKEN", "test-token")

RIGHT — force-set, regardless of pre-existing env:
os.environ["API_BEARER_TOKEN"] = "test-token"

**Why this matters:** The production box sources `/etc/aegis/aegis.env` for ops-side smoke tests. With `setdefault`, the real production creds silently leak into the `TestClient`, producing a fake "20 pre-existing failures" baseline (verified May 2026). Force-set always.

---

## Corpus rules (the firewall against parser drift)

- **NEVER auto-generate manifests for real statements.** Real-statement manifests are written by the operator from reading the statement. Auto-generation means grading the parser against its own output, which defeats the test.
- **NEVER scrape real statements** from anywhere. The operator provides them.
- **NEVER relax tolerances** to make the corpus pass. Tolerances are part of the contract:
  - Money totals (deposits, withdrawals, ADB, true_revenue): ±$1
  - Counts (NSF, MCA positions, transaction count): exact
  - Hard-decline reasons: exact set match
  - Fraud scores: ±5
  - Recommendation (approve/decline/refer): exact
- **NEVER consider the corpus passing if you retrofitted manifests to match output.** Adjusting a manifest is allowed only when the operator confirms the manifest itself was wrong, with the correction logged in `CORPUS_FINDINGS.md`.

---

## Corpus generator determinism

`scripts/generate_corpus.py` uses a fixed seed. The contract is: "two consecutive `--clean` runs produce identical SHA-256 hashes." If you touch the generator, verify this still holds before committing — `reportlab` has a `/CreationDate` and `/ModDate` trap that breaks reproducibility (use `invariant=True` on `canvas.Canvas`).

---

## Snapshot tests

State disclosure snapshots under `tests/snapshots/` lock the prescribed-form HTML. Updating a snapshot is a deliberate decision — include a comment in the commit explaining why the prescribed form's output changed (regulator update? bug fix? template correction?). Never update a snapshot to "make the test pass" without that reason.

---

## What to run

- `make test-fast` — quick iteration, skips corpus
- `make test` — full suite including corpus (sets `CORPUS=1` internally)
- `make check` — `typecheck + lint + test`, the pre-commit/pre-deploy gate

`CORPUS=1` is baked into `make test` so the operator cannot accidentally ship without corpus validation. The opt-out is `make test-fast`; the corpus is not opt-out.