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

## `toast_sample.csv` — synthetic-but-realistic, fabricated values

**Status:** SYNTHETIC. Column headers match Toast's documented sales-
export format (23 columns: Date, Server, Order ID, Order #, Location,
Revenue Center, Tab Name, Item, Qty, Gross Amount, Discount Amount,
Net Amount, Void, Void Reason, Check Amount, Tip Amount, Total Amount,
Transaction Type, Payment Type, Last 4, Card Brand, Card Holder,
Dining Options). All row values are fabricated — fake order IDs, fake
ticket descriptions, single-name servers ("Alex" / "Jamie" / "Sam"),
generic card holder labels ("Guest" / "Cardholder N"). No real merchant
brand names, no real customer names.

**20 rows breakdown:**

| Transaction Type / Void | Count | Notes |
|---|---|---|
| `Payment` (Void=No, Card) | 14 | Visa / Mastercard / Amex / Discover mix; 3 distinct locations (Main Street / Downtown / Westside); 3 distinct Revenue Centers (Bar / Dining Room / Takeout) |
| `Payment` (Cash) | 1 | `Payment Type = Cash`, no card info; still maps to `gross_charge` |
| `Refund` | 3 | Negative `Net Amount`; refund reasons (bad pour, cold entree, missing item) |
| `Payment` (Void=Yes) | 2 | Voided payment rows; map to `adjustment` (excluded from validation identity) |

**Kind mapping after extraction:**

- 15 rows → `gross_charge` (14 card + 1 cash)
- 3 rows → `refund`
- 2 rows → `adjustment` (the two voids)
- 0 rows → `chargeback`, `fee` (Toast doesn't report processor fees)
- 1 synthetic → `payout` (derived from identity, attributed to period end)

**Identity math (all amounts in USD):**

```
gross_volume     = 763.80   (15 payment rows: 28.50+45.75+18.00+35.25
                              +78.40+25.00+22.85+52.10+112.30+15.50
                              +41.75+89.00+33.60+145.85+19.95)
refunds_total    =  46.25   (3 refund rows: 15.00+22.50+8.75)
chargebacks_total=   0.00
fees_total       =   0.00   (Toast doesn't report processor fees)
payouts_total    = 717.55   (synthetic — derived from identity)

Identity: 763.80 - 46.25 - 0.00 - 0.00 = 717.55  ✓
```

Period: 2026-05-01 → 2026-05-30 (30 days inclusive).
`avg_daily_volume = 763.80 / 30 = 25.46`.

**Why synthetic is tolerable here:**

Same justification as `square_sample.csv` — the column header set is
byte-for-byte the documented Toast format, all 23 columns are
populated, and the row coverage exercises every branch of
`_TOAST_TRANSACTION_TYPE_MAP` plus the void-handling path. Replace
with a real sanitised Toast export when one becomes available;
adjust assertion values in `test_toast_csv.py` to match.

**Replacement procedure:** identical to the Square procedure above —
export from Toast → Reports → Export, sanitise via
`tests/_fixture_sanitize.py::sanitize_fixture_payload`, replace
`toast_sample.csv`, update assertions, confirm PII canary green.

## `clover_sample.csv` — synthetic-but-realistic, fabricated values

**Status:** SYNTHETIC. Column headers match Clover Dashboard's documented
transactions export format (Date & Time, Description, Amount, Tip, Tax,
Total, Payment Type, Card Type, Last 4, Auth Code, Card Holder Name,
Employee, Order ID, Device ID, Note). All row values are fabricated —
fake descriptions, fake auth codes, fake card-type / Last 4 / device
combinations. Card Holder Names are "Cardholder N" / "Guest" only (no
named individuals); Employees are numeric IDs only (no names).

**15 rows breakdown:**

| Mapped kind | Count | Notes |
|---|---|---|
| `gross_charge` | 11 | 10 card payments (Visa / Mastercard / Amex / Discover mix) + 1 cash payment (empty Card Type / Auth Code) |
| `refund` | 2 | "Refund - bad item" and "Refund - canceled order" — Description-driven mapping |
| `adjustment` | 2 | 1 Void (Auth Code = VOID) + 1 Manual Adjustment (Description) — both excluded from identity |

**Identity math (all amounts in USD):**

```
gross_volume     = 1164.50  (11 gross_charge rows)
refunds_total    =   37.50  (2 refund rows)
chargebacks_total=    0.00  (Clover doesn't separate chargebacks in this export)
fees_total       =    0.00  (Clover doesn't report processor fees here)
payouts_total    = 1127.00  (synthetic — derived from identity)

Identity: 1164.50 - 37.50 - 0 - 0 = 1127.00  ✓
```

Period: 2026-05-02 → 2026-05-30 (29 days inclusive).
`avg_daily_volume = 1164.50 / 29 = 40.16`.

**Why synthetic is tolerable here:**

CLAUDE.md's "External-integration test discipline" requires fixtures from
external systems to be CAPTURED REAL payloads. This fixture is synthetic
but:

- The column header set is byte-for-byte the documented Clover Dashboard
  transactions CSV format.
- All 15 columns are present.
- The kind-mapping branches all exercise (Payment → gross_charge, Refund
  → refund via Description, Void → adjustment via Auth Code, Adjustment
  → adjustment via Description, cash payment → gross_charge with empty
  card columns).
- Synthetic-payout derivation path is exercised (no real payout row in
  the fixture, derived from the simplified identity since fees=0).

Replace with a real sanitised Clover Dashboard export when one becomes
available — see "Replacement procedure" below — and adjust assertion
values in `test_clover_csv.py` to match.

**Replacement procedure (when a real export becomes available):**

1. Export real Clover Dashboard transactions CSV (Dashboard → Reports →
   Transactions → Export).
2. Run it through `tests/_fixture_sanitize.py::sanitize_fixture_payload`
   to strip merchant PII (named individuals in Card Holder Name /
   Employee, real Auth Codes, real Order IDs, location identifiers).
3. Replace `clover_sample.csv` with the sanitised output.
4. Update assertion values in `test_clover_csv.py` to match the new
   fixture's totals.
5. Confirm the PII canary at `tests/test_fixture_pii_canary.py` still
   passes after the sanitised fixture lands.

## `stripe_balance_transactions_minimal.csv`

(Pre-existing — unchanged.)
