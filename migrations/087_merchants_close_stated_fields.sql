-- Migration 087 — additive merchant columns for the Close FINANCIAL
-- block parser.
--
-- Close CRM stores application data inside the Lead ``description``
-- field as a structured ``FINANCIAL:`` block (verified against real
-- prod leads). The block carries operator-typed values that AEGIS
-- wants to surface on the dossier as "what the merchant told us",
-- DISTINCT from "what AEGIS measured from statements". The same data
-- powers two existing scoring detectors:
--
--   * ``detect_impossible_payment_load`` (severity 85) — reads
--     ``MerchantRow.stated_daily_payment``. Fires when stated daily
--     payment * 22 business days > true revenue * 1.5 (the real
--     "Vibration Guys" case: $125K/day on $120K/month revenue).
--   * ``detect_stated_vs_measured_revenue_divergence`` (severity 60)
--     — reads ``MerchantRow.monthly_revenue``. Fires when application
--     stated revenue diverges > 40% from bank-statement-measured.
--
-- ADDITIVE ONLY. Every column is nullable / defaults to NULL so a
-- pre-087 row reads as "merchant didn't tell us" rather than silently
-- passing the detectors' guard (both fire only when the stated field
-- is populated AND positive).
--
-- Columns + Close FINANCIAL block source mapping:
--
--   * ``monthly_revenue``         numeric(14,2) — "Monthly Gross Revenue"
--     Read by the stated-vs-measured divergence detector. Naming matches
--     the existing parser hard-dep (see patterns.py:2650).
--   * ``avg_monthly_cc_sales``    numeric(14,2) — "Avg Monthly CC Sales"
--   * ``stated_monthly_deposits`` integer — "Monthly Deposits" (count of
--     deposit transactions the merchant claims; not a money value).
--   * ``stated_mca_positions``    integer — "Existing MCA Positions"
--     Operator called this critical for "merchant stated N but bank
--     shows M" warnings. Pure count.
--   * ``stated_current_lenders``  text[] — "Current Lenders" (comma-
--     separated list in the Close payload; parser splits into array).
--   * ``stated_mca_balance``      numeric(14,2) — "Existing MCA Balance"
--   * ``stated_daily_payment``    numeric(14,2) — "Daily/Weekly Payment"
--     Read by the impossible-payment-load detector. Naming matches the
--     existing parser hard-dep (see patterns.py:2614).
--   * ``stated_bank``             text — "Bank"
--   * ``use_of_funds``            text — "Use of Funds"
--
-- Idempotent: ``ADD COLUMN IF NOT EXISTS`` so a partial re-apply or
-- bootstrap-probe-then-replay leaves the schema in the same end state.

BEGIN;

ALTER TABLE merchants
  ADD COLUMN IF NOT EXISTS monthly_revenue         numeric(14, 2),
  ADD COLUMN IF NOT EXISTS avg_monthly_cc_sales    numeric(14, 2),
  ADD COLUMN IF NOT EXISTS stated_monthly_deposits integer,
  ADD COLUMN IF NOT EXISTS stated_mca_positions    integer,
  ADD COLUMN IF NOT EXISTS stated_current_lenders  text[] NOT NULL DEFAULT ARRAY[]::text[],
  ADD COLUMN IF NOT EXISTS stated_mca_balance      numeric(14, 2),
  ADD COLUMN IF NOT EXISTS stated_daily_payment    numeric(14, 2),
  ADD COLUMN IF NOT EXISTS stated_bank             text,
  ADD COLUMN IF NOT EXISTS use_of_funds            text;

COMMENT ON COLUMN merchants.monthly_revenue IS
  'Application-stated monthly gross revenue parsed from the Close Lead '
  'description FINANCIAL block. Drives the stated-vs-measured revenue '
  'divergence detector (severity 60). NULL when the merchant did not '
  'supply this field.';
COMMENT ON COLUMN merchants.avg_monthly_cc_sales IS
  'Application-stated average monthly credit-card sales parsed from the '
  'Close Lead description FINANCIAL block. Surfaces on the dossier.';
COMMENT ON COLUMN merchants.stated_monthly_deposits IS
  'Application-stated count of monthly deposits parsed from the Close '
  'Lead description FINANCIAL block. Integer count, not a money value.';
COMMENT ON COLUMN merchants.stated_mca_positions IS
  'Application-stated count of existing MCA positions parsed from the '
  'Close Lead description FINANCIAL block. Foundation for the '
  '"merchant stated N but bank shows M" stacking warning.';
COMMENT ON COLUMN merchants.stated_current_lenders IS
  'Application-stated list of current MCA lender names parsed from the '
  'Close Lead description FINANCIAL block. Empty array when the merchant '
  'did not supply this field or supplied an empty value.';
COMMENT ON COLUMN merchants.stated_mca_balance IS
  'Application-stated outstanding MCA balance parsed from the Close '
  'Lead description FINANCIAL block.';
COMMENT ON COLUMN merchants.stated_daily_payment IS
  'Application-stated daily / weekly MCA payment parsed from the Close '
  'Lead description FINANCIAL block. Drives the impossible-payment-load '
  'detector (severity 85): when stated_daily_payment * 22 business days '
  'exceeds 150 percent of bank-measured monthly revenue the deal is '
  'mathematically insolvent under current obligations.';
COMMENT ON COLUMN merchants.stated_bank IS
  'Application-stated bank of record parsed from the Close Lead '
  'description FINANCIAL block. Cross-check against the statement-derived '
  'bank name in the parse pipeline.';
COMMENT ON COLUMN merchants.use_of_funds IS
  'Application-stated use of funds parsed from the Close Lead '
  'description FINANCIAL block. Free-text from the operator.';

COMMIT;
