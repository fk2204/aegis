-- Migration 035 — seed production funder catalog (R0.1).
--
-- Background
-- ----------
-- The funders table (migration 003 + extensions 027/028/029) is empty
-- in production. With zero rows, `SupabaseFunderRepository.list_active()`
-- returns `[]`, the matcher iterates nothing, and every deal matches
-- zero funders. The deep audit (2026-06-08) flagged this as a launch
-- blocker (R0.1).
--
-- This migration seeds eight real MCA funder rows so the matching
-- engine has data to match against from day one. Names are real
-- (OnDeck, Rapid Finance, Forward Financing, Credibly, Kapitus,
-- Mulligan Funding, CFG Merchant Solutions, Pearl Capital).
--
-- IMPORTANT — values are industry-typical 2026 PLACEHOLDERS
-- ---------------------------------------------------------
-- The numeric criteria below (min monthly revenue, min TIB, advance
-- range, factor / holdback envelopes, max positions, state /
-- industry exclusions) are defensible industry-typical bands, NOT
-- each funder's actual published criteria. They exist so:
--   1. the matcher exercises the full code path,
--   2. the dashboard has rows to render,
--   3. operator review has a starting point instead of a blank page.
--
-- Before any live submission to any of these funders, the OPERATOR
-- MUST replace the criteria here with that funder's actual published
-- numbers (typically via /ui/funders/import re-extraction against
-- the funder's latest criteria PDF, or direct UPDATE on the row).
-- The structured re-extraction pipeline preserves operator_notes and
-- contact fields, so re-running it on these seed rows is safe.
--
-- Defensible-tier conventions used below
-- --------------------------------------
-- Tier 1 (OnDeck, Rapid Finance, Forward Financing):
--   $15K-$20K min monthly revenue, 6mo TIB,
--   $20K-$500K advance range, factor 1.18-1.35, holdback 8-15%,
--   max 1-2 positions, tighter state list (avoid SD/NV/ND/MT).
-- Tier 2 (Credibly, Kapitus, Mulligan Funding):
--   $10K-$15K min, 6mo TIB,
--   $5K-$400K advance range, factor 1.22-1.45, holdback 10-18%,
--   max 2-3 positions.
-- Tier 3 (CFG Merchant Solutions, Pearl Capital):
--   $8K-$10K min, 3-6mo TIB,
--   $5K-$250K advance range, factor 1.30-1.49, holdback 10-20%,
--   max 3-5 positions, more permissive; CA/NY excluded (disclosure
--   complexity) per the audit's "cautious tier 3" convention.
--
-- Excluded industries — common MCA exclusion list:
--   cannabis, firearms, adult-entertainment, multi-level-marketing,
--   debt-consolidation, payday-lending. Tier 1 funders also tend to
--   exclude trucking and used-auto-sales.
--
-- Excluded states — SD, NV, ND are commonly avoided industry-wide
--   (regulatory or low-volume). CA and NY are excluded by the
--   cautious tier-3 funders due to CFDL disclosure complexity.
--
-- Idempotency
-- -----------
-- INSERT ... ON CONFLICT (name) DO NOTHING — `name` has a UNIQUE
-- constraint (migration 003 line 11). Safe to re-run; existing rows
-- (including any operator-edited values) are NEVER overwritten by
-- this migration. To refresh a row, the operator should UPDATE it
-- explicitly or DELETE + re-run.
--
-- All rows are seeded with active=true so they appear in
-- list_active() immediately.

INSERT INTO funders (
  name,
  active,
  min_monthly_revenue,
  min_avg_daily_balance,
  min_credit_score,
  min_months_in_business,
  max_positions,
  accepts_stacking,
  min_advance,
  max_advance,
  max_nsf_tolerance,
  typical_factor_low,
  typical_factor_high,
  typical_holdback_low,
  typical_holdback_high,
  requires_coj,
  charges_merchant_advance_fees,
  excluded_industries,
  excluded_states,
  notes_residual
)
VALUES
  -- Tier 1 — premium funders, tighter criteria, lower factor rates.
  (
    'OnDeck',
    true,
    20000.00,        -- min_monthly_revenue
    2500.00,         -- min_avg_daily_balance
    625,             -- min_credit_score
    12,              -- min_months_in_business
    1,               -- max_positions (first-position only by default)
    false,           -- accepts_stacking
    20000.00,        -- min_advance
    500000.00,       -- max_advance
    3,               -- max_nsf_tolerance
    1.180,           -- typical_factor_low
    1.300,           -- typical_factor_high
    0.0800,          -- typical_holdback_low
    0.1500,          -- typical_holdback_high
    false,           -- requires_coj
    false,           -- charges_merchant_advance_fees
    ARRAY[
      'cannabis',
      'firearms',
      'adult-entertainment',
      'multi-level-marketing',
      'debt-consolidation',
      'payday-lending',
      'trucking',
      'used-auto-sales'
    ]::TEXT[],
    ARRAY['SD','ND','NV','MT']::TEXT[],
    'Seed row — industry-typical placeholders. Replace with current OnDeck criteria before live submission.'
  ),
  (
    'Rapid Finance',
    true,
    15000.00,
    2000.00,
    600,
    6,
    1,
    false,
    15000.00,
    500000.00,
    4,
    1.220,
    1.330,
    0.0800,
    0.1500,
    false,
    false,
    ARRAY[
      'cannabis',
      'firearms',
      'adult-entertainment',
      'multi-level-marketing',
      'debt-consolidation',
      'payday-lending',
      'trucking'
    ]::TEXT[],
    ARRAY['SD','ND','NV']::TEXT[],
    'Seed row — industry-typical placeholders. Replace with current Rapid Finance criteria before live submission.'
  ),
  (
    'Forward Financing',
    true,
    15000.00,
    2000.00,
    550,
    6,
    2,
    true,
    20000.00,
    500000.00,
    5,
    1.250,
    1.350,
    0.1000,
    0.1500,
    false,
    false,
    ARRAY[
      'cannabis',
      'firearms',
      'adult-entertainment',
      'multi-level-marketing',
      'debt-consolidation',
      'payday-lending'
    ]::TEXT[],
    ARRAY['SD','ND','NV']::TEXT[],
    'Seed row — industry-typical placeholders. Replace with current Forward Financing criteria before live submission.'
  ),

  -- Tier 2 — broader access, mid-pack factor & holdback.
  (
    'Credibly',
    true,
    15000.00,
    1500.00,
    550,
    6,
    2,
    true,
    5000.00,
    400000.00,
    5,
    1.220,
    1.400,
    0.1000,
    0.1700,
    false,
    false,
    ARRAY[
      'cannabis',
      'firearms',
      'adult-entertainment',
      'multi-level-marketing',
      'debt-consolidation',
      'payday-lending'
    ]::TEXT[],
    ARRAY['SD','ND','NV']::TEXT[],
    'Seed row — industry-typical placeholders. Replace with current Credibly criteria before live submission.'
  ),
  (
    'Kapitus',
    true,
    12500.00,
    1500.00,
    550,
    6,
    3,
    true,
    10000.00,
    400000.00,
    6,
    1.250,
    1.420,
    0.1000,
    0.1800,
    false,
    false,
    ARRAY[
      'cannabis',
      'firearms',
      'adult-entertainment',
      'multi-level-marketing',
      'debt-consolidation',
      'payday-lending'
    ]::TEXT[],
    ARRAY['SD','ND','NV']::TEXT[],
    'Seed row — industry-typical placeholders. Replace with current Kapitus criteria before live submission.'
  ),
  (
    'Mulligan Funding',
    true,
    10000.00,
    1000.00,
    550,
    6,
    2,
    true,
    5000.00,
    350000.00,
    6,
    1.250,
    1.450,
    0.1200,
    0.1800,
    false,
    false,
    ARRAY[
      'cannabis',
      'firearms',
      'adult-entertainment',
      'multi-level-marketing',
      'debt-consolidation',
      'payday-lending'
    ]::TEXT[],
    ARRAY['SD','ND','NV']::TEXT[],
    'Seed row — industry-typical placeholders. Replace with current Mulligan Funding criteria before live submission.'
  ),

  -- Tier 3 — most permissive on TIB/credit, higher factor rates,
  -- exclude CA/NY for CFDL disclosure caution per audit convention.
  (
    'CFG Merchant Solutions',
    true,
    10000.00,
    1000.00,
    500,
    6,
    3,
    true,
    5000.00,
    250000.00,
    8,
    1.300,
    1.480,
    0.1200,
    0.2000,
    false,
    false,
    ARRAY[
      'cannabis',
      'firearms',
      'adult-entertainment',
      'multi-level-marketing',
      'debt-consolidation',
      'payday-lending'
    ]::TEXT[],
    ARRAY['CA','NY','SD','ND','NV']::TEXT[],
    'Seed row — industry-typical placeholders. Replace with current CFG Merchant Solutions criteria before live submission.'
  ),
  (
    'Pearl Capital',
    true,
    8000.00,
    750.00,
    500,
    3,
    5,
    true,
    5000.00,
    250000.00,
    10,
    1.300,
    1.490,
    0.1000,
    0.2000,
    false,
    false,
    ARRAY[
      'cannabis',
      'firearms',
      'adult-entertainment',
      'multi-level-marketing',
      'debt-consolidation',
      'payday-lending'
    ]::TEXT[],
    ARRAY['CA','NY','SD','ND','NV']::TEXT[],
    'Seed row — industry-typical placeholders. Replace with current Pearl Capital criteria before live submission.'
  )
ON CONFLICT (name) DO NOTHING;
