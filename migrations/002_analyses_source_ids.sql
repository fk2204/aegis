-- Migration 002 — add `_source_ids` UUID[] columns to every aggregate
-- field on `analyses`.
--
-- Every aggregate metric MUST be traceable back to specific transaction
-- rows. The corresponding application-side Pydantic models
-- (parser/models.py) carry _SourcedMoney / _SourcedInt with `source_ids`;
-- this migration mirrors that shape into the database.
--
-- These columns are intentionally arrays of UUIDs (FK enforcement to
-- transactions.id is omitted because Postgres does not enforce FK on
-- array elements without a join table). Application-level invariants in
-- aegis/parser/aggregate.py guarantee every UUID corresponds to a row.

ALTER TABLE analyses
  ADD COLUMN IF NOT EXISTS avg_daily_balance_source_ids UUID[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS true_revenue_source_ids       UUID[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS num_nsf_source_ids            UUID[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS days_negative_source_ids      UUID[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS mca_daily_total_source_ids    UUID[] NOT NULL DEFAULT '{}';
