-- Migration 032 — analyses.pattern_analysis jsonb
--
-- Stage 2 of the chip-evidence-expansion work (see
-- ~/.claude/projects/.../project-post-drilldown-backlog.md item #6).
-- The dossier currently re-runs `analyze_patterns()` on every render
-- because the parser's `PatternAnalysis` isn't persisted; the new
-- Today / Review Queue chip expanders would inherit that cost
-- (and pay it across 8+ merchant cards per render).
--
-- This column caches the serialized PatternAnalysisDTO from the parser
-- pipeline so card-builder reads are O(1) per chip-evidence lookup.
-- Source of truth for scoring stays in code — the scorer always
-- recomputes from current transactions. The stored DTO drives only
-- the chip-render path.
--
-- Shape: jsonb object matching `aegis.parser.patterns.PatternAnalysisDTO`:
--   {
--     "schema_version": 1,
--     "patterns": [
--       {"code": "...", "severity": ..., "detail": "...", "source_ids": ["uuid", ...]},
--       ...
--     ],
--     "mca_positions": [{"funder_label": "...", "daily_equivalent": "123.45", ...}, ...],
--     "has_kiting": false,
--     "paydown_suspected": false,
--     "counterparty_signals": {...} | null,
--     "payroll_present": false,
--     "acceleration_clause_triggered": false,
--     "unauthorized_withdrawal_dispute": false,
--     "ai_generated_score": 0
--   }
--
-- Decimals serialize as strings (same convention as monthly_breakdown
-- per migration 009) so Pydantic round-trips them losslessly. UUIDs
-- serialize as strings.
--
-- Nullable, no backfill. Existing rows stay NULL — card builders fall
-- back to rendering the chip without an expander (current behavior).
-- New uploads from chunk 2 onward populate the column. A later
-- one-off backfill script could re-run analyze_patterns() per legacy
-- document, but the operator confirmed deferring that indefinitely.
--
-- GIN index for future analytics queries ("find every analysis whose
-- patterns include unauthorized_withdrawal_dispute"). Not load-bearing
-- in stage 2, but adding it after the column has data means a full
-- table rewrite — cheaper to add now while the column is empty.

ALTER TABLE public.analyses
  ADD COLUMN IF NOT EXISTS pattern_analysis JSONB;

COMMENT ON COLUMN public.analyses.pattern_analysis IS
  'Cached PatternAnalysisDTO from parser pipeline. NULL on rows
   analyzed before migration 032 / stage 2 chunk 2; populated on
   every new analysis after chunk 2 ships. Read by Today / Review
   Queue card builders and (post-stage-2 cleanup) the dossier
   renderer to avoid re-running analyze_patterns() on every render.';

CREATE INDEX IF NOT EXISTS analyses_pattern_analysis_gin
  ON public.analyses USING gin (pattern_analysis jsonb_path_ops);
