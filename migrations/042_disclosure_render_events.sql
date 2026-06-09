-- Migration 042 — disclosure_render_events (U16 — persist deferred from U3).
--
-- U3 (commit 924d799) added an ``APRDisclosureError`` catcher in
-- ``api/routes/disclosures.py`` that returns a structured ``needs_review``
-- response and writes one ``audit_log`` row. The agent's note on that
-- commit was: "Persistent disclosure_status column / state machine is
-- intentionally deferred to a follow-up ticket as instructed."
--
-- This migration closes that loop with a render-event table.
--
-- Why a NEW table rather than a column on ``disclosure_transmissions``
-- -------------------------------------------------------------------
-- ``disclosure_transmissions`` (migration 036) represents disclosures
-- that were actually SENT to a merchant: it stores ``html_sha256``,
-- ``recipient_email``, ``sent_at``, and the disclosed financial terms
-- (apr / funding_provided / finance_charge etc), and the row is bound
-- by a 4-year STORED ``retention_until`` floor mandated by CA 10 CCR
-- § 952 and NY 23 NYCRR § 600. Every column on that table assumes a
-- regulator-facing transmission event happened.
--
-- A ``needs_review`` event has no ``sent_at``, no rendered HTML, no
-- ``html_sha256``, and (by design — see ``disclosure.py``) no APR. The
-- semantics simply do not fit the transmission shape, and bolting an
-- ``is_send`` discriminator onto 036 would let a regulator-shaped query
-- silently pick up internal-only pre-flight events.
--
-- A separate table makes the boundary explicit:
--
--   * ``disclosure_transmissions`` (036) → regulator-shaped sent record.
--     4-year retention floor. STORED retention_until. The audit table.
--   * ``disclosure_render_events`` (042) → internal pre-flight render
--     log. Did AEGIS's preview render cleanly (``ok``) or fail
--     (``apr_compute_failed`` / ``needs_review``)? No retention floor,
--     no regulator-facing semantics. The operator-debug table.
--
-- Per ``.claude/rules/compliance.md`` SCOPE NOTE: AEGIS is internal
-- pre-flight; the funder owns regulator-facing issuance. This table is
-- AEGIS's record of its own render attempts, NOT a regulator-facing
-- status surface.
--
-- Status enum (free-text VARCHAR(32) so future render statuses do not
-- require a schema migration):
--
--   * ok                       — render succeeded; row may carry the
--                                ``recipient_email`` if the render led
--                                to a transmission.
--   * needs_review             — render produced a known-bad output and
--                                AEGIS held the disclosure. Currently
--                                used by the APR-failure path; future
--                                detectors (zero-balance day, missing
--                                disbursement_date) can reuse it.
--   * apr_compute_failed       — specific case of needs_review where
--                                ``compliance.apr.calculate_apr`` could
--                                not converge for the supplied payment
--                                schedule.
--   * template_render_failed   — Jinja2 template render itself raised.
--                                Future-use; not emitted yet.
--   * format_validation_failed — post-render format check failed
--                                (e.g. missing required field after
--                                template fill). Future-use.
--
-- Soft FKs to deals / merchants
-- -----------------------------
-- Both nullable; not FK-enforced. Mirrors the ``disclosure_transmissions``
-- (036) and ``funder_renewal_attestations`` (040) precedent: compliance-
-- adjacent audit rows must outlive the rows they describe, and the route
-- may catch an ``APRDisclosureError`` before any merchant lookup
-- completed (in which case both ids are NULL and the row is queryable
-- by (status, rendered_at)).
--
-- Indexes
-- -------
--   * (deal_id, rendered_at DESC) — per-deal render history: "show me
--     every render attempt for this deal newest-first."
--   * (status, rendered_at DESC) — operator-facing triage scan: "show
--     me every needs_review event in the last hour."
--
-- RLS: enabled, service-role only. Mirrors migrations 036 / 037 / 040.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS disclosure_render_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Soft FK to deals/merchants — see header note. Both nullable because
  -- the route may catch an APRDisclosureError before any merchant lookup
  -- completes.
  deal_id UUID,
  merchant_id UUID,

  -- USPS two-letter code when known. NULL when the render failed before
  -- a state could be resolved (defensive — APRDisclosureError currently
  -- always carries one).
  state VARCHAR(2),

  -- Repo-relative template path (e.g.
  -- 'compliance/templates/ca_sb1235.html.j2') when the render reached
  -- the template-resolution step. NULL when the render failed earlier
  -- (e.g. APR compute failure before template resolution).
  template_path VARCHAR(255),

  -- See enum list in the header comment. Free-text VARCHAR(32) so a
  -- future render-status case does not require a schema migration.
  -- NOT NULL — every row describes a known render outcome.
  status VARCHAR(32) NOT NULL,

  -- Short human-readable reason. For ``apr_compute_failed`` this is the
  -- ``APRDisclosureError`` message ("brentq failed to converge"). For
  -- ``ok`` events it is typically NULL.
  status_reason VARCHAR(255),

  -- Structured non-PII context (numeric APR inputs, term_days, factor,
  -- deal-id-ish reference). MUST NOT carry business_name / owner_name /
  -- transaction descriptions — see CLAUDE.md PII rules.
  details JSONB,

  -- Optional — set when ``status='ok'`` and the render led to a
  -- transmission. Mirrors ``disclosure_transmissions.recipient_email``.
  recipient_email VARCHAR(255),

  -- When the render attempt happened (wall clock). Distinct from a
  -- ``sent_at`` because not every render attempt results in a
  -- transmission. NOT NULL.
  rendered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  -- Free-text operator identifier. Same surface convention as
  -- ``disclosure_transmissions.sent_by``: may be a Supabase auth uid,
  -- a Close user id, an internal worker name, or 'api' for caller-
  -- agnostic requests.
  rendered_by VARCHAR(128),

  -- Free-form structured metadata for operator-discretion fields
  -- (render-mode flag, dashboard breadcrumb, future shadow-flag refs).
  metadata JSONB
);

-- Per-deal render history: drives the dossier render-history panel.
CREATE INDEX IF NOT EXISTS idx_disclosure_render_events_deal
  ON disclosure_render_events (deal_id, rendered_at DESC);

-- Operator-facing triage: "show me every needs_review event newest
-- first." Drives the held-for-review queue.
CREATE INDEX IF NOT EXISTS idx_disclosure_render_events_status
  ON disclosure_render_events (status, rendered_at DESC);

-- Default-deny RLS: internal-only render-event log, accessible to the
-- service role only. Mirrors migrations 036 / 037 / 040.
ALTER TABLE disclosure_render_events ENABLE ROW LEVEL SECURITY;
