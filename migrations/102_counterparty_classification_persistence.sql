-- Migration 102 — Persist counterparty classification on transactions.
--
-- Root-cause fix for the "$471K all-unknown" Turnbull case + every
-- similar merchant going forward. The 2026-06-30 audit established
-- that `aegis.counterparty.classify` produces a
-- `CounterpartyClassification` for every transaction at scoring time
-- (dictionary lookup + bundle matcher; LLM-assist deferred) but the
-- result was never persisted: every scoring call recomputed from
-- scratch, the operator review surface had no stable target to
-- override, and the dossier ledger couldn't show per-transaction
-- counterparty without a recompute. Concretely: there was no
-- `counterparty_classifications` table, no `counterparty_*` column
-- on `transactions`.
--
-- This migration adds the columns the scorer writes to and the
-- operator override endpoint reads. The application-layer changes
-- (write at scoring time, read-on-aggregate, override endpoint,
-- dossier surface) land in the same PR but in subsequent commits.
--
-- RLS posture
-- -----------
-- `transactions` already has RLS enabled (migration 011: blanket
-- ENABLE ROW LEVEL SECURITY on every public-schema table, no
-- policies, service_role bypass). New columns inherit that posture
-- automatically — no policy work needed here.
--
-- Operator FK pattern
-- --------------------
-- AEGIS does NOT use Supabase `auth.users` for operator identity.
-- Verified: zero migrations reference `auth.users(id)`. Operator
-- identity is the Cloudflare Access SSO email captured as a TEXT
-- column (e.g. `audit_log.actor TEXT NOT NULL` at migration 000,
-- `overrides.operator_id TEXT NOT NULL` at migration 017,
-- `merchant_notes.actor TEXT NOT NULL` at migration 066). Following
-- that pattern for `counterparty_override_by TEXT`.
--
-- CounterpartyClass enum
-- ----------------------
-- The 8 values come from `aegis.counterparty.models.CounterpartyClass`
-- (Literal). CHECK constraint mirrors the Pydantic Literal so a bad
-- write fails at the DB layer AND at the application layer. The list
-- intentionally allows NULL — pre-migration rows have no classification
-- and get backfilled at next scoring run, NOT by this migration (the
-- scorer is the authoritative writer; backfill via re-score is the
-- right pattern).
--
-- Idempotency
-- -----------
-- All ALTERs use `IF NOT EXISTS`. CHECK constraints follow the
-- migration-076 pattern (DROP IF EXISTS first, then ADD) so re-runs
-- don't trip DuplicateObject.

ALTER TABLE public.transactions
  ADD COLUMN IF NOT EXISTS counterparty_class       TEXT,
  ADD COLUMN IF NOT EXISTS counterparty_confidence  INTEGER,
  ADD COLUMN IF NOT EXISTS counterparty_reason      TEXT,
  ADD COLUMN IF NOT EXISTS counterparty_overridden  BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS counterparty_override_by TEXT,
  ADD COLUMN IF NOT EXISTS counterparty_override_at TIMESTAMPTZ;

-- ─────────────────────────────────────────────────────────────────────
-- CHECK: counterparty_class must be in the CounterpartyClass Literal
-- or NULL (pre-migration / un-scored row).
-- ─────────────────────────────────────────────────────────────────────

ALTER TABLE public.transactions
  DROP CONSTRAINT IF EXISTS transactions_counterparty_class_check;

ALTER TABLE public.transactions
  ADD CONSTRAINT transactions_counterparty_class_check
  CHECK (counterparty_class IS NULL OR counterparty_class IN (
    'processor',
    'own_account',
    'own_account_unconfirmed',
    'card_paydown',
    'international_client',
    'end_customer',
    'book_wire_unresolved',
    'unknown'
  ));

-- ─────────────────────────────────────────────────────────────────────
-- CHECK: confidence is 0..100 (matches
-- `CounterpartyClassification.confidence: int = Field(ge=0, le=100)`).
-- INTEGER, not NUMERIC — the model is integer, not decimal.
-- ─────────────────────────────────────────────────────────────────────

ALTER TABLE public.transactions
  DROP CONSTRAINT IF EXISTS transactions_counterparty_confidence_check;

ALTER TABLE public.transactions
  ADD CONSTRAINT transactions_counterparty_confidence_check
  CHECK (counterparty_confidence IS NULL OR
         (counterparty_confidence >= 0 AND counterparty_confidence <= 100));

-- ─────────────────────────────────────────────────────────────────────
-- CHECK: override integrity. Either NO override (default state) OR a
-- complete override record (actor + timestamp both present). No
-- half-overrides — a write that sets `counterparty_overridden=TRUE`
-- without an actor would silently lose audit context.
-- ─────────────────────────────────────────────────────────────────────

ALTER TABLE public.transactions
  DROP CONSTRAINT IF EXISTS transactions_counterparty_override_integrity;

ALTER TABLE public.transactions
  ADD CONSTRAINT transactions_counterparty_override_integrity
  CHECK (
    (counterparty_overridden = FALSE
       AND counterparty_override_by IS NULL
       AND counterparty_override_at IS NULL)
    OR
    (counterparty_overridden = TRUE
       AND counterparty_override_by IS NOT NULL
       AND counterparty_override_at IS NOT NULL)
  );

-- ─────────────────────────────────────────────────────────────────────
-- Indexes
-- ─────────────────────────────────────────────────────────────────────

-- Filter index for scoring-time aggregation reads. Partial WHERE clause
-- keeps index size tied to classified rows only — pre-migration NULL
-- rows are excluded.
CREATE INDEX IF NOT EXISTS transactions_counterparty_class_idx
  ON public.transactions(counterparty_class)
  WHERE counterparty_class IS NOT NULL;

-- Partial index on overrides for the operator review surface. Rare
-- event → small index even after years of overrides. Keyed by
-- merchant_id so "show me all overrides for this merchant" is one
-- index seek.
CREATE INDEX IF NOT EXISTS transactions_counterparty_overridden_idx
  ON public.transactions(merchant_id)
  WHERE counterparty_overridden = TRUE;

-- ─────────────────────────────────────────────────────────────────────
-- Verification queries (run separately to confirm the migration
-- achieved its goal — not part of the migration itself).
-- ─────────────────────────────────────────────────────────────────────

-- All 6 columns should be present and the right type:
--
-- SELECT column_name, data_type, is_nullable, column_default
-- FROM information_schema.columns
-- WHERE table_schema = 'public'
--   AND table_name = 'transactions'
--   AND column_name LIKE 'counterparty_%'
-- ORDER BY ordinal_position;
--
-- Both indexes should exist:
--
-- SELECT indexname, indexdef
-- FROM pg_indexes
-- WHERE schemaname='public' AND tablename='transactions'
--   AND indexname LIKE '%counterparty%';
--
-- All three CHECK constraints should be present:
--
-- SELECT conname, pg_get_constraintdef(oid)
-- FROM pg_constraint
-- WHERE conrelid='public.transactions'::regclass
--   AND conname LIKE '%counterparty%';
