-- Migration 093 — tax_returns table. Stores extracted figures from
-- 1120 / 1120-S / 1065 / Schedule C / 1040 forms uploaded as merchant
-- documents. Populated by src/aegis/parser/tax_return/. Surfaced on
-- the dossier as a "Tax Return Summary" section with YoY gross
-- receipts + net income comparison.

CREATE TABLE IF NOT EXISTS tax_returns (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id          uuid NOT NULL REFERENCES merchants(id) ON DELETE CASCADE,
    document_id          uuid REFERENCES documents(id),
    form_type            text NOT NULL CHECK (form_type IN ('1120','1120s','1065','schedule_c','1040')),
    tax_year             integer NOT NULL,
    gross_receipts       numeric(14,2),
    net_income           numeric(14,2),
    total_assets         numeric(14,2),
    total_liabilities    numeric(14,2),
    officer_compensation numeric(14,2),
    extracted_at         timestamptz DEFAULT now(),
    raw_extraction       jsonb
);
CREATE INDEX IF NOT EXISTS idx_tax_returns_merchant
    ON tax_returns (merchant_id, tax_year DESC);
