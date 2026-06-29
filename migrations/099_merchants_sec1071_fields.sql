-- Migration 099 — §1071 demographic data collection columns on merchants.
--
-- The CFPB's Small Business Lending Data Collection Rule (Reg B
-- §1071) requires lenders that originate small business credit to
-- collect protected demographic data on each application. AEGIS is
-- a broker, NOT a lender — the funders own the §1071 reporting
-- obligation — but operators capture these fields here so the funder
-- submission packet is complete and the dossier renders the data the
-- funder underwriter expects.
--
-- Fields mirror the Reg B Subpart B §1002.107 collection items:
--   * principal_ethnicity / principal_race / principal_sex — protected
--     class data on the principal owner. Free-text columns; the
--     dossier surfaces a fixed pick-list per CFPB Appendix B.
--   * gross_annual_revenue — money column for the small-business
--     size determination.
--   * num_workers_range — bucketed worker count (1, 2-4, 5-9, ...).
--   * census_tract — 11-digit FIPS census tract code, populated from
--     the merchant's business address.
--   * collected_at — operator collection timestamp. NULL means the
--     §1071 panel has never been opened for this merchant.
--
-- ALL columns nullable — §1071 data is collected at the operator's
-- discretion for loan / line-of-credit product types only. Merchants
-- on revenue-based / MCA product types skip the panel entirely
-- (CFPB rule excludes MCAs from §1071 collection).

ALTER TABLE merchants
  ADD COLUMN IF NOT EXISTS sec1071_principal_ethnicity   text,
  ADD COLUMN IF NOT EXISTS sec1071_principal_race        text,
  ADD COLUMN IF NOT EXISTS sec1071_principal_sex         text,
  ADD COLUMN IF NOT EXISTS sec1071_gross_annual_revenue  numeric(14,2),
  ADD COLUMN IF NOT EXISTS sec1071_num_workers_range     text,
  ADD COLUMN IF NOT EXISTS sec1071_census_tract          text,
  ADD COLUMN IF NOT EXISTS sec1071_collected_at          timestamptz;

COMMENT ON COLUMN merchants.sec1071_collected_at IS
  '§1071 demographic data collection timestamp. NULL = panel never opened. '
  'When NULL the dossier renders the collection prompt; when set the dossier '
  'renders the captured values.';
