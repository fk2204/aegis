-- Migration 096 — funders.guidelines_data JSONB + guidelines_uploaded_at
-- timestamp. Populated when the operator uploads a funder guidelines PDF
-- via POST /ui/funders/{id}/guidelines and Bedrock extracts the
-- underwriting criteria. NEVER auto-overrides operator-curated criteria
-- on the FunderRow — the extracted JSONB is a side-channel the dossier
-- displays for context; only fields the operator explicitly promotes
-- land in the live FunderRow columns (separate operator UI flow).

ALTER TABLE funders
  ADD COLUMN IF NOT EXISTS guidelines_data jsonb,
  ADD COLUMN IF NOT EXISTS guidelines_uploaded_at timestamptz;

COMMENT ON COLUMN funders.guidelines_data IS
  'Bedrock-extracted underwriting criteria from uploaded guidelines PDF. '
  'Side-channel — does NOT auto-update the live FunderRow criteria. '
  'Operator promotes individual fields via the funder detail editor.';
