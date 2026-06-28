-- Migration 084 — federal bankruptcy check columns on merchants.
--
-- Phase B of the Master Plan multi-source compliance stack. The
-- ``aegis.business_intel.bankruptcy_checker`` module queries the
-- CourtListener v4 REST API for federal bankruptcy filings against
-- the merchant's business name and the principal's owner name. The
-- result lands on these columns; the dossier renders a chip and
-- (for Chapter 7 active) hard-blocks funder matching upstream.
--
-- All columns nullable: a merchant that has never been checked has
-- ``bankruptcy_checked_at IS NULL`` — that's the "needs first
-- check" sentinel ``ensure_bankruptcy_check`` reads. The check
-- itself runs lazily on first scoring + on the dossier "Refresh"
-- button; we never backfill at migration time because we don't
-- want to burn 5,000 CourtListener requests on existing rows in
-- one shot.
--
-- ``bankruptcy_cases`` is a denormalised JSONB blob mirroring the
-- ``BankruptcyResult.cases`` list — each entry carries
-- ``docket_id``, ``chapter``, ``filing_date``, ``date_terminated``,
-- ``date_closed``, ``case_name``, ``court_id``, ``active``,
-- ``recent``. The chip surfaces top-level summary; the dossier
-- drill-down reads the JSONB for per-case detail.

BEGIN;

ALTER TABLE merchants
  ADD COLUMN IF NOT EXISTS bankruptcy_checked_at timestamptz,
  ADD COLUMN IF NOT EXISTS bankruptcy_active     boolean,
  ADD COLUMN IF NOT EXISTS bankruptcy_recent     boolean,
  ADD COLUMN IF NOT EXISTS bankruptcy_chapter    text,
  ADD COLUMN IF NOT EXISTS bankruptcy_cases      jsonb;

COMMIT;
