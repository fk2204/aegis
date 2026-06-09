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

## 2026-06-09 — R4.7 regional-bank layout expansion

Added three new synthetic layouts to `scripts/generate_corpus.py` to close
audit finding H13 ("Brex / Mercury / community-CU layouts untested"). The
generator now emits three additional bank slugs alongside the legacy six:

| Slug                   | Display name                       | Idiom                                                                                       |
|------------------------|------------------------------------|---------------------------------------------------------------------------------------------|
| `brex_business`        | Brex Inc                           | Modern fintech. Dark banner, period summary block, single transaction table with `Running Balance` column, footer "Statement generated electronically — Brex Inc." |
| `mercury_business`     | Mercury                            | Minimalist sans-serif. Left-aligned account header (`Mercury` · `Acme Demo LLC` · `Account ····5512`), thin grey rule, transactions grouped by date with the date as a small subheader, period summary at the bottom of the last page |
| `community_cu_legacy`  | Members Community Credit Union     | Older dense format. Multi-column header (`Statement Date` / `Account Number` / `Customer ID` / `Page X of Y`), 7pt body font, deposits and withdrawals rendered as two separate sub-tables, closing-balance box at the bottom |

Recipes added: 5 scenarios per layout (15 new fixtures, seeds 70001-70005,
80001-80005, 90101-90105) — `clean_profitable`, `nsf_heavy`, one stress
scenario each (Brex: `mca_stacked` + `processor_holdback`; Mercury:
`mca_stacked` + `customer_concentration`; Community CU: `cash_heavy_retail`
+ `declining_revenue`), and a `math_tampered` to exercise the validator on
each layout. Total corpus is now **71 synthetic PDFs** (was 56).

**Generator surface:** `BankLayout` gained an optional `renderer` callable
that overrides the default `_render_pdf`. The three R4.7 layouts each ship
a dedicated `_render_<format>_statement` function; legacy layouts keep
the shared renderer. Dispatch happens in `_write_pair`:

```python
renderer = layout.renderer if layout.renderer is not None else _render_pdf
renderer(stmt, layout, pdf_path)
```

**Determinism:** verified across three consecutive `python -m
scripts.generate_corpus --clean` runs — SHA-256 hashes of all 71 PDFs
matched run-over-run. `canvas.Canvas(invariant=True)` is preserved on the
new renderers; metadata-tamper hook (`_maybe_append_eof`) reuses the
single-line append path so `math_tampered_*` and any future
`metadata_tampered_*` recipe behaves identically across layouts.

**Tests:** new `tests/parser/test_regional_bank_formats.py` (106 cases =
15 fixtures × 7 parametric checks + 1 coverage assertion). Covers:

1. Text layer is extractable via `pymupdf` (no accidental image-only drop).
2. Bank-identifying strings (`Brex Inc`, `Mercury`, `Members Community
   Credit Union`, plus layout-specific markers like `CLOSING BALANCE` /
   `Deposits & Credits`) are present in the rendered text.
3. Every manifest transaction amount appears in the text layer (catches
   row-dropping pagination bugs).
4. `source_page` and `source_line` are non-null on every transaction
   (AEGIS auditability rule).
5. Manifest-feed pipeline matches the manifest's expected status: 12
   clean / nsf / mca / processor / concentration / declining /
   cash-heavy fixtures hit `validation_passed=True`; 3 `math_tampered`
   fixtures correctly trip `reconciliation_failed`.
6. `true_revenue` stays within the $1 corpus tolerance of `deposit_total`
   and carries non-empty `source_ids` whenever non-zero.
7. Negative-case: layouts MUST NOT contain `Chase Business` / `Bank of
   America` / `Wells Fargo` in their text (guards against the dispatcher
   regressing to `_render_pdf` for the new slugs).

Existing `tests/test_corpus.py` discovery is `rglob("*.manifest.json")`, so
the 15 new fixtures are picked up by the legacy corpus runner too —
yielding 72 corpus items there (was 57). Both suites green.

**Parser regressions discovered:** none in the deterministic
manifest-feed pipeline. Real-LLM-mode validation (Bedrock `bank_name`
extraction from the new layouts) was NOT run — that's the `CORPUS_REAL_LLM=1`
gate and lives outside this commit. Operator follow-up: run the pre-deploy
real-LLM corpus pass and confirm Claude pulls the correct `bank_name`
("Brex Inc" / "Mercury" / "Members Community Credit Union") on at least
one fixture per layout. If any layout produces `bank_name=None` or a
generic string, file as a follow-up and tune the extraction prompt — do
NOT relax the test.

## Conventions for future entries

Each new finding gets a heading + the four-line shape above (Surface /
Symptom / Root cause / Fix). When the operator imports a real
statement and it surfaces a parser drift, add the entry here BEFORE
adjusting the manifest — manifest corrections are auditable.