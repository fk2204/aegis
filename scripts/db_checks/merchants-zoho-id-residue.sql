-- DESCRIPTION: Pre-cutover check for Close integration. Counts merchant rows
-- still carrying zoho_deal_id or zoho_lead_id values so migration 026 can
-- decide between DROP (0 rows) and RENAME-to-archive (>0 rows).
--
-- Informational. Returns one row with four columns:
--   zoho_deal_id_count   — merchants with non-null zoho_deal_id
--   zoho_lead_id_count   — merchants with non-null zoho_lead_id
--   either_set           — merchants with at least one of the two set
--   total_merchants      — total merchant row count (denominator for context)
SELECT
  (SELECT COUNT(*) FROM merchants WHERE zoho_deal_id IS NOT NULL) AS zoho_deal_id_count,
  (SELECT COUNT(*) FROM merchants WHERE zoho_lead_id IS NOT NULL) AS zoho_lead_id_count,
  (SELECT COUNT(*) FROM merchants WHERE zoho_deal_id IS NOT NULL OR zoho_lead_id IS NOT NULL) AS either_set,
  (SELECT COUNT(*) FROM merchants) AS total_merchants;
