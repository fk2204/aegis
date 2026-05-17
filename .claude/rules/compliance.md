---
paths:
  - "src/aegis/compliance/**"
  - "docs/compliance/**"
  - "tests/compliance/**"
---

# AEGIS Compliance Rules

Auto-loads when editing compliance code, dossiers, or compliance tests. Legally load-bearing — incorrect state regulation data creates regulatory exposure.

---

## Non-negotiable rules

- **NEVER add or modify a state in `compliance/states.py` without citing the actual statute** in a comment with: link, bill number, effective date, verification date.
- **The disclosure HTML for each state MUST match the template prescribed by that state's regulator** (CA DFPI, NY DFS, VA SCC, etc.). Generic templates are not acceptable.
- **If a state's prescribed template hasn't been added**, the disclosure endpoint MUST raise `DisclosureTemplateMissing`. NEVER fall back to a generic disclosure.
- A snapshot test exists for each state's disclosure output, locking the format. Updating a snapshot is a deliberate decision with a comment explaining why.

---

## The TS predecessor's mistake — don't repeat it

The TS version invented Tier 1 entries from prior knowledge, producing fictional bill numbers. We do not repeat that. Code is NEVER allowed to fill state regulation fields from prior knowledge.

---

## Authoritative compliance research

Lives in `docs/compliance/`.

- **Read first:** `docs/compliance/CORRECTIONS_2026-05-08.md` — audit log of verification corrections
- **Master matrix:** `docs/compliance/15_aegis_compliance_posture.md` — obligation matrix across all states + federal
- **Per-state dossiers:** `docs/compliance/NN_<state>.md`

When a dossier conflicts with older state research notes elsewhere in the repo, **the dossier wins.** Dossiers dated 2026-05-07 with verification pass 2026-05-08; older notes predate this research and may contain superseded information.

---

## Promotion workflow (Tier 3 → Tier 1 or Tier 2)

This sequence is non-negotiable for Tier 1 promotions because the prescribed form is regulator-mandated.

1. Operator supplies source material: bill text, citation URL, verbatim excerpt (≤500 words), effective date, prescribed form (Tier 1), CoJ rule with sub-citation.
2. Write a dossier under `docs/compliance/NN_<state>.md` with a confidence assessment.
3. Operator reviews the dossier against the source material.
4. Update `src/aegis/compliance/states.py` with the new `Tier1Regulation` or `Tier2Regulation`. For Tier 1, add the Jinja template at `compliance/templates/{state}_{bill}.html.j2` matching the regulator's prescribed form line-by-line.
5. Add a snapshot test under `tests/snapshots/` locking the rendered HTML.
6. Boot validator (`validate_states_table()`) must still pass.
7. Operator reviews template against the regulator's prescribed form.
8. Commit message cites the dossier, e.g. `feat(compliance): promote CA from Tier 3 to Tier 1 per docs/compliance/01_california.md`.

---

## Commit messages for compliance work

Always cite the specific dossier and section. This makes the audit trail searchable. Without the citation, future Claude (or a regulator's counsel) can't trace where the rule came from.