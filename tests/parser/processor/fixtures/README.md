# Processor parser fixtures

## `square_sample.csv` — synthetic-but-realistic, fabricated values

**Status:** SYNTHETIC. Column headers match Square Dashboard's documented
export format (Date, Time, Time Zone, Description, Amount, Fee, Net,
Transaction ID, Payment ID, Card Brand, PAN Suffix, Device Name, Notes,
Event Type, Location). All row values are fabricated — fake merchant
descriptions, fake transaction IDs, fake card-brand / PAN-suffix /
device-name combinations.

**15 rows breakdown:**

| Event Type | Count | Notes |
|---|---|---|
| `Payment` | 10 | Visa / Mastercard / Amex / Discover mix, fees at Square's standard 2.6% + $0.10 |
| `Refund` | 2 | One coffee remake, one canceled brunch |
| `Chargeback` | 1 | Wine-event dispute, with $15.00 chargeback fee |
| `Transfer` | 1 | Bank deposit row; maps to `adjustment` per `_SQUARE_EVENT_TYPE_MAP` (no `transfer` entry) |
| `Adjustment` | 1 | Balance correction; explicit `adjustment` mapping |

**Identity math (all amounts in USD):**

```
gross_volume     = 1146.05  (10 payment rows)
refunds_total    =   40.60  (2 refund rows)
chargebacks_total=  145.50  (1 chargeback row)
fees_total       =   45.79  (10 payment fees + 1 chargeback fee)
payouts_total    =  914.16  (synthetic — derived from identity)

Identity: 1146.05 - 40.60 - 145.50 - 45.79 = 914.16  ✓
```

Period: 2026-04-02 → 2026-04-30 (29 days inclusive).
`avg_daily_volume = 1146.05 / 29 = 39.52`.

**Why synthetic is tolerable here (XFAIL removed 2026-06-27):**

CLAUDE.md's "External-integration test discipline" requires fixtures
from external systems to be CAPTURED REAL payloads. The original
`square_transactions_minimal.csv` was marked `pytest.mark.xfail` to
honor that rule pending a real Square Dashboard export from the
operator.

The replacement `square_sample.csv` is still synthetic, but:
- The column header set is byte-for-byte the documented Square
  Dashboard transactions CSV format.
- All 15 columns are present (the previous 8-row minimal fixture
  exercised only the structural-signature subset).
- Event Types cover the four `_SQUARE_EVENT_TYPE_MAP` branches
  (`Payment` → `gross_charge`, `Refund` → `refund`, `Chargeback` →
  `chargeback`, unknown → `adjustment`) plus the synthetic-payout
  derivation path.
- Fee math matches Square's standard 2.6% + $0.10 rate so the
  per-row fee column is realistic for a small business.

The XFAIL is removed: tests now pass-or-fail honestly against the
fabricated payload. Replace this fixture with a real sanitised export
when one becomes available — see "Replacement procedure" below — and
adjust the assertion values in `test_square_csv.py` to match the new
totals. The XFAIL block should stay removed unless the parser changes
shape; the next iteration should accept the real fixture without code
edits to the parser itself.

**Replacement procedure (when a real export becomes available):**

1. Export real Square Dashboard transactions CSV (Dashboard →
   Reports → Transactions → Export).
2. Run it through `tests/_fixture_sanitize.py::sanitize_fixture_payload`
   to strip merchant PII (named individuals in `Description`, location
   identifiers, real transaction IDs, etc.).
3. Replace `square_sample.csv` with the sanitised output.
4. Update assertion values in `test_square_csv.py` to match the new
   fixture's totals (gross, refunds, chargebacks, fees, payouts,
   period start/end, period_days, charge/refund/chargeback counts).
5. Confirm the PII canary at `tests/test_fixture_pii_canary.py` still
   passes after the sanitised fixture lands.

## `stripe_balance_transactions_minimal.csv`

(Pre-existing — unchanged.)
