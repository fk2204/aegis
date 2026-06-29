-- Migration 097 — corpus_documents. Training-corpus isolation table.
-- Holds metadata for PDFs the operator ingests via
-- scripts/ingest_training_corpus.py. NO foreign keys to merchants /
-- documents / analyses — the corpus is deliberately disjoint from the
-- live underwriting pipeline. Ingest signals (font drift / creator
-- mismatch / overlay) feed into bank_layout_hints +
-- creator_fingerprint_registry only when the document fired NO fraud
-- signals (clean-statement-only seeding policy).

CREATE TABLE IF NOT EXISTS corpus_documents (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    file_hash               text NOT NULL UNIQUE,
    original_path           text,
    bank_name               text,
    detected_creator        text,
    detected_producer       text,
    page_count              integer,
    has_font_inconsistency  boolean DEFAULT false,
    has_text_overlay        boolean DEFAULT false,
    has_creator_mismatch    boolean DEFAULT false,
    fraud_signals_fired     boolean DEFAULT false,
    ingested_at             timestamptz DEFAULT now(),
    notes                   text
);
CREATE INDEX IF NOT EXISTS idx_corpus_bank ON corpus_documents (bank_name);
CREATE INDEX IF NOT EXISTS idx_corpus_hash ON corpus_documents (file_hash);
COMMENT ON TABLE corpus_documents IS
  'Training corpus — isolated from live pipeline. No FK to merchants/documents.';
