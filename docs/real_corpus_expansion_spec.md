# Real-statement corpus expansion — operator spec

Phase 11 task #6 (real-corpus half). The synthetic corpus exercises
the parser against 56 manufactured statements + 3 image-only
mixed-modality PDFs. Real statements are needed to catch the
production-layout drift that synthetic generators can't simulate.

This document is the deliverable spec the operator follows to add
real-corpus coverage. Claude Code is NOT allowed to author
manifests for real statements (per ``.claude/rules/testing.md``);
that would be grading the parser against its own output.

---

## Scope

Add **2-3 statements per fintech bank** to
``tests/fixtures/corpus/real/``, with operator-validated manifests
matching the schema in
``scripts/generate_corpus.py``.

Target banks (the fintechs that dominate Commera's deal mix):

| Bank | Count | Notes |
|---|---|---|
| Mercury     | 2-3 | All-online business banking; PDF export from "Statements" tab. |
| Brex        | 2-3 | Card + cash management; PDF export from "Statements". |
| Bluevine    | 2-3 | Small-business checking; PDF export from "Account Activity". |
| Novo        | 2-3 | Small-business banking; PDF export from "Statements". |
| Relay       | 2-3 | Multi-account business banking; per-account PDFs. |
| Lili        | 2-3 | Freelancer/SMB; PDF export from "Statements". |
| Found       | 2-3 | 1099 tax-aware business banking; PDF export from settings. |

Total target: **14-21 real statements + 14-21 hand-authored manifests**.

---

## Sourcing

* Operator pulls statements from merchants who have ALREADY consented
  to AEGIS testing — typically deals already closed (the data is no
  longer underwriting-sensitive). Get written email confirmation from
  the merchant before exporting.
* Each PDF goes on the operator's local box only. **Never commit a
  real statement to git.** The ``tests/fixtures/corpus/real/`` .gitignore
  already excludes everything except the README + ``*.manifest.json``.
* Strip merchant PII from the operator's local copy is unnecessary —
  the parser runs end-to-end with PII to verify the masking path. The
  manifests committed to git contain ONLY aggregate numbers + the
  transaction shape, NEVER the merchant's name / EIN / address.

---

## Manifest authoring

For each PDF, write a paired ``*.manifest.json`` that documents what
the operator READS in the statement. Schema lives in
``tests/fixtures/corpus/real/README.md`` (line 28+). Hard rules:

1. **The manifest is ground truth**. If the parser disagrees with the
   manifest, the FAILING side is the parser unless the manifest is
   demonstrably wrong (in which case CORPUS_FINDINGS.md records the
   correction).
2. **No auto-generation**. The operator reads the statement and types
   the numbers. Tools that "extract a manifest from a PDF" defeat
   the test.
3. **Money tolerances are baked into the runner** (``±$1``); use the
   actual printed values from the statement summary.
4. **Counts must be exact**. NSF, MCA positions, transaction count.
5. **Hard-decline reasons** in the ``expected`` block must match
   exactly. If you expect ``[]``, write it.
6. **Recommendation** must be exact: ``approve|decline|refer``.

Minimal example for one Mercury statement::

```json
{
  "version": "1",
  "scenario": "real_mercury_apr_2026",
  "bank": "mercury",
  "seed": null,
  "summary": {
    "beginning_balance": "12345.67",
    "ending_balance":    "13456.78",
    "deposit_total":     "47300.00",
    "withdrawal_total":  "-45800.00",
    "period_start":      "2026-04-01",
    "period_end":        "2026-04-30",
    "printed_transaction_count": 87
  },
  "transactions": [
    {
      "posted_date": "2026-04-02",
      "description": "ACH DEPOSIT CUSTOMER PAYMENTS",
      "amount":      "1500.00",
      "running_balance": "13845.67",
      "source_page": 1,
      "source_line": 5,
      "category":    "ach_credit"
    }
  ],
  "expected": {
    "validation_passed": true,
    "recommendation":   "approve",
    "fraud_score":      {"max": 25}
  },
  "tolerances": {"money": "1.00", "fraud_score": 5}
}
```

The ``transactions`` array MAY contain just a representative subset
(say, the first 5 + any unusual rows) — the runner's manifest-feed
mode reads them for the test, but does not require every row.

---

## Verification workflow

1. Drop the PDF + manifest pair under ``tests/fixtures/corpus/real/``.
2. Locally: ``make test`` (CORPUS=1 is baked in).
3. The parser pipeline runs against the manifest in manifest-feed
   mode (default). For real-LLM verification (slower, costs money):
   ``CORPUS_REAL_LLM=1 make test`` against the real Bedrock.
4. On failure, inspect the diff. If the parser is wrong, file a
   ticket and DO NOT amend the manifest. If the manifest is wrong,
   amend it AND record the correction in ``CORPUS_FINDINGS.md``.

---

## Why this is deferred to the operator

The Claude Code agent that drafted Phase 11 cannot:

* Source real merchant statements (no access to merchant relationships).
* Author manifests from PDFs (testing.md forbids auto-generation).
* Verify the manifest matches what the operator's eye sees in the
  statement (manifest authorship is the test's source of truth).

The synthetic + image-only corpora cover algorithmic correctness;
the real corpus covers production-layout drift. Both are needed.

---

## Status (operator-tracking)

(Operator: tick off as banks land. Each row is one bank's 2-3
statements.)

- [ ] Mercury
- [ ] Brex
- [ ] Bluevine
- [ ] Novo
- [ ] Relay
- [ ] Lili
- [ ] Found

Last updated: 2026-05-19 (initial spec from Phase 11 ops branch).
