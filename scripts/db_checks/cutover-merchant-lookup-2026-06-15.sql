-- DESCRIPTION: Cutover lookup for two specific merchants — business_name + state. Pre-migration-055 (no industry_choice column).
-- EXPECT_ROWS: 2
--
-- The two merchants are operator-curated cutover anchors. We only
-- read columns guaranteed to exist on prod schema as of 2026-06-15:
-- migration 055 (industry_choice on merchants) has not been applied
-- yet, so referencing it here would 42703 on prod.
SELECT
  id,
  business_name,
  state
FROM merchants
WHERE id IN (
  '7f5146c7-cbf7-4a6d-9e89-f6e6e019a323',
  '5cf4479d-c6ac-4267-a2f7-5e7ef04c1345'
)
ORDER BY id;
