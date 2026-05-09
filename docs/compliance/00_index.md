# AEGIS Compliance Research — Top 5 States Summary

**Researched: 2026-05-07** by Claude.

This index summarizes the five state dossiers in this folder. Read this first, then dive into the per-state file you're working on.

---

## At-a-glance comparison

| Dimension | California | New York | Florida | Georgia | Illinois |
|---|---|---|---|---|---|
| **AEGIS Tier** | 1 | 1 | 1 | 1 | **2** (not Tier 1) |
| **Disclosure law** | SB 1235 + SB 362 | CFDL Art. 8 | FCFDL HB 1353 | SB 90 | None enacted |
| **Effective date** | 2022-12-09 | 2023-08-01 | 2024-01-01 | 2024-01-01 | n/a |
| **Threshold** | $500K | $2.5M | $500K | $500K | n/a |
| **APR required** | Yes (actuarial) | Yes (actuarial w/ tolerance) | **No** | Yes | n/a |
| **Prescribed table form** | Yes (9 rows × 3 cols) | Yes (10 rows × 3 cols) | No (content only) | No (content only) | n/a |
| **Re-disclose APR on every quote** | Yes (SB 362) | Yes (built into reg) | No | No | n/a |
| **CoJ permitted** | **No** (banned 2023) | Conditional (NY residents only) | **No** (banned) | Yes | Yes (commercial only) |
| **Broker comp disclosure required** | No | **Yes** (§ 600.21(f)) | No | No | n/a |
| **Broker advance fees prohibited** | n/a (different regime) | n/a | Yes | Yes | Possibly under Loan Brokers Act |
| **Private right of action** | No (DFPI/CFL) | No (NY DFS) | No (AG only) | No (AG only) | n/a |
| **Disclosure transmission record retention** | 4 years | 4 years | n/a | n/a | n/a |

---

## Critical distinctions to encode in AEGIS

### CoJ matrix
| State | Rule | AEGIS action |
|---|---|---|
| California | Banned (§ 1132) | Hard fail any CA deal where funder requires CoJ |
| New York | NY residents only (CPLR § 3218) | Allow if merchant is NY resident; block if out-of-state merchant + NY-filed CoJ scheme |
| Florida | Banned (§ 55.05) | Hard fail any FL deal where funder requires CoJ |
| Georgia | Allowed | No state-level block; funder reputation as soft signal |
| Illinois | Allowed for commercial | No state-level block for commercial MCA |

### Disclosure obligation matrix
| State | Form-prescribed | APR | Operational impact for AEGIS |
|---|---|---|---|
| California | Yes (10 CCR § 914) | Yes | Build `ca_sb1235.html.j2` matching 9-row table exactly |
| New York | Yes (23 NYCRR § 600.6) | Yes | Build `ny_cfdl.html.j2` matching 10-row table; broker comp letter required |
| Florida | No | No | Build `fl_fcfdl.html.j2` with 6 content items; flexible format |
| Georgia | No | Yes | Build `ga_sb90.html.j2` with 7 content items; APR required |
| Illinois | n/a | n/a | Generic acknowledgment receipt only; quarterly status check |

### Threshold-based scoping
- **Florida + Georgia + California:** $500K threshold — most MCA deals fall under disclosure obligation.
- **New York:** $2.5M threshold — even larger MCA deals require disclosure.
- **AEGIS implication:** when scoring, check `state_threshold_applies(state, financing_amount)` before generating disclosure. Above threshold, no disclosure required.

### Re-disclosure-on-pricing-mention
**California (SB 362, eff 2026-01-01)** and **New York (built into 23 NYCRR Part 600)** both require: every time AEGIS or a funder communicates a charge, pricing metric, or financing amount, the APR must also be stated. This is an architectural rule for the dashboard's pricing display logic, the submission package generator, and any quote emails. **Florida and Georgia do not have this rule.**

---

## What this means for AEGIS work order

Recommended Phase 4 implementation order in AEGIS:

1. **California** (highest regulatory risk + largest body of source material). Build template `ca_sb1235.html.j2`, snapshot test, CoJ hard-fail rule, transmission log table.
2. **New York** (second-highest risk, structurally similar to CA + adds collateral row). Build template `ny_cfdl.html.j2`, snapshot test, broker comp letter generator, conditional CoJ logic.
3. **Florida** (highest MCA volume nationally). Simpler template, no APR, hard-fail CoJ rule.
4. **Georgia** (active law, simpler regime). Template with APR but flexible format.
5. **Illinois** (Tier 2 only). Generic receipt + quarterly status reminder.

Then expand to next states as deal flow indicates.

---

## Files in this package

- `01_california.md` — full CA dossier with CoJ ban and § 952 transmission rules resolved.
- `02_new_york.md` — full NY dossier with CFDL § 600.6 form, conditional CoJ rule, broker comp letter requirement.
- `03_florida.md` — full FL dossier with content-based disclosure (no APR), § 55.05 CoJ ban, broker advance-fee prohibition.
- `04_georgia.md` — full GA dossier with APR-required content disclosure, CoJ allowed, broker advance-fee prohibition.
- `05_illinois.md` — IL dossier flagging that no MCA disclosure law is enacted; Tier 2 placement; pending legislation tracker.

---

## Verification checklist before paste to Claude Code

Before promoting any of these states out of Tier 3, confirm:

- [ ] **CA:** Open SB 1235 bill, 10 CCR § 914 reg, SB 362 bill, § 1132 CoJ statute. Confirm citations match dossier. (10 min)
- [ ] **NY:** Open NY Fin Services Law § 801-811 statute, 23 NYCRR § 600.6 reg, CPLR § 3218 CoJ. Confirm. (12 min)
- [ ] **FL:** Open Fla. Stat. § 559.9613 disclosure section, HB 1353 enrolled text, § 55.05 CoJ. Confirm. (8 min)
- [ ] **GA:** Open SB 90 bill text, O.C.G.A. § 9-12-18 CoJ. Confirm. (8 min)
- [ ] **IL:** Open SB 2234 bill status (confirm sine die), HB 3477 status, 735 ILCS 5/2-1301. Confirm IL has no CFDL. (6 min)

**Total verification time: ~45 minutes once.** Spread it across two evenings if needed.

---

## Confidence assessment overall

- **Highest confidence (paste-ready after URL verification):** CA, NY, FL, GA disclosure framework + CoJ rules.
- **Lower confidence:** GA penalty dollar amounts (verify before relying on numbers in code), NY APR tolerance exact threshold (1/8% vs 1/4% per § 600.5).
- **Action required, not just verification:** IL Loan Brokers Act applicability (one phone call to IL Secretary of State or short attorney consult).

For each state's full citations, prescribed form structure, operational rules, and Python code for `compliance/states.py`, see the per-state file.
