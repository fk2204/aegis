-- Populate tiers JSONB for tiered funders.
--
-- Migration 046 inserted the funders' top-level row (matched against
-- the loosest tier or primary product). For funders with operator-
-- curated tier ladders, this migration writes the full tier set into
-- the funders.tiers JSONB column so match_funder can pick the best
-- tier for a given merchant profile.
--
-- Hand-transcribed from Filip's internal MCA Funder Manual §2 and §6,
-- not auto-parsed — the tier tables are dense and parser-fragile, so
-- the operator-real values live here verbatim. If the manual updates,
-- write a follow-up migration (or use /ui/funders/{id} to edit).
--
-- Tier model (see src/aegis/funders/models.py:FunderTier):
--   name (str)
--   buy_rate_low / buy_rate_high (Decimal)
--   min_months_in_business / min_credit_score / max_positions (int)
--   min_monthly_revenue / max_advance (Money)
--   max_holdback (Decimal fraction, e.g. 0.15 for 15%)
--
-- Idempotent: re-running OVERWRITES the tiers JSONB. If operator has
-- edited via /ui/funders/{id} since the last apply, those edits will
-- be replaced. DELETE this migration row from schema_migrations + the
-- runner will re-apply (clobber).

-- ============================================================
-- Logic Advance — 4-tier ladder (§2 of manual)
-- ============================================================
UPDATE funders SET tiers = '[
  {
    "name": "Elite",
    "buy_rate_low": "1.25",
    "buy_rate_high": "1.25",
    "min_months_in_business": 60,
    "min_credit_score": 700,
    "min_monthly_revenue": "100000.00",
    "max_positions": 1,
    "max_advance": "1500000.00",
    "max_holdback": "0.15"
  },
  {
    "name": "Premium",
    "buy_rate_low": "1.28",
    "buy_rate_high": "1.28",
    "min_months_in_business": 48,
    "min_credit_score": 650,
    "min_monthly_revenue": "50000.00",
    "max_positions": 2,
    "max_advance": "1200000.00",
    "max_holdback": "0.25"
  },
  {
    "name": "Standard",
    "buy_rate_low": "1.30",
    "buy_rate_high": "1.30",
    "min_months_in_business": 12,
    "min_credit_score": 550,
    "min_monthly_revenue": "25000.00",
    "max_positions": 3,
    "max_advance": "1000000.00",
    "max_holdback": "0.45"
  },
  {
    "name": "High-Risk",
    "buy_rate_low": "1.37",
    "buy_rate_high": "1.37",
    "min_months_in_business": 6,
    "min_credit_score": 500,
    "min_monthly_revenue": "25000.00",
    "max_positions": 5,
    "max_advance": "1000000.00",
    "max_holdback": "0.50"
  }
]'::jsonb
WHERE name = 'Logic Advance';

-- ============================================================
-- United Capital Source — 7-product matrix (§6 of manual)
-- Each "tier" here is a product line. Operator picks the product
-- per deal; the tier-aware matcher surfaces eligible products.
-- ============================================================
UPDATE funders SET tiers = '[
  {
    "name": "Merchant Cash Advance",
    "min_months_in_business": 12,
    "min_credit_score": 575,
    "min_monthly_revenue": "25000.00",
    "max_positions": 2,
    "max_advance": "5000000.00"
  },
  {
    "name": "Business Term Loan",
    "min_months_in_business": 12,
    "min_credit_score": 575,
    "min_monthly_revenue": "25000.00",
    "max_positions": 2,
    "max_advance": "10000000.00"
  },
  {
    "name": "Business Line of Credit",
    "min_months_in_business": 12,
    "min_credit_score": 575,
    "min_monthly_revenue": "25000.00",
    "max_positions": 2,
    "max_advance": "1000000.00"
  },
  {
    "name": "Equipment Financing",
    "min_months_in_business": 3,
    "min_credit_score": 575,
    "min_monthly_revenue": "25000.00",
    "max_advance": "10000000.00"
  },
  {
    "name": "Receivables Factoring",
    "min_months_in_business": 12,
    "min_credit_score": 575,
    "min_monthly_revenue": "25000.00",
    "max_positions": 2,
    "max_advance": "25000000.00"
  },
  {
    "name": "SBA Loan",
    "min_credit_score": 650,
    "max_advance": "10000000.00"
  },
  {
    "name": "Home Equity LOC",
    "min_credit_score": 600,
    "max_advance": "750000.00"
  }
]'::jsonb
WHERE name = 'United Capital Source';
