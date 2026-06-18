-- Migration 064 — merchants context fields (Feature D).
--
-- Adds four free-text columns to ``merchants`` that get injected into
-- the Bedrock extraction prompt as an "MERCHANT CONTEXT" block so the
-- LLM can disambiguate ambiguous statement layouts / transactions using
-- what we already know about the deal:
--
--   * deal_context           — operator-written. Edited via a dossier
--     Context-panel textarea. Free-form: pricing context, broker
--     warnings, processor quirks. NULL until the operator types
--     something.
--   * close_lead_description — verbatim Close Lead ``description``
--     field. Auto-refreshed on every Close webhook for the lead
--     and on operator "Refresh Close fields" button click. NULL when
--     the Close lead has no description yet.
--   * close_notes_summary    — concatenated bodies of the most recent
--     5 Close Note activities for the lead, joined by ``\n---\n``.
--     PII-bearing: Close notes often paraphrase transaction details,
--     name owners, etc. Storage is acceptable per CLAUDE.md (database
--     is the funder-review surface); the logger masks the field name
--     so it never lands in stderr / journald.
--   * close_call_transcripts — concatenated ``note`` field of the most
--     recent 3 Close Call activities. Same PII posture as the notes
--     summary; same masking on the log side.
--
-- All four columns are plain ``text NULL`` — no length CHECK because
-- the operator's broker-call transcripts can run long, and the prompt
-- builder treats empty / NULL strings as "omit this line" rather than
-- "render an empty section". Display contract is documented on each
-- column comment.

ALTER TABLE merchants
  ADD COLUMN deal_context           TEXT NULL,
  ADD COLUMN close_lead_description TEXT NULL,
  ADD COLUMN close_notes_summary    TEXT NULL,
  ADD COLUMN close_call_transcripts TEXT NULL;

COMMENT ON COLUMN merchants.deal_context IS
  'Operator-written free-form context about the deal. Surfaces on the '
  'dossier Context panel (editable textarea). Injected into the Bedrock '
  'extraction prompt under "Operator notes" so the LLM can disambiguate '
  'transaction descriptions / merchant identity. NULL = never written.';

COMMENT ON COLUMN merchants.close_lead_description IS
  'Verbatim Close Lead ``description`` field, refreshed on every Close '
  'webhook for this lead AND on operator "Refresh Close fields" click. '
  'Read-only on the dossier. Injected into the extraction prompt under '
  '"Close lead description". NULL = lead has no description in Close '
  'or refresh has not run yet.';

COMMENT ON COLUMN merchants.close_notes_summary IS
  'Concatenated bodies of the most recent 5 Close Note activities for '
  'the lead, joined by ``\n---\n``. Refreshed on every Close webhook '
  'and on operator "Refresh Close fields" click. PII-bearing — never '
  'log. The logger masks the column name as a known PII key; audit '
  'rows for refresh events store only the count, never the bodies.';

COMMENT ON COLUMN merchants.close_call_transcripts IS
  'Concatenated ``note`` field of the most recent 3 Close Call '
  'activities for the lead, joined by ``\n---\n``. Same refresh + PII '
  'posture as ``close_notes_summary``. Injected into the extraction '
  'prompt under "Recent call summaries" so the LLM can correlate '
  'call-derived merchant facts with statement evidence.';
