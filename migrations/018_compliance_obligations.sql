-- Migration 018 — compliance_obligations table (master plan §9.5)
--
-- Registration deadlines, annual reports, license renewals across the
-- Tier 1 states. The obligations UI (Phase 7) reads from here. Status
-- transitions happen as the operator submits renewals; `evidence_file_path`
-- points to the receipt / confirmation document for audit.
--
-- Seeded with the 6 known Tier 1 obligations from master plan §9.5:
--   * VA SCC sales-based broker registration (annual)
--   * CT DOB sales-based provider/broker registration (annual, by Oct 1)
--   * UT DFI commercial financing registration via NMLS (annual)
--   * MO Div of Finance broker registration (one-time + maintain)
--   * TX OCCC sales-based broker registration (by 2026-12-31, annual by Jan 31)
--   * CA DFPI annual report (where applicable)

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS compliance_obligations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  obligation_type TEXT NOT NULL,
  state_code TEXT NOT NULL,
  authority TEXT NOT NULL,
  description TEXT NOT NULL,
  deadline DATE,
  recurrence TEXT,
  status TEXT NOT NULL
    CHECK (status IN ('not_started', 'in_progress', 'submitted', 'active', 'lapsed')),
  next_due_date DATE,
  evidence_file_path TEXT,
  last_reviewed TIMESTAMPTZ,
  notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_obligations_state ON compliance_obligations (state_code);
CREATE INDEX IF NOT EXISTS idx_obligations_status ON compliance_obligations (status);
CREATE INDEX IF NOT EXISTS idx_obligations_next_due
  ON compliance_obligations (next_due_date)
  WHERE next_due_date IS NOT NULL;

ALTER TABLE compliance_obligations ENABLE ROW LEVEL SECURITY;

-- ============================================================================
-- Seed inserts per master plan §9.5
--
-- Idempotent via the WHERE NOT EXISTS guard keyed on (state_code, authority,
-- obligation_type) which uniquely identifies each operator obligation.
-- ============================================================================

INSERT INTO compliance_obligations (
  obligation_type, state_code, authority, description, recurrence, status, notes
)
SELECT 'license_renewal', 'VA', 'VA SCC',
       'Sales-based financing broker registration (VA HB 1027). Renews annually.',
       'annual', 'not_started',
       'Required before serving VA-recipient MCA deals. See master plan §8.2.'
WHERE NOT EXISTS (
  SELECT 1 FROM compliance_obligations
  WHERE state_code = 'VA' AND authority = 'VA SCC'
    AND obligation_type = 'license_renewal'
);

INSERT INTO compliance_obligations (
  obligation_type, state_code, authority, description, recurrence, status, notes
)
SELECT 'license_renewal', 'CT', 'CT DOB',
       'Sales-based financing provider/broker registration (CT SB 1032). Renews annually by October 1.',
       'annual', 'not_started',
       'Required before serving CT-recipient MCA deals. See master plan §8.2.'
WHERE NOT EXISTS (
  SELECT 1 FROM compliance_obligations
  WHERE state_code = 'CT' AND authority = 'CT DOB'
    AND obligation_type = 'license_renewal'
);

INSERT INTO compliance_obligations (
  obligation_type, state_code, authority, description, recurrence, status, notes
)
SELECT 'registration', 'UT', 'UT DFI / NMLS',
       'Commercial financing registration via NMLS (UT HB 198). Annual renewal.',
       'annual', 'not_started',
       'Required before serving UT-recipient deals. See master plan §8.2.'
WHERE NOT EXISTS (
  SELECT 1 FROM compliance_obligations
  WHERE state_code = 'UT' AND authority = 'UT DFI / NMLS'
    AND obligation_type = 'registration'
);

INSERT INTO compliance_obligations (
  obligation_type, state_code, authority, description, recurrence, status, notes
)
SELECT 'registration', 'MO', 'MO Div of Finance',
       'Broker registration (MO SB 1359 §427.300). One-time + maintain.',
       'one_time', 'not_started',
       'Unusual: broker-only registration; providers do not register. See master plan §8.2.'
WHERE NOT EXISTS (
  SELECT 1 FROM compliance_obligations
  WHERE state_code = 'MO' AND authority = 'MO Div of Finance'
    AND obligation_type = 'registration'
);

INSERT INTO compliance_obligations (
  obligation_type, state_code, authority, description, deadline, recurrence, status, notes
)
SELECT 'registration', 'TX', 'TX OCCC',
       'Sales-based financing broker registration (TX HB 700 Ch. 398). Initial by 2026-12-31; annual renewal by January 31.',
       DATE '2026-12-31', 'annual', 'not_started',
       'Required regardless of deal size. Standard ACH-debit MCA structures cannot satisfy HB 700 first-priority-lien rule — see master plan §8.5.'
WHERE NOT EXISTS (
  SELECT 1 FROM compliance_obligations
  WHERE state_code = 'TX' AND authority = 'TX OCCC'
    AND obligation_type = 'registration'
);

INSERT INTO compliance_obligations (
  obligation_type, state_code, authority, description, recurrence, status, notes
)
SELECT 'annual_report', 'CA', 'CA DFPI',
       'California DFPI annual report (where applicable to commercial financing providers/brokers under SB 1235 / SB 362).',
       'annual', 'not_started',
       'Required for CFL-licensed providers. See master plan §8.2.'
WHERE NOT EXISTS (
  SELECT 1 FROM compliance_obligations
  WHERE state_code = 'CA' AND authority = 'CA DFPI'
    AND obligation_type = 'annual_report'
);
