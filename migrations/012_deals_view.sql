-- Migration 012 — deals view.
--
-- A deal in AEGIS is the join (merchants × documents [× analyses]).
-- No new table — phase 7 audit finding F1 locks this shape: "deal is a
-- derived view of (merchant_id, document_id), not a stored entity."
--
-- The view exists so a caller that wants the joined shape without
-- replicating the SELECT can ``SELECT * FROM deals`` and get a stable
-- column set. The Python ``SupabaseDealRepository`` goes through the
-- ``documents`` table with nested PostgREST selects rather than this
-- view (PostgREST's view support is limited for nested filters); the
-- view is here for ad-hoc SQL / Supabase Studio inspection / future
-- BI tooling.
--
-- Row Level Security note:
--   Postgres views inherit RLS from the underlying tables. Migration
--   011 enabled RLS on every public table (merchants, documents,
--   analyses). Because AEGIS connects via ``SUPABASE_SERVICE_KEY``
--   (service_role) which bypasses RLS, the backend can read the view
--   unchanged. The anon / authenticated PostgREST roles are denied,
--   matching the underlying tables' policy.
--
-- deal_id column shape:
--   Composite string ``'{merchant_id}:{document_id}'`` matching
--   ``aegis.deals.models.format_deal_id``. Python's parse_deal_id
--   recovers the components — see models.py for the rationale on
--   composite-string vs UUID v5.

CREATE OR REPLACE VIEW deals AS
SELECT
  (documents.merchant_id::text || ':' || documents.id::text) AS deal_id,
  documents.merchant_id,
  documents.id                    AS document_id,
  documents.uploaded_at           AS created_at,
  documents.parse_status,
  documents.fraud_score,
  merchants.business_name,
  merchants.state,
  analyses.id                     AS analysis_id,
  analyses.statement_period_start,
  analyses.statement_period_end,
  analyses.monthly_revenue,
  analyses.avg_daily_balance
FROM documents
JOIN merchants
  ON documents.merchant_id = merchants.id
LEFT JOIN analyses
  ON analyses.document_id = documents.id
WHERE documents.merchant_id IS NOT NULL;

-- No GRANT statements: service_role already has full access through
-- Supabase's default role config; anon/authenticated are intentionally
-- denied via the underlying tables' RLS (no policies = full deny).
--
-- Verification query (run separately):
--   SELECT count(*) FROM deals;
--   SELECT deal_id, business_name, parse_status FROM deals LIMIT 5;
