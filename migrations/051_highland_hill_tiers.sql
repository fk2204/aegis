-- Populate tiers JSONB for Highland Hill Capital (§7 of manual).
--
-- Source: Filip's internal MCA Funder Manual §7 ("Commission Structure
-- (Rate Ladder)", lines 576-589). Migration 046 inserted the funder row
-- with the loosest tier values (factor 1.499); this migration writes the
-- full 5-rate ladder so match_funder can surface every offer the funder
-- publishes.
--
-- Operator guidance from the manual: "All offers go out at 1.499 unless
-- stated otherwise" — Tier 1 is the default. Tier 5 (1.369) is pending
-- eligibility per the manual; treated here as a valid tier so the matcher
-- can surface it as an option for the operator, with that caveat captured
-- in the tier name.
--
-- Tier model (see src/aegis/funders/models.py:FunderTier):
--   name (str)
--   buy_rate_low / buy_rate_high (Decimal) — factor rate from the ladder
--   min_months_in_business / min_credit_score / min_monthly_revenue /
--     max_positions / max_advance — inherited from top-level FunderRow
--     (TIB 12mo, FICO 500, $30K monthly revenue, 8 positions, $5M max);
--     left NULL here since these tiers differ only by rate/ISO points.
--
-- Note on ISO points: FunderTier has no `iso_points` field today. The ISO
-- commission structure (12 / 10 / 8 / 6 / 3 points) lives in the tier
-- name for operator visibility on /ui/funders/{id}. Promoting this to a
-- structured Decimal field is a follow-up if the matcher ever needs to
-- compute commission economics.
--
-- Idempotent: re-running OVERWRITES the tiers JSONB. If operator has
-- edited via /ui/funders/{id} since the last apply, those edits will be
-- replaced. Same convention as migration 049.

UPDATE funders SET tiers = '[
  {
    "name": "Tier 1 — 1.499 (12 ISO pts, default)",
    "buy_rate_low": "1.499",
    "buy_rate_high": "1.499"
  },
  {
    "name": "Tier 2 — 1.459 (10 ISO pts)",
    "buy_rate_low": "1.459",
    "buy_rate_high": "1.459"
  },
  {
    "name": "Tier 3 — 1.429 (8 ISO pts)",
    "buy_rate_low": "1.429",
    "buy_rate_high": "1.429"
  },
  {
    "name": "Tier 4 — 1.399 (6 ISO pts)",
    "buy_rate_low": "1.399",
    "buy_rate_high": "1.399"
  },
  {
    "name": "Tier 5 — 1.369 (3 ISO pts, pending eligibility)",
    "buy_rate_low": "1.369",
    "buy_rate_high": "1.369"
  }
]'::jsonb
WHERE name = 'Highland Hill Capital';
