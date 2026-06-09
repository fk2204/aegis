-- Migration 044 — merchants_shadow_signals
--
-- U22 — persists the cross-statement Pattern list that U15 (commit
-- 88f1e9b) stashes on ``PipelineResult.cross_statement_patterns``.
-- The U15 agent flagged the persistence decision as a follow-up:
--
--   "Persisting cross_statement_patterns (decision between
--    pattern_analysis.shadow_patterns vs a new merchants.shadow_signals
--    channel) — left for a follow-up."
--
-- Decision: NEW table, keyed by merchant. Rationale:
--
--   (A) pattern_analysis.shadow_patterns is DOCUMENT-scope — every row
--       inside an ``analyses.pattern_analysis`` JSONB is bound to ONE
--       document. Cross-statement signals are by definition CROSS-document
--       (the U12 detector consumes the current upload + every prior
--       upload). Pushing them into pattern_analysis would either:
--         * duplicate the same ``duplicate_pdf_upload`` event into the
--           shadow_patterns of BOTH documents (current + prior collider),
--           with no merchant-level dedup, OR
--         * silently drop one half so the chronologically-second parse
--           "wins" and the first prior never carries the signal.
--       Both outcomes are wrong for the operator-review surface.
--
--   (B) A merchant-keyed table makes the boundary explicit:
--         * ``pattern_analysis.shadow_patterns`` -> per-document shadow
--           (R1.1 / R1.3 fuzzy / disguise / same-day cluster, M9
--           structured-deposit-cluster). Scope = one document.
--         * ``merchants_shadow_signals`` (044) -> per-merchant shadow.
--           Scope = the merchant. Future signals (renewal-attestation
--           shadow, related-account suspicion, operator-side flags) land
--           here cleanly without conflating with document-scope rows.
--
--   (C) One row per signal -> queryable, indexable, joinable. The
--       dossier route reads ``merchants_shadow_signals WHERE merchant_id
--       = ? ORDER BY detected_at DESC`` and feeds it through the U18
--       humanizer (commit 9d13e61) for render. The same query supports
--       a future operator triage tile ("merchants with the most
--       merchant-scope shadow signals in the last 7 days").
--
-- The U12 detector emits Pattern.severity=0 (shadow-only invariant) and
-- the U15 orchestrator never raises that — ``signal_severity SMALLINT
-- NOT NULL DEFAULT 0`` mirrors that contract on the DB side. Live
-- (severity > 0) signals are out of scope for this migration; the
-- column exists so a future operator-validated flip via env-var doesn't
-- need a schema change, but per CLAUDE.md "decision-boundary changes
-- — shadow-first" + the U22 instructions, no code path writes
-- severity > 0 today.
--
-- No retention column. Shadow signals are operator-review history;
-- they outlive the documents that produced them (a duplicate-PDF
-- signal stays meaningful even after the colliding PDF is retention-
-- purged seven years out). Purge happens when the merchant is
-- hard-deleted; no calendar-based expiry.
--
-- Indexes
-- -------
--   * (merchant_id, detected_at DESC) — per-merchant render history:
--     drives the dossier "Merchant-level shadow signals" section
--     newest-first.
--   * (signal_code, detected_at DESC) — operator-facing roll-up: "show
--     every duplicate_pdf_upload across all merchants in the last
--     week" for triage. Mirrors the 042 (status, rendered_at DESC)
--     pattern.
--
-- RLS: enabled, service-role only. Mirrors migrations 036 / 037 / 040 /
-- 042 — operator UI reaches this table via the API process, not via
-- direct Supabase client credentials.
--
-- PII discipline (per CLAUDE.md)
-- ------------------------------
-- ``detail`` may carry the raw (statement-literal) ``account_holder``
-- string from the U12 detector's Pattern.detail (e.g.
-- ``holder=Acme LLC:existing_last4=9999:new_last4=1234``). That holder
-- is PII; the column is acceptable in the database because the
-- merchant-scope dossier render needs it for operator context. Loggers
-- mask by key name; the audit_log row paired with each write carries
-- the signal CODE + severity + source_document_id only — never the
-- holder string. See ``src/aegis/merchants/shadow_signals.py``
-- ``record_shadow_signal`` for the paired-write contract.
--
-- Idempotent via ``IF NOT EXISTS``.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS merchants_shadow_signals (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- The merchant the signal is bound to. Soft FK (no REFERENCES) — the
  -- table outlives merchant rows when the operator soft-deletes them,
  -- mirroring the 036 / 042 precedent for compliance-adjacent audit
  -- shapes.
  merchant_id UUID NOT NULL,

  -- The U12 Pattern.code (``duplicate_pdf_upload``,
  -- ``related_account_suspected``, future). Free-text VARCHAR(64) so a
  -- future detector landing a new code does not require a schema
  -- migration. Mirrors the 042 status free-text precedent.
  signal_code VARCHAR(64) NOT NULL,

  -- Always 0 today per the U12 shadow-only invariant. SMALLINT (not
  -- BOOL) so a future operator-validated decision-boundary flip can
  -- record severity > 0 without a schema change. NOT NULL + DEFAULT 0
  -- enforces the shadow default at the DB layer.
  signal_severity SMALLINT NOT NULL DEFAULT 0,

  -- Humanized detail string from Pattern.detail. May contain PII
  -- (account_holder) per the header note. Loggers mask by key name.
  detail TEXT,

  -- The document whose upload triggered the signal — the current
  -- upload in the U15 worker hook. Nullable because a future
  -- merchant-scope signal (e.g. renewal-attestation suspicion) may
  -- not have a single triggering document. Soft FK.
  source_document_id UUID,

  -- Prior-document UUIDs (Pattern.source_ids) — the documents the
  -- detector matched against. JSONB array of UUID strings. The U18
  -- humanizer reads this for the chip drill-down ("matched 2 prior
  -- uploads on the same SHA").
  source_ids JSONB,

  -- Raw evidence the formatter parses. Schema:
  --   {"emitted_by": "cross_statement_detector"}
  -- Future detectors register their own emitted_by value so the
  -- humanizer can route the detail format. PII-safe by contract — the
  -- raw holder string lives in ``detail``, not here.
  metadata JSONB,

  -- When the worker fired the signal. NOT NULL.
  detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  -- Free-text actor identifier. Convention: 'worker' for the U15
  -- post-parse hook; future operator-side writes use 'dashboard' or a
  -- specific Close user id. Mirrors the 036 sent_by / 042 rendered_by
  -- conventions.
  detected_by VARCHAR(128)
);

-- Per-merchant render history: drives the dossier "Merchant-level
-- shadow signals" section newest-first.
CREATE INDEX IF NOT EXISTS idx_merchants_shadow_signals_merchant
  ON merchants_shadow_signals (merchant_id, detected_at DESC);

-- Operator-facing roll-up: "show every duplicate_pdf_upload across all
-- merchants in the last week." Drives the future triage tile.
CREATE INDEX IF NOT EXISTS idx_merchants_shadow_signals_code
  ON merchants_shadow_signals (signal_code, detected_at DESC);

-- Default-deny RLS: internal-only shadow-signal log, accessible to the
-- service role only. Mirrors migrations 036 / 037 / 040 / 042.
ALTER TABLE merchants_shadow_signals ENABLE ROW LEVEL SECURITY;
