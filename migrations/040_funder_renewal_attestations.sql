-- Migration 040 — funder_renewal_attestations (R3.2 follow-up, U6).
--
-- R3.2 (commit 6102bbc) shipped the operator-visibility renewal calendar
-- and migration 039 (commit 7fb28ef) shipped the ``merchants.maturity_date``
-- column the calendar reads from, but every row in the calendar still
-- defaults to ``renewal_status='not_required_funder_owns'`` because AEGIS
-- has no operator-side capture for "the funder has confirmed they sent the
-- pre-maturity disclosure on date X."
--
-- This table closes that loop: one row per (merchant × maturity_date ×
-- funder_name) operator attestation that the funder partner has
-- transmitted the required notice. The renewal calendar reads from it to
-- flip the per-row status off the default into one of:
--
--   * disclosure_sent      — attestation exists for this (merchant, maturity)
--   * disclosure_pending   — no attestation, state deadline < 14 days away,
--                            deadline not yet past
--   * disclosure_overdue   — no attestation, state deadline already past
--   * not_required_funder_owns — the default (no attestation, deadline > 14d
--                            out or no AEGIS-tracked state deadline)
--
-- Per CLAUDE.md SCOPE NOTE + ``.claude/rules/compliance.md`` SCOPE NOTE:
-- AEGIS does NOT own the regulator-facing renewal disclosure obligation —
-- funder partners do (CA SB 362 § 22806 — 60 days pre-maturity; NY 23
-- NYCRR § 600.17 — 30 days pre-maturity). This table records OPERATOR
-- ATTESTATIONS that the funder has fulfilled their obligation; the
-- row's existence is the operator's claim, not a regulator-facing audit
-- artifact. The funder's own audit trail remains the regulator-facing
-- record.
--
-- Soft FK to merchants
-- --------------------
-- ``merchant_id`` is NOT FK-enforced. Compliance-adjacent audit rows
-- must outlive every other row by design (the merchant may be soft-
-- deleted but the attestation that the funder transmitted a notice
-- on date X must survive). Mirrors the pattern from migrations 036
-- (disclosure_transmissions) and 037 (scoring_shadow_disagreements).
--
-- Indexes
-- -------
--   * (merchant_id, maturity_date) — the renewal-calendar lookup:
--     "did the operator attest for this merchant + this maturity?"
--   * (attested_at DESC) — the operator-facing recent-activity view:
--     "show me every attestation captured newest first"
--
-- RLS: enabled, service-role only. Mirrors migrations 036 / 037.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS funder_renewal_attestations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Soft FK to merchants — see header note. Compliance-audit rows
  -- must outlive the merchant they describe.
  merchant_id UUID NOT NULL,

  -- The funder partner attesting they sent the pre-maturity notice.
  -- Free-text rather than a FK to ``funders.id`` because attestations
  -- may reference funders that pre-date the funders table (legacy
  -- deals) and the regulator-facing question "who sent the notice"
  -- is answered by name, not by AEGIS's internal funder id.
  funder_name VARCHAR(255) NOT NULL,

  -- The renewal maturity this attestation covers. Soft references
  -- ``merchants.maturity_date`` (migration 039); not enforced because
  -- the operator may attest before the maturity_date column is set
  -- (rare but legitimate workflow ordering).
  maturity_date DATE NOT NULL,

  -- When the funder reports they sent the notice. A DATE, not a
  -- TIMESTAMPTZ, because the regulator-facing question is "on what
  -- day", not "at what wall-clock instant."
  disclosure_sent_at DATE NOT NULL,

  -- Operator identifier (Cloudflare Access email or 'dashboard' fallback).
  -- Free-text to accommodate the various operator-identity surfaces
  -- (CF email header, future Supabase auth uid). Mirrors
  -- ``disclosure_transmissions.sent_by``.
  attested_by VARCHAR(128) NOT NULL,

  -- When the operator submitted this attestation. Distinct from
  -- ``disclosure_sent_at`` (which is the funder's claim) — ``attested_at``
  -- is when the claim landed in AEGIS.
  attested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  -- Operator's free-form note (e.g. "confirmed via email from
  -- compliance@funderA.com"). Optional.
  notes TEXT,

  -- The merchant's state at attestation time. Pinned for audit
  -- clarity — the merchant may be edited later but the state
  -- this attestation was filed under stays. USPS 2-letter code.
  state VARCHAR(2) NOT NULL,

  -- The state statute this attestation references, when applicable
  -- (e.g. 'CA SB 362 § 22806', 'NY 23 NYCRR § 600.17'). NULL for
  -- merchants in states with no AEGIS-tracked renewal-disclosure
  -- deadline. Pinned at insert so a future statute change is
  -- traceable.
  applicable_statute VARCHAR(64),

  -- Free-form structured metadata for operator-discretion fields
  -- (channel of confirmation, future renewal-cohort identifiers).
  metadata JSONB
);

-- Renewal-calendar lookup index: "did the operator attest for
-- this merchant + this maturity?" The (merchant_id, maturity_date)
-- composite is the natural lookup the renewal-status accessor uses.
CREATE INDEX IF NOT EXISTS idx_funder_renewal_attestations_merchant_maturity
  ON funder_renewal_attestations (merchant_id, maturity_date);

-- Recent-activity scan: newest attestation first. Drives the
-- operator-facing recent-activity panel (when added).
CREATE INDEX IF NOT EXISTS idx_funder_renewal_attestations_attested_at
  ON funder_renewal_attestations (attested_at DESC);

-- Default-deny RLS: this is internal-only audit data, accessible
-- to the service role only (the renewal-calendar route + the UI
-- attestation-capture form). Mirrors the pattern from migrations
-- 036 (disclosure_transmissions) and 037
-- (scoring_shadow_disagreements).
ALTER TABLE funder_renewal_attestations ENABLE ROW LEVEL SECURITY;
