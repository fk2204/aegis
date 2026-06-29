-- Migration 091 — operator validation verdicts for shadow-probe detectors.
--
-- Context (Phase 2 / item 3.8 of docs/AEGIS_COMPLETE_BUILD_PLAN.md). The
-- text-layer probe v2 (parser/metadata.py::_probe_text_layer_v2_shadow)
-- ships in shadow mode per CLAUDE.md "Decision-boundary changes —
-- deliberate + shadow-first": when v2 disagrees with the live probe on
-- whether to route to vision, the pipeline appends a
-- ``[SHADOW] text_layer_probe_v2_disagrees: ...`` entry to
-- ``documents.all_flags`` but the live routing decision is unchanged.
--
-- To flip the probe from shadow to live the operator needs a corpus of
-- adjudicated cases. This table is that corpus: one row per (operator,
-- document, probe) verdict where the operator has looked at the original
-- PDF and decided whether v2's stricter routing call would have been
-- correct.
--
-- Schema
-- ------
--  * ``probe_name`` is TEXT (not an enum) so a future probe — say
--    ``page_layer_probe_v3`` — can reuse the same table without a
--    migration. Today the only value is ``text_layer_probe_v2``.
--  * ``operator_verdict`` is a two-value CHECK so the loose "did the
--    operator say v2 was right" semantics are pinned to the schema. The
--    UI only ever submits these two strings.
--  * ``UNIQUE (document_id, probe_name, operator_email)`` makes the
--    verdict idempotent per operator. A second click on the same row
--    from the same operator is a no-op on the schema layer (the route
--    handler returns 200 + the unchanged row).
--  * ``ON DELETE CASCADE`` on document_id mirrors the discipline
--    documents_indexes (migration 090) uses elsewhere: a deleted
--    document drags its derived rows with it; orphan verdicts on a
--    purged doc would be misleading.
--
-- Read paths
-- ----------
--  * ``count_verdicts(probe_name)`` powers the "ready to flip" banner
--    in the admin UI: ≥10 v2_correct AND ≤2 v1_correct flips the page
--    to the "request operator confirmation" affordance.
--  * ``list_unreviewed_disagreements(probe_name)`` joins back to the
--    documents.all_flags shadow disagreements and filters out those
--    already verdicted by the requesting operator.
--
-- NOT applied. Awaiting operator approval per
-- ``.claude/rules/operating-principles.md`` Rule 1 (production data
-- writes require explicit per-action approval).

BEGIN;

CREATE TABLE IF NOT EXISTS probe_review_verdicts (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id      UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    probe_name       TEXT NOT NULL,
    operator_verdict TEXT NOT NULL
      CHECK (operator_verdict IN ('v2_correct', 'v1_correct')),
    operator_email   TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (document_id, probe_name, operator_email)
);

CREATE INDEX IF NOT EXISTS probe_review_verdicts_probe_idx
  ON probe_review_verdicts (probe_name);

COMMENT ON TABLE probe_review_verdicts IS
  'Per-operator verdicts on shadow-probe disagreements. Drives the '
  'corpus that an operator validates a shadow detector against before '
  'flipping it to live. Currently feeds text_layer_probe_v2; future '
  'shadow probes reuse the same table by setting a fresh probe_name.';

COMMIT;
