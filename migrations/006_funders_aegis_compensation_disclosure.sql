-- Migration 006 — funders.aegis_compensation_disclosure_text column.
--
-- Per docs/compliance/02_new_york.md (23 NYCRR § 600.21(f)): when a
-- broker is involved in a commercial financing transaction, the
-- provider (funder) must inform the recipient in writing how, and by
-- whom, the broker is compensated. AEGIS is the broker. The funder
-- includes a per-funder text block describing AEGIS's compensation
-- arrangement for that funder when transmitting the disclosure to the
-- merchant. The text is per-funder because commission structure /
-- ISO fee schedule varies by funder.
--
-- Default empty string: existing funders are assumed to NOT yet have
-- a compensation disclosure on file. The disclosure-generation guard
-- (compliance/broker_compensation.py) blocks NY-merchant disclosures
-- when this column is empty for the chosen funder.

ALTER TABLE funders
  ADD COLUMN IF NOT EXISTS aegis_compensation_disclosure_text TEXT NOT NULL DEFAULT '';
