-- 078_llm_costs.sql
--
-- Dedicated per-call Bedrock cost table.
--
-- The existing implementation (migrations 0–077 era) writes a
-- ``bedrock.usage`` row into ``audit_log`` for every call (see
-- ``src/aegis/ops/cost_tracking.py``). That works for the weekly digest
-- but it's awkward to query for per-call-type breakdowns, monthly
-- trends, and per-deal totals because every cell needs a JSON probe.
--
-- 078 adds a relational shape on the side. ``CostTrackingBedrockClient``
-- dual-writes: the audit_log row stays (no breakage for any caller that
-- reads ``bedrock.usage`` today), and a parallel row lands in
-- ``llm_costs`` for the operator UI at ``GET /ui/costs``.

BEGIN;

CREATE TABLE IF NOT EXISTS llm_costs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id uuid NULL REFERENCES merchants(id) ON DELETE SET NULL,
    document_id uuid NULL REFERENCES documents(id) ON DELETE SET NULL,
    model_id text NOT NULL,
    input_tokens integer NOT NULL CHECK (input_tokens >= 0),
    output_tokens integer NOT NULL CHECK (output_tokens >= 0),
    estimated_cost_usd numeric(10, 6) NOT NULL CHECK (estimated_cost_usd >= 0),
    call_type text NOT NULL CHECK (call_type IN (
        'extraction',
        'classification',
        'narrator',
        'business_intel',
        'web_presence',
        'creator_fingerprint'
    )),
    called_at timestamptz NOT NULL DEFAULT now()
);

-- Lookup pattern: per-merchant in a window.
CREATE INDEX IF NOT EXISTS idx_llm_costs_merchant_called_at
    ON llm_costs (merchant_id, called_at DESC);

-- Lookup pattern: per-document drilldown.
CREATE INDEX IF NOT EXISTS idx_llm_costs_document_called_at
    ON llm_costs (document_id, called_at DESC);

-- Lookup pattern: monthly trend aggregation.
CREATE INDEX IF NOT EXISTS idx_llm_costs_called_at
    ON llm_costs (called_at DESC);

-- Lookup pattern: cost by call_type within a window.
CREATE INDEX IF NOT EXISTS idx_llm_costs_call_type_called_at
    ON llm_costs (call_type, called_at DESC);

COMMENT ON TABLE llm_costs IS
    'Per-call Bedrock cost ledger. Dual-written alongside audit_log bedrock.usage rows.';
COMMENT ON COLUMN llm_costs.estimated_cost_usd IS
    'Computed via cost_tracking.compute_cost_usd — quantized to 6 decimal places to retain sub-cent precision.';
COMMENT ON COLUMN llm_costs.call_type IS
    'Coarse category for the operator UI. Set explicitly when the wrapper has known intent; inferred from method name otherwise.';

COMMIT;
