-- DESCRIPTION: Prod health — at least one audit_log row written in the last hour.
-- EXPECT_ROWS_MIN: 1
--
-- This is the cutover-check's "is prod alive?" probe. The heartbeat timers
-- (aegis-heartbeat-web + aegis-heartbeat-worker) plus any operator-driven
-- write should produce audit_log rows continuously. Zero rows in the last
-- hour means either (a) audit_log writes are wedged, (b) the box stopped
-- writing, or (c) clock skew between workstation and DB — all of which
-- the operator must look at before a cutover.
--
-- Read-only by construction (db_verify.py enforces). No PII surfaces in
-- the row count itself.
SELECT 1
FROM audit_log
WHERE created_at > NOW() - INTERVAL '1 hour'
LIMIT 1;
