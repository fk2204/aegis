-- Migration 004 — disclosure_transmission_log table.
--
-- Tracks every disclosure document AEGIS forwards on behalf of a funder
-- to a CA merchant per 10 CCR § 952 — see
-- docs/compliance/01_california.md, section "Section 952 operational
-- rules for AEGIS." § 952 requires unaltered transmission with proof:
-- the disclosure document itself, the timestamp sent, the merchant
-- delivery / acknowledgment receipt, and confirmation back to the
-- funder. All four artifacts must be retained at least 4 years.
--
-- ``retention_until`` is computed as ``transmitted_at + 4 years +
-- 30 day buffer`` per dossier 10 (record_retention). The 30-day buffer
-- absorbs operator clock skew + statute-of-limitations edge cases so a
-- record is not reaped a day before a regulator could plausibly request
-- it. STORED generated column locks the value at insert time so a clock
-- change does not retroactively shorten the retention window.

CREATE TABLE IF NOT EXISTS disclosure_transmission_log (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- The deal this disclosure relates to. Tied to the parsed document
  -- (the bank statement that drove the underwriting decision) since
  -- AEGIS does not currently materialize a separate ``deals`` table.
  deal_id UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,

  -- Funder whose disclosure is being forwarded. ON DELETE RESTRICT —
  -- compliance records must outlive funder rows.
  funder_id UUID NOT NULL REFERENCES funders(id) ON DELETE RESTRICT,

  -- sha256 hex of the disclosure document AEGIS forwarded. Lets a
  -- regulator (or attorney) verify the doc was unaltered.
  disclosure_doc_hash TEXT NOT NULL,

  transmitted_at TIMESTAMPTZ NOT NULL,
  transmitted_to_email TEXT NOT NULL,

  -- Acknowledgment (signature receipt or email open / click) and the
  -- confirmation back to the funder. Both nullable because they land
  -- after the row is created.
  merchant_acknowledged_at TIMESTAMPTZ,
  funder_notified_at TIMESTAMPTZ,

  -- Computed at insert; locked once stored. 4 years is § 952's floor;
  -- the 30-day buffer is dossier-10 guidance.
  retention_until TIMESTAMPTZ
    GENERATED ALWAYS AS (transmitted_at + INTERVAL '4 years 30 days') STORED,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dtl_deal      ON disclosure_transmission_log (deal_id);
CREATE INDEX IF NOT EXISTS idx_dtl_funder    ON disclosure_transmission_log (funder_id);
CREATE INDEX IF NOT EXISTS idx_dtl_retention ON disclosure_transmission_log (retention_until);
