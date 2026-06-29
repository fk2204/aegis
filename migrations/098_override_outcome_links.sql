-- Migration 098 — override_outcome_links junction table.
-- Connects an operator override (decision flywheel) to the eventual
-- deal outcome. Powers the override-accuracy report at
-- /ui/overrides/summary: "when AEGIS said manual_review/decline but
-- operator overrode, what % actually funded?"
--
-- Auto-linked from the deal-outcome write path: when an outcome is
-- recorded for a merchant, every existing override on that merchant
-- gets a link row inserted (idempotent UNIQUE constraint absorbs
-- re-runs). Link failures are audited but do NOT roll back the
-- outcome insert — the outcome row is the operator-visible source of
-- truth, the link is a derived flywheel artifact.

CREATE TABLE IF NOT EXISTS override_outcome_links (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    override_id uuid NOT NULL REFERENCES overrides(id) ON DELETE CASCADE,
    outcome_id  uuid NOT NULL REFERENCES deal_outcomes(id) ON DELETE CASCADE,
    linked_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (override_id, outcome_id)
);
CREATE INDEX IF NOT EXISTS idx_override_outcome_override
    ON override_outcome_links (override_id);
CREATE INDEX IF NOT EXISTS idx_override_outcome_outcome
    ON override_outcome_links (outcome_id);
