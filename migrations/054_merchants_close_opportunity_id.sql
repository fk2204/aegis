-- Migration 054 — close_opportunity_id integration prep.
--
-- Adds close_opportunity_id to merchants. The Close inbound webhook
-- (/webhooks/close) fires on opportunity status_id transitions to
-- "Docs In — Pre-UW" (see settings.close_docs_in_pre_uw_status_id);
-- the event payload's ``object_id`` IS the opportunity id. This
-- column persists the captured id so the outbound offer-sync
-- (push_offer_to_opportunity, scoring_v2/offer.py series) can PATCH
-- the right Close Opportunity custom fields (Suggested Max Advance,
-- Recommended Factor Rate, Recommended Holdback Pct, True Revenue,
-- Holdback Capacity, Existing MCA Debits Identified, Existing MCA
-- Daily Debits Total) without re-deriving the id at every sync call.
--
-- Idempotency: schema_migrations gates re-runs at the application
-- layer. The statements below are plain ALTER TABLE — a partial
-- re-application would error explicitly rather than silently
-- masking drift, which is the preferred fail mode.
--
-- Index design: partial UNIQUE on close_opportunity_id WHERE NOT NULL.
-- Multiple NULL values are allowed (legacy merchants without an
-- Opportunity linkage), but two merchants can't share the same
-- Close Opportunity. Mirrors migration 026's close_lead_id index
-- shape exactly.

ALTER TABLE merchants ADD COLUMN close_opportunity_id TEXT;

CREATE UNIQUE INDEX idx_merchants_close_opportunity_id
  ON merchants (close_opportunity_id)
  WHERE close_opportunity_id IS NOT NULL;
