-- Migration 088 — Postgres function for count_documents_by_status.
--
-- Replaces the Python-side counting in
-- aegis.storage.SupabaseDocumentRepository.count_by_parse_status which
-- pulled up to 10,000 documents.parse_status rows over the wire and
-- counted them in Python (~1 second on the prod box at 190 docs;
-- scales linearly with row count).
--
-- A single GROUP BY on the Postgres side returns one row per distinct
-- parse_status value; the wire payload is < 200 bytes and the query
-- runs in single-digit ms.

CREATE OR REPLACE FUNCTION count_documents_by_status()
RETURNS TABLE(parse_status text, count bigint)
LANGUAGE sql STABLE
AS $$
  SELECT parse_status::text, COUNT(*)::bigint
  FROM documents
  GROUP BY parse_status;
$$;

-- Grant execute to the supabase service_role + authenticated roles so
-- the supabase-py client (which uses the service_role JWT) can call it
-- via .rpc('count_documents_by_status', {}).
GRANT EXECUTE ON FUNCTION count_documents_by_status() TO service_role;
GRANT EXECUTE ON FUNCTION count_documents_by_status() TO authenticated;
