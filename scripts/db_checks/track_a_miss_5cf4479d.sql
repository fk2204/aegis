-- DESCRIPTION: Triage the one real Track A lookback miss — merchant 5cf4479d / document 49c7d058. business_name + state + breakdown + flags.
-- EXPECT_ROWS: 1
--
-- Operator request 2026-06-15: legacy fraud_score=70 declined, Track A
-- verdict=clean. Pull every input Track A reads (and the legacy
-- pipeline soft-scoring inputs) so we can see whether Track A is
-- correctly clean or genuinely missing a signal.
--
-- Columns referenced exist in DocumentRow / MerchantRow as of HEAD
-- 077266b (post-migration-055; merchants.industry_choice is now safe
-- but we don't need it for this query). fraud_score_breakdown is
-- JSONB so metadata_score / math_score come out of it via ->>.
-- all_flags is text[]; [PATTERN] and [MATH] entries are derived
-- (filtered) into the response rather than read as standalone columns.
SELECT
  m.id            AS merchant_id,
  m.business_name AS merchant_business_name,
  m.state         AS merchant_state,
  d.id            AS document_id,
  d.parse_status  AS document_parse_status,
  d.uploaded_at   AS document_uploaded_at,
  d.fraud_score   AS document_fraud_score,
  -- Per-component scores. NULLIF guards against breakdown missing the key.
  (d.fraud_score_breakdown ->> 'metadata')::int AS metadata_score,
  (d.fraud_score_breakdown ->> 'math')::int     AS math_score,
  (d.fraud_score_breakdown ->> 'pattern')::int  AS pattern_score,
  d.fraud_score_breakdown                       AS fraud_score_breakdown_full,
  -- Pattern flags = entries prefixed with "[PATTERN]" in all_flags.
  -- ARRAY-aggregated so the row stays one record.
  ARRAY(
    SELECT f FROM unnest(d.all_flags) AS f WHERE f LIKE '[PATTERN]%'
  ) AS pattern_flags,
  -- Validation failures = entries prefixed with "[MATH]" in all_flags.
  ARRAY(
    SELECT f FROM unnest(d.all_flags) AS f WHERE f LIKE '[MATH]%'
  ) AS validation_failures,
  -- Metadata flags get their own column on the documents table.
  d.metadata_flags AS metadata_flags,
  d.all_flags      AS all_flags_full
FROM documents d
JOIN merchants m ON m.id = d.merchant_id
WHERE m.id = '5cf4479d-c6ac-4267-a2f7-5e7ef04c1345'
  AND d.id = '49c7d058-3e2a-4554-ad46-f4063146b36e';
