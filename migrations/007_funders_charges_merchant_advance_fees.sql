-- Migration 007 — funders.charges_merchant_advance_fees column.
--
-- Per docs/compliance/03_florida.md (Fla. Stat. § 559.9614(1)(a)): a
-- broker may not assess, collect, or solicit an advance fee from a
-- merchant for brokering services (with a narrow exception for actual
-- third-party services like credit checks paid by the merchant directly
-- to an independent third party).
--
-- AEGIS's standard practice is broker commission paid by the funder,
-- never advance fees from merchants. A funder whose ISO contract
-- requires merchant-side advance fees must have this column flipped
-- TRUE; the matcher then hard-fails any pairing with a merchant in a
-- state that flags broker_advance_fees_prohibited (currently FL only).
-- The block is parallel to the CoJ rule introduced in migration 005:
-- a state-level statutory prohibition propagates from STATES through
-- the matcher into a deal-level hard fail.
--
-- Default false: existing funders are assumed NOT to charge merchant
-- advance fees until the operator explicitly sets the flag from the
-- funder's ISO agreement.

ALTER TABLE funders
  ADD COLUMN IF NOT EXISTS charges_merchant_advance_fees BOOLEAN NOT NULL DEFAULT false;
