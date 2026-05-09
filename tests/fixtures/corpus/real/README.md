# Real-statement corpus

Operator-supplied PDFs live here, paired with **hand-written**
manifests. The corpus runner reads both and asserts the parser +
scorer + disclosure pipeline produces numbers that match.

## Hard rules

1. **Real PDFs are not committed.** `.gitignore` in this directory
   excludes everything except this README and `*.manifest.json`. PDFs
   carry merchant PII; they live on the operator's box only.
2. **Never auto-generate manifests for real statements.** Doing so
   would mean grading the parser against its own output, which defeats
   the test. The operator writes the manifest from reading the PDF.
3. **Manifest schema matches the synthetic corpus** (see
   `scripts/generate_corpus.py`). The runner doesn't distinguish
   synthetic from real — it just walks every `*.manifest.json` it
   finds under `tests/fixtures/corpus/`.
4. **Manifest corrections are auditable.** If a real-statement test
   fails because the operator miscounted in the manifest, fix the
   manifest AND log the correction in `CORPUS_FINDINGS.md`. Don't
   silently retrofit.

## Authoring a manifest

Minimal example for a real statement:

```json
{
  "version": "1",
  "scenario": "real_chase_apr_2026",
  "bank": "chase_business",
  "seed": null,
  "summary": {
    "beginning_balance": "12000.00",
    "ending_balance": "13500.00",
    "deposit_total": "47300.00",
    "withdrawal_total": "-45800.00",
    "period_start": "2026-04-01",
    "period_end": "2026-04-30",
    "printed_transaction_count": 87
  },
  "transactions": [
    {
      "posted_date": "2026-04-02",
      "description": "ACH DEPOSIT CUSTOMER PAYMENTS",
      "amount": "1500.00",
      "running_balance": "13500.00",
      "source_page": 1,
      "source_line": 5,
      "category": "ach_credit"
    }
  ],
  "expected": {
    "validation_passed": true,
    "recommendation": "approve",
    "fraud_score": {"max": 25}
  },
  "tolerances": {"money": "1.00", "fraud_score": 5}
}
```

`source_page` and `source_line` are 1-indexed inside the source PDF.
The runner verifies the parser preserves these so the dashboard
drill-down can answer "where did this number come from?"
