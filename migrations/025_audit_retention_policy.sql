-- Migration 025 — audit_retention_policy lookup table (mp Phase 7 §17).
--
-- Per-state retention windows the archiver applies before moving audit_log
-- rows to audit_log_archive. Master plan §17:
--   NY = 4 yr (23 NYCRR § 600.21)
--   CA = 4 yr (10 CCR § 952)
--   Tier 3 default = 5 yr defensive
--
-- The archiver reads this table at runtime so policy adjustments are a
-- single SQL UPDATE, not a code change. The `state_code` column is keyed
-- to `decisions.state_code` (which the audit row inherits via deal_id);
-- rows that cannot be attributed to a state fall back to the
-- '__default__' sentinel.
--
-- Citations live in the `statute_citation` column so an auditor reading
-- the table can verify provenance without reading code.

CREATE TABLE IF NOT EXISTS audit_retention_policy (
  state_code TEXT PRIMARY KEY,
  retention_years INTEGER NOT NULL CHECK (retention_years >= 1),
  statute_citation TEXT NOT NULL,
  last_reviewed DATE NOT NULL DEFAULT CURRENT_DATE,
  notes TEXT
);

ALTER TABLE audit_retention_policy ENABLE ROW LEVEL SECURITY;

-- Seed inserts. Idempotent via ON CONFLICT DO NOTHING.

INSERT INTO audit_retention_policy
  (state_code, retention_years, statute_citation, notes)
VALUES
  ('CA', 4, 'CA 10 CCR § 952', 'Tier 1: 4 years from disclosure transmission.'),
  ('NY', 4, '23 NYCRR § 600.21', 'Tier 1: 4 years from disclosure transmission.'),
  ('FL', 4, 'No explicit statute; mirror CA/NY (mp §10)', 'Tier 1 recommended.'),
  ('GA', 4, 'No explicit statute; mirror CA/NY (mp §10)', 'Tier 1 recommended.'),
  ('VA', 4, 'VA SCC SB 1027 + KYC cascade', 'Tier 1.'),
  ('UT', 5, 'UT HB 198 + KYC cascade', 'Tier 1.'),
  ('CT', 4, 'CT SB 1032 + KYC cascade', 'Tier 1.'),
  ('MO', 4, 'MO SB 1359 §427.300', 'Tier 1.'),
  ('TX', 5, 'TX HB 700 Ch. 398 + KYC cascade', 'Tier 1.'),
  ('__default__', 5, 'Tier 3 default per master plan §17', 'Defensive 5-yr default.')
ON CONFLICT (state_code) DO NOTHING;
