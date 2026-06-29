-- Migration 094 — ar_aging_reports table. Stores extracted aging
-- buckets + top-debtor concentration for ABL / factoring product
-- screens. Populated by src/aegis/parser/ar_aging/.

CREATE TABLE IF NOT EXISTS ar_aging_reports (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id         uuid NOT NULL REFERENCES merchants(id) ON DELETE CASCADE,
    document_id         uuid REFERENCES documents(id),
    report_date         date,
    total_outstanding   numeric(14,2),
    current_amount      numeric(14,2),
    days_30_60          numeric(14,2),
    days_60_90          numeric(14,2),
    days_90_plus        numeric(14,2),
    debtor_count        integer,
    concentration_pct   numeric(5,2),
    top_debtors         jsonb,
    extracted_at        timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ar_aging_merchant
    ON ar_aging_reports (merchant_id, report_date DESC);
