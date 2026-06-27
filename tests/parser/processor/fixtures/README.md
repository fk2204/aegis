# Processor parser fixtures

## `square_transactions_minimal.csv` — TODO: replace with real export

**Status:** synthetic / hand-written. Tests that consume it are marked
`pytest.mark.xfail(strict=False)` at the module level in
`tests/parser/processor/test_square_csv.py`.

**Why this matters:** AEGIS's `CLAUDE.md` "External-integration test
discipline" requires fixtures from external systems to be CAPTURED REAL
payloads, not invented. A green test against a hand-written fixture
proves only that the code matches the author's assumptions — only a
green test against a real captured payload proves the code matches
reality. The 2026-06-05 Close-attachment field-drop bug is the
reference incident (a synthetic fixture invented an `id` field that
real Close attachments don't have; the suite passed; production crashed
on the first real API call).

**Replacement steps (operator):**

1. Export a real Square Dashboard transactions CSV (Dashboard →
   Reports → Transactions → Export).
2. Run it through `tests/_fixture_sanitize.py::sanitize_fixture_payload`
   to strip merchant PII (named individuals in `Description`, location
   identifiers, real transaction IDs, etc.).
3. Replace `square_transactions_minimal.csv` with the sanitised output.
4. Update the assertion values in `test_square_csv.py` to match the new
   fixture's totals (the existing assertions are tied to the synthetic
   row math: gross 607.90, refunds 23.50, chargebacks 75.00, fees
   32.63, payouts 476.77).
5. Remove the `pytestmark = pytest.mark.xfail(...)` block at the top of
   `test_square_csv.py`.
6. Confirm the PII canary at `tests/test_fixture_pii_canary.py` still
   passes after the sanitised fixture lands.

Until those steps are done, treat passing Square CSV tests as
informational only — they do NOT prove the parser handles real Square
exports correctly.
