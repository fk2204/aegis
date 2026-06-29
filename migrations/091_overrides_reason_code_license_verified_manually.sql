-- Migration 091 — align overrides.reason_code CHECK constraint with the
-- Python ReasonCode Literal.
--
-- Source of truth: ``src/aegis/compliance/overrides.py`` ``ReasonCode``.
-- The Literal has been ahead of the CHECK constraint since
-- ``license_verified_manually`` was added to the application code
-- (Phase 10 licensing flow); any INSERT carrying that reason currently
-- fails at the DB boundary with a CHECK violation, blocking the
-- operator's "license verified out-of-band" override path.
--
-- The constraint name follows Postgres's default for CHECK constraints
-- declared inline on a column: ``<table>_<column>_check``. Migration
-- 017 (overrides table create) did NOT name the CHECK explicitly, so
-- the auto-generated name is ``overrides_reason_code_check``.
--
-- Note on table naming: build plan §11.4 refers to "dossier_overrides"
-- but the canonical table — created in migration 017 and extended in
-- migration 072 for the dossier flow — is ``overrides``. Both the
-- legacy ``record_override`` path and the dossier ``record_dossier_override``
-- path write to this same table.
--
-- DRIFT GUARD: ``tests/compliance/test_migrations.py`` enforces that
-- this migration's value list matches the Python Literal exactly. If
-- you add a new ReasonCode value in overrides.py, ship a paired
-- migration here in the same commit — the drift test fails CI
-- otherwise.

ALTER TABLE overrides
  DROP CONSTRAINT IF EXISTS overrides_reason_code_check;

ALTER TABLE overrides
  ADD CONSTRAINT overrides_reason_code_check
  CHECK (reason_code IN (
    'score_too_conservative',
    'score_too_aggressive',
    'funder_specific_fit',
    'merchant_context_external',
    'data_quality_concern',
    'pattern_false_positive',
    'pattern_false_negative',
    'license_verified_manually',
    'gut'
  ));
