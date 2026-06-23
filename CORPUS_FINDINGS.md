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

## 2026-06-17 — Legacy doc recovery campaign (LOAD LIFT + TMF)

Multi-day cleanup of the manual_review / pending backlog left by the
pre-mig-060 ingest era. Driven by two specific merchants the operator
flagged: LOAD LIFT ENTERPRISE LLC (4 TD Bank statements) and TMF
TRANSPORT INC (3 Chase + 4 misclassified attachments). The campaign
yielded one prod-shipping pipeline feature (vision third-pass
fallback), four script flags, and the five findings below. The
parser-tooling commits are in the `main` log between `33825d0` and
`963f63e`.

### Finding — Bedrock drops the page-1 period block on text input for Chase + TD layouts

- **Surface:** every TD Bank Convenience Checking statement (LOAD LIFT
  TD January/April/May, 3 of 3) AND every Chase Business Complete
  Checking statement (TMF 20260227, 20260529, list (16), list (17), 4
  of 4) submitted through `extract_statement` returned
  `summary.period_start=None` / `period_end=None`, raising a Pydantic
  `ValidationError` chained into `ExtractionError`. The period text IS
  in the PDF — `pymupdf.get_text("text")` reads it correctly — but the
  text the LLM ingests via the Bedrock document block does not surface
  it.
- **Symptom:** `_run_pipeline_with_retry`'s text-with-hint second pass
  returned a byte-identical null-period response. Hint tuning (commit
  `33825d0` initial seed, commit `a3a1f1e` corrected layouts, commit
  `b272a7f` prompt worked-examples + non-bail rule) recovered the TD
  statements but not the Chase ones — Chase docs kept landing in
  manual_review with the same error.
- **Root cause:** Bedrock's text channel on these specific PDFs drops
  the period block silently. The PDFs use CID-encoded fonts (Chase
  emits its own font subset; TD's iLovePDF re-saves re-embed too) — the
  text Bedrock receives is correct character-wise but spatially the
  period block sits between a logo, a customer-service box, and a
  blank `Account Number:` row and the model fails to bind it.
- **Fix:** ship a vision third-pass fallback in `run_pipeline`
  (commit `19e4776`). New kwarg `vision_fallback_on_extraction_error`
  catches `ExtractionError` from `extract_statement` and re-runs the
  same PDF through `extract_statement_via_vision` (existing function;
  rasterizes via pymupdf at 200 DPI). Wired into
  `scripts/recover_legacy_docs.py`'s
  `_run_pipeline_with_retry` so the cascade is now text → text+hint →
  vision. Vision is opt-in (off by default) because tokens are
  ~5-8× text and capped by `MAX_OCR_PAGES=20`; the webhook + worker
  callers stay on text-only.

### Finding — Funding-application form uploads name-collide with bank statements via merchant-slug filenames

- **Surface:** LOAD LIFT had 4 copies of
  `load-lift-enterprise-llc_2026-06-08.pdf` on the Close lead, all in
  `parse_status='pending'`. Initial inspection assumed bank statements;
  they were the Commera Funding Application PDF form filled by the
  borrower.
- **Symptom:** the regular ingest path tried to parse them as
  statements, generating spurious `summary.period_start=None`
  extraction errors that polluted the recovery CSV and burned Bedrock
  tokens. The existing `application` deny-list term did NOT catch the
  filenames — Close's form pipeline emits files named
  `<merchant-slug>_<YYYY-MM-DD>.pdf`, no "application" substring.
- **Root cause:** the merchant-slug filename convention bypasses every
  generic non-statement deny term (driver / license / contract /
  application / voided / etc.).
- **Fix:** added `load-lift-enterprise-llc` as a deny-list substring
  in `src/aegis/close/field_map.py::NON_STATEMENT_FILENAME_TERMS`
  (commit `71247f4`). Narrow per the deny-list rule ("each term
  surfaces a concrete non-statement filename"). Future merchant
  Funding Application uploads will need the same treatment until the
  Close form pipeline starts emitting a content-type-aware filename
  or AEGIS gains a content-shape pre-filter (out of scope for this
  campaign).

### Finding — TD Convenience Checking prints `Electronic Payments` and `Service Charges` as separate ACCOUNT SUMMARY rows; Bedrock extracts only the former as `withdrawal_total`

- **Surface:** LOAD LIFT's 3 TD Bank docs that DID clear extraction
  via the new vision fallback (or that the corrected hints recovered
  on text) landed in manual_review with
  `[MATH] reconciliation_failed_withdrawal_total: listed N vs printed
  N-15.00` — off by exactly $15.00 every month. Confirmed on
  `8a8fa92a` (Jan: listed 3164.04 / printed 3149.04), `3630bbab`
  (Apr: 1813.65 / 1797.65, off by $16 because April carried a $1
  duplicate), `e0ee9129` (May: 4164.04 / 4149.04, off by $15).
- **Symptom:** `validate_extraction` fails the reconciliation gate;
  doc routed to `manual_review` AFTER extraction, not before.
- **Root cause:** TD's first-page ACCOUNT SUMMARY block lists FIVE
  category lines: `Beginning Balance`, `Electronic Deposits`,
  `Electronic Payments`, `Service Charges` ($15 / month flat MSF),
  `Ending Balance`. There is no consolidated "Total Withdrawals" row.
  Bedrock extracts the `Electronic Payments` figure into
  `summary.withdrawal_total` because that's the closest-named single
  field, but the transaction stream includes BOTH the Electronic
  Payments rows AND the $15 Service Charge row. Sum of extracted
  withdrawals = Electronic Payments + Service Charges = printed
  withdrawal_total + $15.
- **Fix:** NOT IN THIS CAMPAIGN. The fix is a per-bank summary-coercion
  rule that knows TD's withdrawal_total = Electronic Payments +
  Service Charges, OR a prompt addition that instructs Bedrock to sum
  TD's split categories into the JSON `withdrawal_total`. Both are
  decision-boundary-adjacent (could change which TD docs land
  `proceed` vs `manual_review`) and need shadow-mode validation per
  CLAUDE.md's scoring-discipline rule. Filed for follow-up; the 3 TD
  docs stay in manual_review until then.

### Finding — Chase Business Complete Checking row-count over-extraction

- **Surface:** TMF's `463ba348` (20260227 Chase statement) cleared
  extraction with non-null period dates but failed with
  `[MATH] extraction_row_count_mismatch: listed 81 vs printed 64
  (tolerance 3)`.
- **Symptom:** Bedrock extracted 17 MORE transaction rows than the
  printed transaction count from the CHECKING SUMMARY block.
- **Root cause:** Chase Business Complete Checking statements embed
  `*start*deposits and additions` / `*end*deposits and additions`
  section-delimiter strings in the rendered text. The corrected
  prompt (`b272a7f`) tells the LLM to ignore these, but Bedrock is
  still treating some of them as transactions — OR the CHECKS PAID
  subsection (which prints rows in a `CHECK NO. / DATE / AMOUNT`
  shape distinct from the main `DATE / DESCRIPTION / AMOUNT` table)
  is being double-counted because its rows look transaction-shaped
  AND are referenced from the summary block.
- **Fix:** NOT IN THIS CAMPAIGN. Needs a CORPUS_REAL_LLM=1 reproducer
  against a sanitized fixture of this Chase layout to isolate which
  rows Bedrock is over-counting, then either tighten the prompt's
  `Checks Paid` discrimination or post-process the transaction list
  in `extract.py` to drop delimiter-shaped synthetics. Filed for
  follow-up; `463ba348` stays in manual_review until then.

### Finding — Lili Bank docs carry a phantom `storage_path` with no matching `pdf_store` blob

- **Surface:** the 2026-06-17 `--vision-retry` run encountered four
  Lili Monthly Statement docs (`c06000b6`, `503f3a9d`, `c2c95368`,
  `fffe63a3`) whose `documents.storage_path` was populated but
  `pdf_store.fetch_plaintext(doc_id)` raised
  `PdfStoreNotFoundError`. The selector treated them as
  vision-retryable (they pass the storage-path predicate), but the
  fetch immediately 404s.
- **Symptom:** the `--vision-retry` candidate list inflates with docs
  that can't actually be retried, AND the regular
  `--backfill-sha-matches` Close-fetch path skips them on a phantom
  "already-sealed" check (`pdf_store.fetch_plaintext` raising
  NotFound is the script's signal for "needs backfill"; but the
  populated `storage_path` column on the documents row blocks them
  from the unsealed-doc branches of other tooling).
- **Root cause:** unknown — pre-mig-060 legacy ingest path appears to
  have set `documents.storage_path` independently of the pdf_store
  seal in some cases (one hypothesis: an early arq worker wrote
  `storage_path` to the Supabase Storage object path BEFORE the
  chunk-B AES-GCM seal landed, then the Storage path was wiped during
  the chunk-A → chunk-B migration without clearing the column). The
  contract — `storage_path NOT NULL` IFF pdf_store has the row — was
  violated by whatever wrote that column historically. Forward path
  prevents recurrence; this finding is about cleaning the historical
  pile.
- **Fix:** added `--fix-phantom-storage` to
  `scripts/recover_legacy_docs.py` (commit `ae9c853`). Scans every
  `documents.storage_path NOT NULL`, probes `pdf_store.fetch_plaintext`,
  NULLs the column on any 404, and writes a
  `document.storage_path_nulled` audit row per repair. After this
  sweep, the affected docs naturally fall back into the
  `--backfill-sha-matches` Close re-fetch path on a later recovery
  run. Dry-run by default; bypasses Close traversal.

## 2026-06-23 — Image-only bank layouts (vision-only, no extractable text)

Three banks encountered during the June 2026 bank-coverage sweep whose
statements are entirely image-based (0 extractable characters on all
pages examined). Text-extraction hints cannot help these — they require
vision-mode parsing. Flagged here so future sessions don't attempt
text-hint seeding.

| Bank | Statements examined | Notes |
|---|---|---|
| Arthur State Bank | 4 | All 0 chars, vision-only |
| PNC Bank | 3 | All 0 chars, vision-only |
| The Bank of Bennington | 3 | All 0 chars, vision-only |

## Conventions for future entries

Each new finding gets a heading + the four-line shape above (Surface /
Symptom / Root cause / Fix). When the operator imports a real
statement and it surfaces a parser drift, add the entry here BEFORE
adjusting the manifest — manifest corrections are auditable.