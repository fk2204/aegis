# Fraud Signal Catalog — Display Consolidation

**Scope:** Presentation-only legibility fix on the dossier surface. **Zero decision changes.** Every signal still fires; every severity, hard-decline threshold, score formula, and tier outcome is unchanged. What changes is **how the dossier RENDERS the signals**, not what the engine COMPUTES.

This document captures the consolidation map. Two prior catalog documents — a 55-signal as-built audit (Bucket A decision contradictions + Bucket B legibility issues) and a tampering-signal triage — provide the full surface inventory. They live in sibling worktrees and are referenced at the end; this document records the code-level consolidation actually shipped.

---

## Overview

An underwriter reading the dossier cold should see each fraud finding **once**, in worker-language English, with the dollar amounts, dates, and source rows that triggered it. Before this consolidation, the dossier presented the same underlying fact up to four different ways: as a pattern card (with a plain-English description), as a soft-signal aggregate card (without one), as a hard-decline reason (raw identifier string with no description), and sometimes also as a soft-concern line (also raw identifier). A worker reading "customer_concentration_severe" on the soft-concerns line and a "Customer Concentration" card 200 pixels below would not know they describe the same merchant's same dominant customer — they look like two independent findings.

The legibility work landing here does three things:

1. **Consolidate duplicate cards.** When the `customer_concentration` Pattern detector fires, the dossier no longer also renders the aggregate-derived `top_counterparty_concentration` soft-signal card — the pattern card carries severity banding, source-row drill-down, and the top-payee label in its detail string already. The aggregate card was duplicative.

2. **Suppress dual-tier patterns when the hard-decline list already shows them.** `acceleration_clause_triggered` and `unauthorized_withdrawal_dispute` fire BOTH as pattern severity contributors AND as Phase 9 hard-decline reasons. Before this change, the dossier showed each twice — once as a soft severity-banded card below, once as a raw identifier in the verdict's hard-decline list above. Now, when the scorer attaches the hard-decline reason, the pattern card is suppressed at the DISPLAY layer; the hard-decline line carries the rich worker copy. The scoring layer is unchanged — the pattern still contributes to `patterns.fraud_score` and the hard-decline still fires.

3. **Humanize hard-decline reasons and soft concerns.** The verdict block used to render raw identifier strings like `acceleration_clause_triggered: OnDeck …` directly. Workers had no description, no context, no "what does this mean and what do I do" hook. Two new copy maps (`HARD_DECLINE_COPY`, `SOFT_CONCERN_COPY`) in `src/aegis/web/_pattern_cards.py` carry plain-English title + description for every code `score.py` emits, mirroring the discipline `PATTERN_COPY` already provides for pattern cards.

The engine layer is untouched. The same deal passes / fails before and after this work. The display is what changed.

---

## Consolidation map

Three concrete consolidations. Each table shows what was previously rendered, what is rendered now, and which file owns the new canonical view.

### Consolidation 1 — Customer concentration (4 surfaces → 1 card + 1 humanized line)

The v2 catalog's Bucket B.1 identified four surfaces for the same underlying fact: the top counterparty's share of revenue. The four signals all still fire; the dossier now renders them as a single canonical card plus an optional humanized soft-concern line.

| Signal | Source | Before | After |
|---|---|---|---|
| `customer_concentration` (pattern) | `patterns.py:1075-1080`; statement-derived | Pattern card in §3 with title "Customer Concentration", severity banding, source rows | **Canonical card** in §3. Renders with severity banding + drill-down + the top-payee + share% in the detail string |
| `top_counterparty_concentration:{pct}%_({payee})` (aggregate flag) | `aggregate.py:252`; same statement-derived fact | A second card in §3 ("Customer concentration · 43% top payee") sourced from `soft_signals.customer_concentration` | **Suppressed when the pattern card is present.** Template gate: `{% if soft_signals.customer_concentration and not has_concentration_pattern %}` |
| `top_counterparty_*pct` (soft-score factor) | `score.py:421-426`; same statement-derived fact, tiered 30/40/60 | Numeric delta in `score_result.breakdown` only; not on the dossier verdict | Unchanged — never appeared on the dossier list anyway |
| `customer_concentration_severe` (soft concern) | `score.py:338-339`; operator-entered field >60% | Raw identifier `customer_concentration_severe` in the verdict's soft-concerns list | **Humanized** via `SOFT_CONCERN_COPY` — title "Operator-flagged customer concentration", description points the worker to the pattern card |

**Canonical card:** the pattern card in `src/aegis/web/_pattern_cards.py` (`PATTERN_COPY["customer_concentration"]`), rendered through the existing `{% for card in pattern_cards %}` loop. Aggregate suppression sits in `merchant_detail_dossier.html.j2` at the soft-signals block.

**Worker net effect:** ONE card per concentration fact, with severity + drill-down + top-payee + share%. Operator-entered overlap shows as ONE humanized soft-concern line that explicitly cross-references the card. Down from up to three cards.

### Consolidation 2 — Payroll status (3 surfaces → 1 card per state)

The v2 catalog's Bucket B.6 identified three names for "payroll status." The three signals describe DIFFERENT states of the same question ("is this merchant running payroll?"), so they don't always overlap on a single dossier — but when the absent-state fires it does so under three names that look like three findings.

| Signal | Source | Before | After |
|---|---|---|---|
| `payroll_absent` (pattern) | `patterns.py:1364`; severity 10 | Pattern card "Payroll Absent" in §3, severity banding | **Canonical card** when payroll is absent. Unchanged |
| `payroll_present_detector` (soft-score factor) | `score.py:451`; +4 | Numeric delta in breakdown only | Unchanged — never on the dossier verdict |
| `payroll_absent_high_revenue` (soft concern) | `score.py:454`; display-only soft_concern | Raw identifier `payroll_absent_high_revenue` in soft-concerns list | **Humanized** via `SOFT_CONCERN_COPY` — title "Payroll absent at revenue scale", description points to the pattern card |
| `payroll_cadence:{cadence}_{pct}%_of_revenue` (aggregate flag) | `aggregate.py:298`; PRESENT-state signal | Card "Payroll cadence" in §3 from `soft_signals.payroll_cadence` | Unchanged — the absent-state pattern and the present-state aggregate are mutually exclusive on any given deal, so they're not duplicating |

**Canonical card:** when payroll IS detected, the soft-signals `payroll_cadence` card. When payroll IS absent, the `payroll_absent` pattern card. The soft concern code is now humanized so the worker reads "Payroll absent at revenue scale — see the Payroll Absent pattern finding below" instead of `payroll_absent_high_revenue` verbatim.

**Worker net effect:** ONE card describing the payroll status (present-cadence OR absent), with the humanized soft-concern line as a cross-reference. Down from two cards + a raw identifier.

### Consolidation 3 — Acceleration / withdrawal dispute (2 lines → 1 humanized hard-decline line)

The v2 catalog's Bucket B.7 identified the dual-tier presentation: these Phase 9 signals fire BOTH as pattern severity contributors AND as hard-decline reasons.

| Signal | Source | Before | After |
|---|---|---|---|
| `acceleration_clause_triggered` (pattern) | `patterns.py:1252`; severity 50 | Pattern card in §3 with rich description | **Suppressed** when `score_result.hard_decline_reasons` carries the same code. Controlled by `PATTERN_CODE_BY_HARD_DECLINE_REASON` in `_pattern_cards.py` |
| `acceleration_clause_triggered` (hard-decline reason) | `score.py:194-195` | Raw identifier on the verdict's hard-decline list | **Canonical line.** Humanized via `HARD_DECLINE_COPY` — title "Funder acceleration on prior MCA", full description |
| `unauthorized_withdrawal_dispute` (pattern) | `patterns.py:1196`; severity 35 | Pattern card in §3 | **Suppressed** when the equivalent `unauthorized_withdrawal_dispute_active` hard-decline reason fires |
| `unauthorized_withdrawal_dispute_active` (hard-decline reason) | `score.py:196-197` | Raw identifier on hard-decline list | **Canonical line.** Humanized via `HARD_DECLINE_COPY` — title "Active unauthorized-withdrawal dispute" |

Note the naming gap between the pattern code (no suffix) and the hard-decline reason code (`_active` suffix). `PATTERN_CODE_BY_HARD_DECLINE_REASON` bridges this explicitly so the suppression catches both names.

**Canonical line:** the humanized hard-decline line at the top of the verdict block. The pattern still emits its severity contribution into `patterns.fraud_score` upstream; only the duplicate DISPLAY card is hidden.

**Worker net effect:** ONE authoritative line — the hard-decline banner — with worker-language copy and the source-row evidence available on the stacking card or the source documents. Down from two render sites.

---

## Card-entry additions to `_pattern_cards.py`

The `PATTERN_COPY` map already carried 19 high-quality plain-English entries from prior work — covering every Pattern code currently emitted by `aegis.parser.patterns`. The existing regression test (`test_every_emitted_pattern_code_has_copy_or_is_rendered_elsewhere`) enforces this contract. Two adjacent maps were added in this round:

**`HARD_DECLINE_COPY`** — humanizes every hard-decline reason `score.py:_check_hard_declines` emits. 17 entries; one per emitted reason. Confidence-tagged for catalog-doc visibility:

| Code | Title | Confidence |
|---|---|---|
| `ofac_sanctions_match` | OFAC sanctions match | confident |
| `stacking_exceeds_limit` | Stacking exceeds limit | confident |
| `debt_to_revenue_exceeds_40pct` | Debt-to-revenue exceeds 40% | confident |
| `fraud_score_critical` | Fraud score critical | confident |
| `incremental_pdf_saves` | Incremental PDF saves | confident |
| `revenue_below_minimum` | Revenue below minimum | confident |
| `industry_excluded` | Industry excluded | confident |
| `days_negative_gt_15` | More than 15 negative days | confident |
| `nsf_count_gte_10` | 10 or more NSF events | confident |
| `returned_ach_gt_5` | More than 5 returned ACHs | confident |
| `tib_under_3_months` | Business under 3 months old | confident |
| `validation_failed_manual_review_required` | Validation failed — manual review | confident |
| `prior_default` | Defaulted on prior MCA | confident |
| `dscr_below_1` | Debt-service coverage below 1.0 | confident |
| `acceleration_clause_triggered` | Funder acceleration on prior MCA | confident |
| `unauthorized_withdrawal_dispute_active` | Active unauthorized-withdrawal dispute | confident |
| `bank_statement_tampering_confirmed` | Bank statement tampering confirmed | best_guess (this hard-decline reason is unreachable today — see the tampering-triage catalog) |

**`SOFT_CONCERN_COPY`** — humanizes every soft concern `score.py` attaches that surfaces on the verdict. 9 entries. The `ai_generated_statement_*` triplet is marked `best_guess` because the underlying composite is opaque — see Bucket B.5 in the v2 catalog.

| Code | Title | Confidence |
|---|---|---|
| `soft_score_below_threshold` | Composite score below tier floor | confident |
| `missing_time_in_business` | Time in business missing | confident |
| `missing_credit_score` | Credit score missing | confident |
| `customer_concentration_severe` | Operator-flagged customer concentration | confident |
| `top_5_expense_concentration` | Top-5 expense concentration | confident |
| `payroll_absent_high_revenue` | Payroll absent at revenue scale | confident |
| `ai_generated_statement_strong` | Statement looks AI-generated (strong) | best_guess |
| `ai_generated_statement_medium` | Statement looks AI-generated (medium) | best_guess |
| `ai_generated_statement_weak` | Statement looks AI-generated (weak) | best_guess |

`best_guess` entries are drafted from the v2 catalog's Bucket B.2 plain-English drafts. They give a worker something readable to act on today; an operator-review pass should re-tune the language once a cohort of real deals has run through.

`fraud_cluster_triangulated` is intentionally NOT in `PATTERN_COPY` — the pipeline emits it as a flag (`pipeline.py:394`), not a `Pattern(...)`, so it never reaches `build_pattern_cards`. It surfaces through the existing `_flag_labels.py` humanizer on chips elsewhere in the app (Today queue, Review queue), where it already has copy ("Fraud cluster triangulated" + the cluster-signal expander). No new card entry needed in this round.

---

## What still emits but no longer renders separately

| Code | Still emits via | Now folds into |
|---|---|---|
| `top_counterparty_concentration:{pct}%_({payee})` aggregate flag | `aggregate.py:252`. Persisted on `documents.all_flags` for chip categorization on Today / Review Queue | The dossier's `soft_signals.customer_concentration` card is suppressed when a `customer_concentration` Pattern is present. The flag still drives the chip on other views |
| `acceleration_clause_triggered` Pattern | `patterns.py:1252`. Severity 50 still flows into `patterns.fraud_score` | The pattern card is suppressed on the dossier when the hard-decline reason fires. The hard-decline line carries the worker copy |
| `unauthorized_withdrawal_dispute` Pattern | `patterns.py:1196`. Severity 35 still flows into `patterns.fraud_score` | Same as acceleration — pattern card suppressed, hard-decline line is the canonical render |

In every case the scoring engine still consumes the signal exactly as before; only the duplicate dossier render is hidden.

---

## Pointer to the v2 catalog + tampering triage

This document is the **consolidation map** — what was rendered before, what's rendered now, where to find the new copy. For the **full inventory** of every signal AEGIS emits today and the decision contradictions / legibility issues identified across the engine, see two sibling worktree catalogs:

- **v2 catalog (55 signals, Buckets A and B):** the comprehensive as-built audit. Bucket A flags four decision contradictions that operator policy should triage (EOF 2-vs-2 threshold mismatch, 65-vs-70 fraud-score gate mismatch, the unreachable `bank_statement_tampering_confirmed`, and the customer-concentration triple-count). Bucket B flags eight legibility issues (B.1 concentration, B.2 unclear copy for 6 signals, B.3 single-statement transfer false-positives, B.4 paydown dual-reading, B.5 opaque AI-generated composite, B.6 payroll triple-name, B.7 dual-tier acceleration / dispute, B.8 unreachable `missing_source` validator code). This round addresses the **display half** of B.1, B.6, and B.7 — the engine-side decision contradictions remain for the operator to triage in a future round.

- **Tampering triage:** focuses on `bank_statement_tampering_confirmed`, the v2 catalog's A.3. Documents what AEGIS catches today vs. what it misses (competent fabrications, surgical edits, description-only changes), proposes three composition rules (A / B / C) the operator can choose between, and scopes the wiring cost (~150 LOC + one migration). This round does not touch the tampering wiring; the catalog stands as a follow-up scope.

Both sibling catalogs contain detailed `file:line` citations for every signal claim. When this round's display consolidation needs a deeper look — e.g. "why does the engine emit `customer_concentration` at three different thresholds?" — those documents carry the receipts.

---

**Last updated:** 2026-06-04
**Scope:** dossier display only. Engine, scoring, and persistence unchanged.
**Related:** `src/aegis/web/_pattern_cards.py`, `src/aegis/web/templates/merchant_detail_dossier.html.j2`, `docs/FLAG_GLOSSARY.md`.
