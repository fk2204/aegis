# Corpus findings

Per Phase 5.5 of `REWRITE_PLAN.md`, every fix surfaced by the corpus
suite is logged here with the scenario, root cause, fix, and the SHA
that resolved it.

## Corpus expansion (2026-05-10)

**Expanded corpus to 56 PDFs across 13 scenarios × 6 banks**,
satisfying Phase 5.5's "≥ 50 synthetic" gate.

| Scenario                          | Chase | BoA | Wells | CapOne | Regional | CU |
|-----------------------------------|:-----:|:---:|:-----:|:------:|:--------:|:--:|
| clean_profitable                  | ✓     | ✓   | ✓     | ✓      | ✓        | ✓  |
| nsf_heavy                         | ✓     | ✓   | ✓     | ✓      | ✓        | ✓  |
| mca_stacked                       | ✓     | ✓   | ✓     | ✓      | —        | —  |
| math_tampered                     | ✓     | ✓   | ✓     | ✓      | —        | —  |
| cash_heavy_retail                 | ✓     | ✓   | ✓     | ✓      | ✓        | —  |
| very_new_account                  | ✓     | ✓   | ✓     | —      | ✓        | —  |
| declining_revenue                 | ✓     | ✓   | ✓     | —      | —        | ✓  |
| customer_concentration            | ✓     | ✓   | ✓     | —      | —        | ✓  |
| kiting                            | ✓     | ✓   | ✓     | —      | ✓        | —  |
| preloan_spike                     | ✓     | ✓   | ✓     | —      | —        | ✓  |
| processor_holdback                | ✓     | ✓   | ✓     | —      | ✓        | —  |
| prompt_injection_in_description   | ✓     | ✓   | ✓     | —      | —        | —  |
| metadata_tampered                 | ✓     | ✓   | ✓     | ✓      | —        | —  |

**Metadata-tamper hook:** `metadata_tampered` recipes append a second
`%%EOF` marker to the PDF after reportlab renders. `parser/metadata.py`
counts `eof_markers=2`, raises `incremental_saves: 2 EOF markers`, and
the pipeline's `_decide` returns `parse_status="manual_review"`. The
underlying transactions reconcile correctly — only the metadata layer
fires the rejection.

**Real-LLM gate:** `prompt_injection_in_description` is a
no-op for manifest-feed mode. It only matters in
`CORPUS_REAL_LLM=1` mode, where it verifies Claude's classification
pass ignores the embedded `IGNORE PRIOR INSTRUCTIONS` text.

All 56 PDFs + the `real/` README check pass under `make test-fast`
(57 tests).

## Initial corpus build (2026-05-09)

**Bootstrapped corpus** of 6 PDFs across 4 scenarios × 2 banks
(superseded by the 2026-05-10 expansion above).

### Finding 2 — non-deterministic PDF bytes (reportlab timestamps)

- **Surface:** every regenerated PDF showed as `git diff` modified even
  with the same fixed seed.
- **Symptom:** `git status` flagged 6 of 6 existing PDFs as modified
  after a clean regen, even though manifest content was unchanged.
- **Root cause:** `reportlab.canvas.Canvas` embeds the current
  wall-clock time into `/CreationDate` and `/ModDate` by default,
  breaking REWRITE_PLAN's "fixed seed produces identical PDFs" gate.
- **Fix:** `scripts/generate_corpus.py` — pass `invariant=True` to
  `canvas.Canvas`. reportlab then omits the date entries and produces
  byte-identical output across runs with the same seed. Verified:
  two consecutive `--clean` runs produce identical SHA-256 hashes.
- **Commit:** corpus expansion commit (2026-05-10).

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
- **Commit:** initial Phase 5.5 commit (2026-05-09).

## Conventions for future entries

Each new finding gets a heading + the four-line shape above (Surface /
Symptom / Root cause / Fix). When the operator imports a real
statement and it surfaces a parser drift, add the entry here BEFORE
adjusting the manifest — manifest corrections are auditable.
