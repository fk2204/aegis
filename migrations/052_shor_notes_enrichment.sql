-- Enrich notes_residual for Shor Capital (§5 of manual).
--
-- Source: Filip's internal MCA Funder Manual §5. Migration 046 inserted
-- the funder row with an empty notes_residual; this migration writes the
-- operator-curated summary so the funder detail page (/ui/funders/{id})
-- surfaces the strategic context (2nd-position-only stacker, fixed-by-
-- tier ISO commission, single-payout structure, default trigger) that
-- the structured columns don't capture.
--
-- Kept under 500 chars for UI readability.
--
-- Idempotent: re-running OVERWRITES notes_residual. If operator has
-- edited via /ui/funders/{id} since the last apply, those edits will be
-- replaced. Same convention as migration 049.

UPDATE funders SET notes_residual = 'Low-rate stacker — 2nd position only. Factor 1.45–1.49 (sell to merchant); ISO commission fixed by tier (1.45=8 pts / 1.47=9 pts / 1.49=10 pts). One-time lump-sum payout 5 business days after funding. 30-day default trigger, ACH debit authorized. Submissions: submissions@shor.capital. Phone: (877) 218-8043. Per manual §5: stacking-friendly, simple economics, no upsell.'
WHERE name = 'Shor Capital';
