-- Enrich notes_residual for §3 Velocity Capital Group, §9 Big Think
-- Capital, and §10 Bizi Connect from Filip's internal MCA Funder Manual.
--
-- Migration 046 (VCG) left notes_residual = ''. Migration 047 (Big Think
-- + Bizi Connect) populated a brief auto-generated note. This migration
-- writes operator-curated summaries pulled verbatim from manual §3 / §9
-- / §10 so /ui/funders/{id} surfaces the strategic context the
-- structured columns can't capture: prohibited-industry highlights,
-- commission economics, payment timing, clawback mechanics, restrictive
-- covenants.
--
-- Each note kept under ~500 chars for UI readability. Pattern mirrors
-- migration 052 (Shor Capital).
--
-- Idempotent: re-running OVERWRITES notes_residual. If operator has
-- edited via /ui/funders/{id} since the last apply, those edits will be
-- replaced. Same convention as migration 049 / 052.

UPDATE funders SET notes_residual = '1st-4th positions, all 50 states. Up to $2M (largest cap in stack). Min 12mo TIB / $20K rev / 500 FICO. Withholding cap 30%. 16 prohibited industries — hard avoid Trucking, Auto, Real Estate, Law, Gas Stations. Commission 0–15 pts by underwriting risk; +1/+2/+3 bonus at 3/5/7 funded/mo. Payment: lump sum 7 business days after funding (NOT same-day per Schedule A). Clawback within 30 days if 5 consec or 10 non-consec misses; ACH offset if refused. Renewals at 50% paid. §3.'
WHERE name = 'Velocity Capital Group';

UPDATE funders SET notes_residual = 'AFFILIATE PARTNER (not points ISO). 35% commission split on new deals; BTC handles all underwriting + funding. Paid 5th of each month for prior-month funded deals. NO CLAWBACK: defaults reduce future commissions only, never invoiced or ACH-pulled — lowest cash-flow risk in stack. 2yr non-solicit on BTC merchants/employees. BTC-approved marketing only. Confirm box with Aidan Miller. §9.'
WHERE name = 'Big Think Capital';

UPDATE funders SET notes_residual = 'LOAN MARKETPLACE (not MCA). 30% of the FEE BC earns on loans funded & closed via your referral link (NOT % of loan, NOT points). BC controls underwriting + merchant contact. Paid within 10 days after end of month earned; W-9 required. PSF BAN: no fees to customer/lender. 18mo non-solicit on Marketplace lenders + 24mo non-circumvention post-termination. NY law. Different lane from MCA. §10.'
WHERE name = 'Bizi Connect';
