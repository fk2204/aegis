# Corpus findings

Every fix surfaced by the corpus suite is logged here with the scenario,
root cause, fix, and the SHA that resolved it. (Originally introduced as
Phase 5.5 of the now-archived `docs/archive/REWRITE_PLAN.md`.)

## Corpus expansion (2026-05-10)

**Expanded corpus to 56 PDFs across 13 scenarios × 6 banks**,
satisfying the original "≥ 50 synthetic" gate.

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
  breaking the "fixed seed produces identical PDFs" gate.
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
- **Commit:** initial corpus commit (2026-05-09).

## Non-statement PDF baseline (2026-05-28)

**Question:** when the parser is given a NON-statement PDF — driver's
license, voided check, vendor invoice, application form — what
happens? Could a non-statement produce a passing all-zeros analysis
(the "C-1 silent failure" case identified during the
fix/close-note-attachments design)?

**Method:** five reportlab-generated fixtures fed through the real
extraction + validation pipeline against Claude Sonnet 4.6 via
Bedrock. Test at
`tests/parser/test_non_statement_baseline.py`, gated behind
`RUN_BEDROCK_TESTS=true` so CI skips. Run on the Hetzner box
(prod Bedrock credentials) — total spend ~$0.25.

**Result: 5/5 cleanly rejected. No C-1 case observed.**

| Fixture                | Outcome              | Caught by                              |
|------------------------|----------------------|----------------------------------------|
| drivers_license        | ExtractionError      | Pydantic schema (null Decimal fields)  |
| voided_check           | ExtractionError      | Pydantic schema (null Decimal fields)  |
| vendor_invoice         | ExtractionError      | Pydantic schema (null Decimal fields)  |
| application_form       | ExtractionError      | Pydantic schema (null Decimal fields)  |
| off_period_statement   | ValidationFailed     | `invalid_period: 2 days outside 14-50` |

**Why it works:** Claude 4.6 is conservative on the four required
Decimal summary fields (`beginning_balance`, `ending_balance`,
`deposit_total`, `withdrawal_total`). Given a non-statement, it
returns the schema-shaped envelope but emits `null` for those
fields rather than fabricating. The `Money` type rejects null →
4 ValidationErrors → ExtractionError. The off-period case is the
"adversarial" shape — Claude DOES happily extract a real-looking
statement when one is presented, regardless of its period — but the
deterministic period gate (14–50 days) catches that downstream.

**No guard added.** The C-1 silent-failure path is not reachable
against the five fixture shapes tested. The
`test_non_statement_baseline` test asserts
`parse_status != 'passes_validation'` so a future shift in Claude's
behavior (e.g. starting to fabricate zeros) would surface as a test
failure; the operator can decide then whether to add the guard.

**What this DOESN'T cover:** the test fixtures are synthetic and
reportlab-generated. Real-world non-statements vary widely (scanned
docs, exported screenshots, marketing PDFs, etc.). The Y1 pin gate
in `aegis.workers.process_close_attachments` is still the primary
defense — the operator pins what they confirm is a statement.
Content-type filter + parser robustness are the second + third
layers. This baseline measures only the third.

---

## Conventions for future entries

Each new finding gets a heading + the four-line shape above (Surface /
Symptom / Root cause / Fix). When the operator imports a real
statement and it surfaces a parser drift, add the entry here BEFORE
adjusting the manifest — manifest corrections are auditable.