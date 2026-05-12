-- Add zoho_lead_id to merchants for Lead-stage Zoho sync.
-- Mirrors zoho_deal_id; populated by ZohoSync.push_merchant_to_lead.
ALTER TABLE merchants ADD COLUMN IF NOT EXISTS zoho_lead_id TEXT;
CREATE INDEX IF NOT EXISTS idx_merchants_zoho_lead_id ON merchants (zoho_lead_id) WHERE zoho_lead_id IS NOT NULL;
