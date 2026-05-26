---
paths:
  - "src/aegis/compliance/**"
  - "docs/compliance/**"
  - "tests/compliance/**"
---

# AEGIS Compliance Rules

> **SCOPE NOTE (2026-05-25).** Commera Capital is a pure ISO broker. AEGIS is an internal pre-screening tool — it does not generate merchant-facing disclosures, does not enforce per-state CFDL gates at the broker layer, and is not the regulatory primary actor. State CFDL disclosures, renewal disclosures, COJ / auto-debit / forum overlays, and prescribed-form template management are funder concerns. This rule file governs internal-use-only compliance code: dossier hygiene, audit-log discipline, and historical state metadata. The Tier 1 / Tier 2 / Tier 3 framework in `docs/compliance/states.yaml` is preserved as informational metadata only — it does not drive runtime broker behavior. See the SCOPE NOTE at the top of `docs/AEGIS_MASTER_PLAN.md` for the full framing.

Auto-loads when editing internal compliance code, dossiers, or related tests.

---

## Non-negotiable rules

- **Don't fill state regulation fields from prior knowledge.** Every statute reference in code or dossiers cites a verifiable source: bill number, link, effective date, verification date. The TS predecessor invented Tier 1 entries from training data and produced fictional bill numbers; do not repeat that mistake even though state regulations no longer drive runtime behavior.
- **Audit-log writes are required for every state change.** Any mutation of compliance-adjacent data (dossier update, state-metadata edit, override capture, decision snapshot) records to `audit_log` via `record()`. **Audit-write failures FAIL the operation** — never silently log-and-continue.
- **Decisions are immutable once captured.** Decision snapshots are operational-quality and internal-review artifacts; never edit a row in-place. Corrections are appended as new rows referencing the prior decision; the prior row stays.

---

## Authoritative compliance research

Lives in `docs/compliance/`. Used as informational reference, not runtime gating.

- **Read first:** `docs/compliance/CORRECTIONS_2026-05-08.md` — audit log of verification corrections
- **Master matrix:** `docs/compliance/15_aegis_compliance_posture.md` — obligation matrix (read as funder-side context, not broker-side enforcement)
- **Per-state dossiers:** `docs/compliance/NN_<state>.md`

When a dossier conflicts with older state research notes elsewhere in the repo, **the dossier wins.** Older notes may contain superseded information.

---

## States.yaml access patterns

`docs/compliance/states.yaml` is loaded at boot by `aegis.compliance.state_matrix` and is preserved as informational metadata. Reading from it is fine; documentation refresh is fine. **Do not add runtime branches that gate broker behavior on tier values.** Per-state compliance gating belongs to the funder.

---

## Commit messages for compliance work

When updating dossiers or compliance metadata, cite the specific dossier and section in the commit body. Future Claude (or an internal reviewer) should be able to trace where a fact came from. The cite is for internal traceability, not for a regulator-facing audit trail.
