# Corpus findings

Per Phase 5.5 of `REWRITE_PLAN.md`, every fix surfaced by the corpus
suite is logged here with the scenario, root cause, fix, and the SHA
that resolved it.

## Initial corpus build (2026-05-09)

**Bootstrapped corpus** of 6 PDFs across 4 scenarios × 2 banks:

| Scenario           | Chase Business | BoA Business |
|--------------------|:--------------:|:------------:|
| clean_profitable   | ✓              | ✓            |
| nsf_heavy          | ✓              | ✓            |
| mca_stacked        | ✓              | —            |
| math_tampered      | ✓              | —            |

All 6 currently pass. The framework supports adding more by appending a
`Recipe(bank, scenario, seed)` to `CORPUS_RECIPES` in
`scripts/generate_corpus.py`. Phase 5.5's target of ≥50 PDFs across 13
scenarios is incremental work tracked under task #9 in the operator's
queue.

### Finding 1 — withdrawal_total sign convention

- **Surface:** every non-tampered scenario (5 of 6 manifests).
- **Symptom:** `validate_extraction` raised
  `reconciliation_failed_withdrawal_total: listed N vs printed -N`.
- **Root cause:** the corpus generator stored `withdrawal_total` as a
  signed (negative) value; the validator compares it against the sum of
  `abs(amount)` over negative rows, which is positive. The contract is
  "printed totals are positive", not "signed".
- **Fix:** `scripts/generate_corpus.py` — store `withdrawal_total` as
  `-withdrawals_signed` and print as a positive figure on the rendered
  PDF. Manifest schema unchanged.
- **Commit:** initial Phase 5.5 commit (this turn).

## Conventions for future entries

Each new finding gets a heading + the four-line shape above (Surface /
Symptom / Root cause / Fix). When the operator imports a real
statement and it surfaces a parser drift, add the entry here BEFORE
adjusting the manifest — manifest corrections are auditable.
