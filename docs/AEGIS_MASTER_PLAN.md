# AEGIS — Master Upgrade Plan

**Audience:** Claude Code, executing on the AEGIS codebase
**Owner:** Commera Capital (MCA brokerage; internal underwriting and compliance tool, built to scale with the business)
**Status:** Authoritative master plan — combines industry research + state compliance into a single execution document
**Last updated:** 2026-05-17
**Disclaimer:** This document distills published industry and legal commentary as of May 2026. It is operational guidance for engineering, not legal advice. Before activating any state's Tier 1 surface in production, a licensed attorney in that state must review the disclosure template and broker-conduct rules.

---

## How to use this document

1. **Sections 1–3** are the mental model. Read first. Do not skip.
2. **Sections 4–8** are reference: industry landscape, metrics, parsers, fraud, state matrix. Treat as authoritative spec.
3. **Sections 9–10** are the data model AEGIS needs.
4. **Sections 11–22** are the execution phases. Do in order. Each has acceptance criteria.
5. When a phase says "counsel gate," stop and confirm with the operator before merging.
6. When this document and the codebase disagree, this document is the spec — propose a change to one of them; do not silently drift.

---

## 1. What AEGIS is and isn't

AEGIS is the underwriting and compliance brain for Commera Capital, an MCA brokerage. It lives behind Zoho CRM and produces three things per deal:

1. A **defensible analysis** of the merchant (parsed bank statements, scored, with patterns and source-IDs)
2. A **compliant disclosure** (where required by state law) with snapshot-tested templates
3. An **immutable decision snapshot** that a regulator or counsel can reproduce months later

AEGIS is **not** a CRM, not a servicing platform, not a syndication manager, not a collections tool. Zoho handles workflow. AEGIS handles the brain.

This boundary matters: every time a feature is proposed that drags AEGIS into CRM/servicing territory ("submission pipeline UI," "syndicate payout tracking"), push back — that's Zoho's job, or it's outside scope.

**AEGIS is built to scale with the business.** Commera Capital is growing, and AEGIS must grow with it — in deal volume, in number of operators using the system, in geographic reach, and in product complexity. No design decision in this document assumes a single-operator deployment or a fixed deal volume. Concretely:

- Data model assumes multi-user from day one (every action attributable to an actor; no "this is fine because there's only one person").
- All workloads route through arq + Redis so horizontal scaling is a config change, not a rewrite.
- Background jobs (parsing, alerting, retention, watchlist review) run on schedule, not on ad-hoc operator triggers.
- Permission and audit surfaces assume multiple operators with different roles (underwriter, compliance reviewer, admin) will eventually exist.
- Per-deal compute cost (Bedrock tokens, parser time) is tracked from day one so unit economics stay visible as volume grows.
- Compliance posture is built for breadth (all 50 states, all current Tier 1 statutes), not just the states Commera serves today, so growth into new geographies doesn't require a rebuild.

When in doubt between "the simple solo solution" and "the scalable one," pick the scalable one if the cost difference is modest. The cost of retrofitting scale into a tool that assumed it would never need it is always larger than the cost of building it in from the start.

---

## 2. The three principles AEGIS lives by

These override any future pressure to deviate. If a feature request contradicts them, the feature is wrong, not the principle.

**(1) Determinism beats elegance.** LLMs extract ambiguous text (descriptions, classification). Deterministic Python computes anything that becomes a number on the dossier. The two-pass parser is the firewall against hallucination. Money math never goes through an LLM.

**(2) Source-ID every aggregate.** Every number on the dossier traces to specific transactions, which trace to specific page+line in the source PDF. No exceptions. This is the difference between "AI guessed" and "regulator-defensible."

**(3) Snapshot decisions, don't recompute them.** A decision made today must be reproducible 180 days from now even after rules drift, after the parser changes, after the score weights update. Immutable `decisions` table is the mechanism. Without it, AEGIS cannot defend against "why did you decline Merchant X on 2026-03-14?"

Two corollaries:
- **Fail closed.** If validation fails: manual_review. If OFAC cache is stale: decline-pending-refresh. If a parser can't reconcile: don't guess.
- **Compliance gates by artifact, not by config flag.** Tier 1 disclosure availability = the artifact folder + locked template + snapshot test all exist. Not a boolean in YAML.

---

## 3. Current state of AEGIS — what works, what's broken

### What works (preserve)
- Two-pass parser architecture (pikepdf metadata → OCR fallback → LLM extraction → deterministic validation gate → LLM classification → deterministic detectors → deterministic aggregation with source-IDs)
- Tier 1/2/3 state structure
- Locked Jinja templates with snapshot tests for current Tier 1 set
- APR via `scipy.optimize.brentq` per Reg Z Appendix J
- OFAC SDN check with 24h cache, fail-closed at 7 days
- 56-PDF synthetic corpus with 13 scenarios × 6 banks
- `compliance/renewal.py` (logic exists)
- `docs/compliance/10_record_retention.md` (policy exists)
- audit_log writes on every state change

### What's broken or missing (this plan fixes)
- 40 states sit in Tier 3 / `StateNotAudited` with no sequenced audit plan
- **VA, CT, MO, UT, TX possibly miscategorized** as state_not_served when they should be Tier 1 served (with TX needing a deal-killing overlay rule)
- **LA and TX (2025 enactments) likely not yet in the matrix at all**
- **CA SB 362 (effective 2026-01-01) renewal re-disclosure** not surfaced
- No `decisions` immutable snapshot table — adverse-action evidence cannot be reproduced
- No audit-log query UI
- Renewal queue UI missing (CA SB 362 + NY §600.17 require it)
- No retention-job enforcement (policy without execution)
- No machine-readable state matrix
- No broker advance-fee guard (FL/GA prohibit it)
- No TX auto-debit guard (HB 700 effectively kills standard MCA in TX)
- No alerting on Bedrock / Zoho failures
- No operator-override capture for the decision flywheel
- No funder-reply ingestion
- No Bedrock per-deal cost tracking
- No DR plan (single-box deployment is fine today but is the failure mode tomorrow)
- No multi-operator readiness (role schema, per-actor audit, role-gated permissions)
- No horizontal-scale prep (some state may be local to the box; unverified)
- Real-statement corpus (especially fintech banks) not built
- Parser-drift regression loop runs only on synthetic corpus
- Several industry-standard detectors missing (unreconciled internal transfer, mca_payoff_signature, customer_concentration, processor_holdback_detected, ai_generated_statement, acceleration_clause_triggered)

---

## 4. Industry landscape — what AEGIS is measured against

### 4.1 Direct competitors (parser + risk layer)
What every funder underwriter benchmarks against. AEGIS output must be at least as defensible.

| Tool | Notable capabilities | What we learn |
|---|---|---|
| **Ocrolus** | Used by Brex, SoFi, PayPal, LendingClub. "Detect" product flags tampering + algorithmic anomalies; "Authenticity Score" (≤30 = highly suspicious); Counterparty Detection (revenue concentration, hidden bank accounts via unreconciled transfers); Forensic Analytics (stop payments, paid-in-full indicators); fraud found in 6–7% of statements. | Bar for fraud signals is high. Tampering + algorithmic + counterparty are table stakes. |
| **MoneyThumb (PDF Insights + Thumbprint)** | MCA-industry favorite (Onyx IQ integration). 3,000+ bank format coverage. Claims 95% auto-reconcile at 99.9% accuracy. | Industry-standard scorecard fields: true revenue, MCA position, NSF/OD count, low-balance days, summary reconciliation. |
| **Heron Data** | Built for MCA funders + brokers. Email-in → CRM write-back. 90% scrubbed <1min at 99%+ accuracy. Outputs ADB, NSF days, DSCR, true revenue, "out of appetite" flag. | Email-forward intake protocol matters because brokers receive deals by email. |
| **Inscribe** | Document risk screening. Detects forged/fabricated/AI-generated fakes, template fraud, editing traces. Multi-account unification. | AI-generated statements are now a real threat. Multi-account view exposes hidden transfers. |
| **DocuClipper** | Accountant-leaning. "50+ signals" for tampering: metadata, font consistency, balance arithmetic. | Signal count is marketing — what matters is validation layers. |
| **Dragin** | NY-based, MCA-specific. End-to-end email ingestion + scrubbing + fraud + auto-underwriting. | MCA-specific niche is winnable; generic tools leave a gap. |
| **LlamaParse** | Generic. Agentic auto-mode escalates per-page (text on clean, vision on hard). | Auto-escalation pattern is right cost-control architecture. |

### 4.2 NOT AEGIS's competition
LendFoundry, LendSaaS, Centrex, Cloudsquare, MCA Track, Onyx IQ, MCAOS, Libati, PRM, TurnKey Lender, Stratix MCA Suite — these are CRM + LOS + servicing + syndication platforms. AEGIS does not compete here. **Scope-creep guardrail:** if a feature would make AEGIS look like these, push back.

### 4.3 Bank-data APIs (auxiliary, not replacement)
| Tool | Use |
|---|---|
| **Plaid** | Real-time bank account data via OAuth. Ingest as supplementary signal alongside parsed PDFs. PDFs remain ground truth. |
| **DecisionLogic** | Direct bank pull. Per DailyFunder chatter: "lenders won't accept it" — summary-only output isn't trusted by MCA underwriters. |

### 4.4 Fraud-specific tools (watch list)
VerifyPDF, Inscribe — both flag AI-generated bank statements in seconds. Build our own behavioral-plausibility layer (Layer 4 in §7); reserve right to integrate one if AI-fake volume rises.

---

## 5. Industry-standard MCA metrics — the authoritative list

These are the metrics every MCA underwriter computes. AEGIS must compute all of them, name them as the industry does, and surface them on the dossier so funders recognize the output.

### 5.1 Revenue metrics

| Metric | Definition | Industry threshold |
|---|---|---|
| **Gross deposits** | Sum of all credits | — (never used alone) |
| **True revenue** | Gross deposits MINUS: inter-account transfers, MCA funding inflows, loan proceeds, refunds, owner deposits, reversals | — |
| **Average monthly true revenue** | True revenue / months (3-month preferred, 6-month for seasonal) | Min $10–15k major funders; $8k mid; $5k specialty |
| **Revenue trend** | Slope of monthly true revenue | Up: factor improves 0.05–0.10. Down: tier downgrade |
| **Revenue volatility (CV)** | stddev / mean of monthly true revenue | High CV ⇒ tier downgrade even at same average |
| **Deposit count / month** | Number of credits | Low+high$ = lumpy/B2B; high+low$ = retail |
| **Largest deposit % of revenue** | Max single credit / month total | >30% from one source = customer concentration |

### 5.2 Balance metrics

| Metric | Definition | Threshold |
|---|---|---|
| **Average Daily Balance (ADB)** | Sum of EOD balances / days | **≥10–15× expected daily MCA payment** = ≥10–15% of monthly revenue |
| **Minimum daily balance** | Lowest EOD | <$500 = elevated risk |
| **Ending balance** | Last day | Consistently <$1k = tight cash flow |
| **Days negative** | EOD < $0 | 0–4: ok. 5–9: yellow. ≥10: usually decline |
| **Days near-zero** | EOD < $100 (or <$500 per funder) | Softer signal |
| **Negative concentration** | Clustered vs spread | Clustered = forgivable. Spread = chronic = decline |

### 5.3 NSF / overdraft metrics

| Metric | Threshold |
|---|---|
| **NSF count / month** | 0–2: ok. 3–5: yellow + compensating factors. 6–10: usually decline. >10: auto-decline |
| **NSF trend** | Improving > worsening at same average |
| **Overdraft fees** | Counted alongside NSFs |
| **NSF dollar exposure** | Large = customer/vendor bouncing. Small = personal |
| **Returned ACH** | Treated as NSF-equivalent |

### 5.4 MCA stacking metrics (the deal-killer)

| Metric | Threshold |
|---|---|
| **Active MCA positions** | 0: clean. 1: 2nd-pos market. 2: limited. 3+: most decline |
| **Total daily MCA debit** | Combined with new advance ≤15–25% of average daily deposits |
| **MCA debt-to-revenue** | >1.0–1.5× = stacking-spiral, responsible funders cap here |
| **Stacking pattern signature** | Match: amount-regularity + frequency-regularity + originator dict (RAPID FINANCE, ON DECK, KAPITUS, FORA, EBF, WBL, etc.) |
| **Holdback capacity** | (ADB × 0.15) − sum(existing daily MCAs). <$50/day = no room |

### 5.5 TIB / entity metrics

| Metric | Threshold |
|---|---|
| **Time in business** | <6mo: auto-decline most. 6–12mo: startup only, expensive. 12–24mo: standard. 24+: best |
| **Bank account age** | <3mo open = high fraud risk (synthetic identity) |
| **Industry NAICS** | Restricted: cannabis, adult, firearms, crypto, gambling, MLM, debt-relief, MSB |

### 5.6 Cash-flow ratios

| Metric | Threshold |
|---|---|
| **DSCR** | <1.0: can't service. 1.0–1.25: tight. >1.25: healthy. Banks want 1.25–1.35×; MCAs more flexible |
| **Operating cash flow margin** | Industry-dependent. Restaurants 5–10%. Trucking 8–15%. Services 15–30% |
| **Cash conversion** | <5%: burns on receipt. >20%: healthy reserve |

### 5.7 Counterparty signals (Ocrolus playbook)

| Metric | Tells you |
|---|---|
| **Payroll cadence** | Regular biweekly/weekly = real operating business. Missing on $50k/mo = red flag |
| **Top 5 counterparties (revenue)** | >40% from one = single-customer dependency |
| **Top 5 counterparties (expense)** | Business model understanding |
| **Recurring vendor matches** | ADP/Gusto/Paychex/Rippling, Stripe/Square/Toast/Clover, insurance, rent = confirms operations |
| **Unreconciled internal transfers** | Transfers to accounts not in the bundle = hidden bank account = possible undisclosed MCA |

### 5.8 Paper grades (industry standard)

| Grade | Profile | Factor | Funder |
|---|---|---|---|
| **A** | 24+mo, $25k+ rev, ADB >15%, 0–2 NSF, 0 stacking, no liens, FICO 650+ | 1.15–1.25 | First-position prime |
| **B** | 12+mo, $15k+ rev, ADB 8–15%, 3–5 NSF, 0–1 MCA, no major derogs | 1.25–1.35 | Mainstream |
| **C** | 6+mo, $10k+ rev, ADB <8%, 6–10 NSF, 1–2 MCAs, minor liens | 1.35–1.45 | Sub-prime / B-C paper |
| **D** | <12mo, declining, >10 NSF, 3+ MCAs, prior defaults, liens, judgments | 1.45–1.55 | Last-resort, high decline |

Surface paper grade prominently on the dossier. Score thresholds should map cleanly to A/B/C/D.

---

## 6. Parsers AEGIS must own

### 6.1 Document classification

| Parser | Status | Notes |
|---|---|---|
| Bank statement | ✅ Have | Primary pipeline |
| Merchant processor (Stripe/Square/Toast/Clover/PayPal) | ⚠️ Gap | Required for split-funded MCA |
| Voided check | ✅ Have | |
| Driver's license / ID | ⚠️ Gap | Cross-check against application + statement holder |
| Articles of organization / EIN | ⚠️ Gap | Entity verification |
| Tax return (1120/1040/Sched C) | ⚠️ Gap | Required >$500k under most CFDLs |
| Funder approval/decline email | ⚠️ Gap | Feeds operator-override flywheel |
| MCA contract | ⚠️ Gap | Parse competing offers for comparison |

### 6.2 Bank coverage tiers

**Tier 1 — flawless required (~80% of deals):**
Chase, BoA, Wells Fargo, Capital One, US Bank, PNC, TD, Citi, Truist, Regions, Fifth Third, Huntington, BMO, Citizens, KeyBank, M&T, Santander.

**Tier 2 — must work, lower volume:**
Comerica, BancorpSouth, First Horizon, Synovus, Webster, NYCB, Valley National, Texas Capital, Frost, Zions. CUs: NFCU, PenFed, SECU, Alliant, BECU, Mountain America. Fintechs: Square Banking, Brex Cash, Mercury, Bluevine, Relay, Novo, Lili, Found.

**Fintech-bank gotcha:** Most MCA funders **decline** Mercury, Brex, Bluevine, Novo, Relay, Lili, Found accounts because ACH debit reliability is poor. AEGIS must detect the bank from header/footer, flag `FintechBankRisk`, and surface "many funders decline fintech bank accounts" — not as a parse failure but as a pre-submission soft warning. Saves wasted submissions.

**Tier 3 — best-effort, manual review acceptable:**
Community banks, smaller CUs, foreign statements.

### 6.3 Two-pass architecture (preserve)

Already in AEGIS. Document the rationale here so future Claude Code sessions don't try to "simplify":

```
PDF in
  ↓ pikepdf metadata layer       — tamper signals
  ↓ OCR fallback if image
  ↓ Pass 1: LLM extraction       — raw rows + printed summary (NO aggregates from LLM)
  ↓ Deterministic validation     — begin + deposits − withdrawals == end ± $1
                                   FAIL → manual_review, no retry
  ↓ Pass 2: LLM classification   — per-row category + confidence
  ↓ Deterministic detectors      — stacking, kiting, NSF clusters, etc.
  ↓ Deterministic aggregation    — ADB, true revenue, with _source_ids[]
  ↓ Scoring + recommendation
```

**Why this is right (defend against future pressure):**
- LLMs hallucinate numbers. Bank statements are math. Math can't hallucinate.
- The printed summary IS the ground truth. If our extraction doesn't match, *we* are wrong.
- Source-ID traceability is what makes output defensible to a funder or regulator.

### 6.4 Pattern detectors

**Already in AEGIS:** `mca_stacking`, `kiting`, `paydown_mca_suspected`, `nsf_clustering_short`, `nsf_late_concentration`, `preloan_spike`, `deposit_velocity_spike`, `withdrawal_acceleration`, `recent_account_opening`, `wash_deposit_suspected`, `duplicate_deposits`, `synthetic_low_variance`, `round_number_deposits`.

**To add (in priority order):**

| Detector | What it catches | Detection |
|---|---|---|
| `unreconciled_internal_transfer` | Hidden bank account / undisclosed MCA | Transfer-out >$500 with no matching transfer-in in any submitted statement |
| `mca_payoff_signature` | Paid-off MCA not showing as active | Single debit >$5k to name matching MCA-originator dict |
| `customer_concentration` | One counterparty >30% of revenue | Group credits by normalized originator, compute % of true revenue |
| `processor_holdback_detected` | Card processor already withholding for prior MCA | "STRIPE TRANSFER" net of advance withholding, batch shortfalls |
| `chargeback_velocity` | Refunds + chargebacks accelerating | Count "REFUND/CHARGEBACK/DISPUTE" debits/mo, slope |
| `unauthorized_withdrawal_dispute` | Reversal of MCA debit (merchant fighting funder) | Credit matching "REVERSAL/DISPUTE CREDIT" paired with prior debit |
| `acceleration_clause_triggered` | Funder called balance after default | MCA recurring pattern broken + 5–10× larger debit to same originator. Hard decline |
| `payroll_absent` | "Operating business" with no payroll | No ACH to/from ADP/Gusto/Paychex/Rippling/Square Payroll over period (soft signal) |
| `bank_account_changed_mid_period` | New account/routing mid-bundle | Statement i vs i+1 differ. Could be legitimate or evasion |
| `font_inconsistency` (forensics) | One row in different font | Compare font metadata per row |
| `editor_signature` (forensics) | "Adobe Acrobat"/"Foxit" in producer where bank name expected | pikepdf metadata |
| `page_layer_anomaly` | Different content streams per page | pikepdf object stream inspection |
| `ai_generated_statement` | LLM-generated fake | Composite: perfect reconciliation, LLM-style descriptions (full sentences, no abbreviations), generic counterparties. **Score, don't auto-decline** |

### 6.5 Validation gates (preserve)

Non-negotiable. Any change must keep them:
1. Sum(extracted deposits) == printed deposit total ± $1
2. Sum(extracted withdrawals) == printed withdrawal total ± $1
3. Begin + deposits − withdrawals == ending ± $1
4. Daily running-balance reconciliation
5. Statement period continuous (no date gaps)
6. Multi-statement bundle: account number consistent, no month missing

Fail-closed: no retry → `manual_review`. This separates audit-defensible tools from "AI guesses."

---

## 7. Fraud — the 5-layer composite

Industry standard. AEGIS already does this; the list below is what each layer should contain.

**Layer 1 — File forensics (pikepdf)**
Producer/Creator/Author vs known bank signatures; creation vs modification drift; multiple `/Prev` xref entries; encryption status; embedded font list; page count vs declared "page X of Y".

**Layer 2 — Visual / OCR consistency**
Font consistency per row; spacing/alignment per column; logo pixel-hash vs known bank logos; MICR line on checks.

**Layer 3 — Content / math (the firewall — already in AEGIS)**
Begin + deposits − withdrawals == end ± $1; daily running balance; period boundaries.

**Layer 4 — Plausibility / behavioral**
Descriptions look like real OCR (mixed case, abbreviations, occasional noise) not too clean; counterparty diversity matches business model; round-number clustering (>40% ending in 00 = synthetic); time-of-day distributions; statement matches bank's known typography.

**Layer 5 — Cross-document**
Account/routing consistent across submitted months; closing of month N == opening of month N+1; cross-account transfers reconcile.

**Composite fraud score (0–100, per Ocrolus Detect):**
- ≤30: highly suspicious — usually reject
- 31–60: review required
- 61–100: low concern

**Multi-tier escalation rule (per Floowed/Lido/Inscribe):**
- 1 anomaly = note
- 2 anomalies in different layers = manual review
- 3+ anomalies = hard decline

Never auto-decline on a single weak signal. A weird font might be a template update, not fraud.

---

## 8. State compliance — authoritative matrix

### 8.1 The big picture

```
TIER 1 (CFDL enacted, disclosure required, AEGIS must serve with locked template):
  CA, NY, UT, VA, FL, GA, CT, KS, MO, LA, TX                              [11 states]

TIER 2 (Active pending bills — watch list, may move to Tier 1 within 6–18mo):
  NJ, MD, IL, MS, NC, PA, HI, NH, OH                                       [9 states]

TIER 3 (No MCA-specific disclosure law — general UDAP/usury/contract applies):
  Remaining ~30 states                                                     [~30 states]

OVERLAYS (apply regardless of tier):
  COJ banned/restricted:    CA, FL, MA, IN, AK, MD (consumer), TX (HB 700), NY (out-of-state)
  Auto-debit restricted:    TX (HB 700, first-priority lien required) ← deal-killer for std MCA
  Broker advance-fee ban:   FL, GA
  Forum restrictions:       VA (must be VA venue)
  AG enforcement risk:      NY (Yellowstone $1.065B), CA, MA, NJ, DE, MD
```

### 8.2 Tier 1 detailed matrix

| State | Statute | Effective | Threshold | Scope | Broker reg? | APR? | Notes | COJ | Penalty |
|---|---|---|---|---|---|---|---|---|---|
| **CA** | SB 1235 + SB 362 | 2022-12-09 / **2026-01-01** | ≤$500k | All commercial financing | No (provider needs CFL license) | **Yes (Reg Z)** | SB 362 limits "rate"/"interest" usage; **renewal re-disclosure required** | Banned 2023-01-01 | DFPI civil + restitution |
| **NY** | CFDL Art. 8 + 23 NYCRR 600 | 2023-08-01 | ≤$2.5M | All commercial (broad) | No | **Yes (Reg Z or opt-in)** | Broker comp in writing; renewal §600.17; 4-yr retention | Out-of-state restricted 2019 | $2k/violation, $10k intentional |
| **UT** | HB 198 | 2023-01-01 | ≤$1M | All commercial | **Yes (NMLS, annual)** | No | Discloses funds to brokers | Allowed | Up to $20k civil |
| **VA** | HB 1027 | 2022-07-01 | ≤$500k | **MCA only** | **Yes (VA SCC, annual)** | No | **VA venue mandatory**; arbitration restrictions; **COJ void in MCA** | Restricted | Up to $50k/deal |
| **FL** | HB 1353 (FCFDL) | 2024-01-01 | ≤$500k | Loans, OEC, AR purchase + MCA | No reg | No | **Broker advance-fee prohibited**; address+phone in advertising | Banned | $500–$20k; $50k continued |
| **GA** | SB 90 (amends FBPA) | 2024-01-01 | ≤$500k | Loans, AR purchase | No reg | No | **Broker advance-fee prohibited**; no deceptive reps | Civil usury cap may apply | $500–$20k; up to $50k; AG enforces, no PRA |
| **CT** | SB 1032 | 2024-07-01 (no-action thru 2024-10-01) | ≤$250k | **MCA only** | **Yes (CT DOB, annual, by Oct 1)** | No | Broker comp; double-dipping; renewal | Void | Up to $100k/violation |
| **KS** | SB 345 | 2024-07-01 | ≤$500k | Loans, OEC, lines, AR | No reg | No | Standard disclosures; signature required | Void | Standard |
| **MO** | SB 1359 §427.300 | **2025-02-28** | ≤$500k | Standard commercial | **Brokers register (MO Div of Finance); providers do not** | No | Unusual: broker-only registration | Allowed | AG civil |
| **LA** | HB 470 | **2025-08-01** | **No cap** | **MCA only**; no entity exemptions, no bank carve-out | No reg | No | 6 disclosures; AG-only enforcement | Allowed in commercial | $500–$50k; no PRA |
| **TX** | HB 700 (Ch. 398) | **2025-09-01** disclosure; **2026-12-31 registration** | ≤$1M disclosure; reg regardless of size | **MCA only** | **Yes (TX OCCC, by 2026-12-31, renew by Jan 31)** | No | **AUTO-DEBIT PROHIBITED w/o first-priority perfected UCC lien in deposit account — kills most MCA**; COJ void in MCA contracts | Void in MCA | $10k/violation; no PRA |

### 8.3 Tier 2 watch list

| State | Bill | Status May 2026 | Likelihood |
|---|---|---|---|
| **NJ** | S1760 | Reintroduced 2026; Reg Z APR; ≤$500k; 5-deal de minimis | **High** (Dem trifecta) |
| **MD** | SB754 / HB693 | Effective Oct 1, 2025 in prior session text; **recheck if enacted** | Medium |
| **IL** | SB 2234 (Small Business TIL Act) | Reassigned to Financial Institutions; ≤$2.5M | Medium |
| **PA** | HB 1792 | **Includes private right of action — uniquely dangerous** | Watch closely |
| **MS** | Draft 2025 | Pending | Low-medium |
| **NC** | Considering | Pending | Low |
| **HI** | Draft 2025 | Pending | Low |
| **NH** | Draft 2025 | Pending | Low |
| **OH** | Draft 2025 | Pending | Low |

### 8.4 Tier 3 posture (do not auto-decline)

The remaining ~30 states have no MCA-specific disclosure law. **Does not mean no compliance.** General UDAP, usury (where MCA can be recharacterized as loan), and contract law still apply.

AEGIS posture in Tier 3:
- Serve the deal (do not auto-decline)
- Generate a **defensive disclosure** (not state-mandated but best-practice; same fields as Tier 1 minimum: total funds, total to pay, schedule, factor, prepayment, broker comp)
- Persist same `decisions` snapshot
- Flag known-risky AG-enforcement states: MA, DE, MD, NJ-pending

### 8.5 Overlay rules detail

**COJ status:**
- Banned outright: CA, FL, MA, IN, AK
- Banned in MCA contracts: VA, TX
- Banned in consumer only: MD
- Restricted vs out-of-state: NY
- Voided by general law: CT, KS
- Permitted in commercial: NY (in-state), PA, OH, MI

**Auto-debit (TX HB 700):**
Prohibited unless provider holds first-priority perfected UCC security interest in the deposit account. **Effective deal-killer for standard MCA in TX.** AEGIS hard-declines TX deals using standard ACH-debit structure.

**Broker advance-fee (FL, GA):**
Broker may not assess/collect/solicit advance fee from business. Narrow carve-out for **actual** third-party costs (credit check, appraisal) paid by check/MO to **independent third party**. Applies regardless of deal size.

**Forum/arbitration (VA):**
Court action must be brought in VA; forum-selection to other states unenforceable. Arbitration cannot require face-to-face arbitration outside merchant's principal place of business; provider pays arbitration costs.

**Active AG enforcement (heightened scrutiny):**
- NY AG: 2025 Yellowstone $1.065B settlement, 1,100+ judgments vacated
- CA AG: 2024 actions >$100M
- MA AG: treats high-factor MCA as usurious
- NJ AG: ongoing suits vs 3 funders
- DE Division of Banking: 2 large 2025 actions
- MD AG: 2023 injunction

### 8.6 Universal disclosure field set

| Field | Required in |
|---|---|
| total_amount_financed | All Tier 1 |
| disbursement_amount | All Tier 1 |
| finance_charge | All Tier 1 |
| total_repayment_amount | All Tier 1 |
| **estimated_apr** (Reg Z method) | CA, NY, NJ (pending), MD (pending) |
| payment_amount, frequency, term | All Tier 1 |
| prepayment_terms | All Tier 1 |
| other_fees (itemized) | All Tier 1 |
| collateral_security_description | NY mainly |
| broker_compensation | CA, NY, TX (others vary) |
| double_dipping_disclosure | NY, TX, CA (renewal) |
| prior_balance_payoff_amount | TX, NY |
| reduction_in_disbursement | TX |

---

## 9. Data model

### 9.1 `states.yaml` — single source of truth

```yaml
# docs/compliance/states.yaml
version: 2026.05.17
states:
  CA:
    name: California
    tier: 1
    cfdl:
      statute: ["SB 1235 (2018)", "SB 362 (2025/eff 2026-01-01)"]
      effective: 2022-12-09
      threshold_usd: 500000
      product_scope: [sales_based, closed_end, open_end, factoring, lease, asset_based]
      apr_required: true
      apr_method: reg_z_1026_22
      broker_registration: false
      renewal_redisclosure: true
      retention_years: 4
      template_path: docs/compliance/states/CA/03_disclosure_template.j2
      template_sha256: <captured by snapshot test>
    overlays:
      coj: banned
      autodebit: permitted
      broker_advance_fee: permitted
      forum_restriction: none
      ag_enforcement_risk: high
    penalties:
      enforcement_authority: DFPI
      private_right_of_action: false

  TX:
    name: Texas
    tier: 1
    cfdl:
      statute: ["HB 700 (Ch. 398 TX Finance Code)"]
      effective: 2025-09-01
      registration_effective: 2026-12-31
      threshold_usd: 1000000
      product_scope: [sales_based]
      apr_required: false
      broker_registration: true
      registration_authority: TX OCCC
      registration_renewal: annual_by_january_31
      retention_years: 4
    overlays:
      coj: banned_in_mca
      autodebit: prohibited_without_first_priority_lien    # CRITICAL
      broker_advance_fee: permitted
    hard_decline_rules:
      - rule: tx_autodebit_without_first_priority_lien
        message: |
          TX HB 700 prohibits automatic ACH debit unless provider holds
          first-priority perfected UCC security interest in deposit account.
          Standard MCA structures cannot satisfy this. Decline or restructure.
    penalties:
      max_per_violation_usd: 10000
      enforcement_authority: TX OCCC
      private_right_of_action: false

  # ... repeat for all 50 states + DC

watchlist:
  NJ: { bills: [S1760], likelihood: high, last_reviewed: 2026-05-17 }
  PA: { bills: [HB 1792], likelihood: medium,
        notes: "Contains private right of action — uniquely dangerous",
        last_reviewed: 2026-05-17 }
  # ... etc

tier_3_default:
  generate_defensive_disclosure: true
  persist_decision_snapshot: true
  flag_ag_enforcement_states: [MA, NJ, DE, MD]
```

### 9.2 `decisions` table (immutable snapshot — regulator-defense)

```sql
CREATE TABLE decisions (
    id uuid PRIMARY KEY,
    deal_id uuid NOT NULL REFERENCES deals(id),
    decided_at timestamptz NOT NULL DEFAULT now(),
    decided_by text NOT NULL,                       -- 'auto' or operator id

    decision text NOT NULL CHECK (decision IN ('approve','decline','manual_review','redisclosure')),
    decision_reason_codes text[] NOT NULL,
    score numeric(5,2),
    score_factors jsonb NOT NULL,                   -- {name: {weight, value, contribution}}

    analysis_id uuid NOT NULL REFERENCES analyses(id),
    contributing_transaction_uuids uuid[] NOT NULL,
    bank_statement_pdf_sha256 text NOT NULL,

    state_code text NOT NULL,
    cfdl_tier int NOT NULL,
    disclosure_template_path text,
    disclosure_template_sha256 text,
    disclosure_pdf_sha256 text,
    apr_calculated numeric(8,4),
    apr_method text,

    ofac_cache_timestamp timestamptz NOT NULL,
    ofac_cache_sha256 text NOT NULL,

    aegis_version text NOT NULL,
    rule_pack_version text NOT NULL
);

-- Immutability triggers
CREATE OR REPLACE FUNCTION block_decision_modification() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'decisions table is append-only; use a new row to supersede';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER decisions_no_update BEFORE UPDATE ON decisions
    FOR EACH ROW EXECUTE FUNCTION block_decision_modification();
CREATE TRIGGER decisions_no_delete BEFORE DELETE ON decisions
    FOR EACH ROW EXECUTE FUNCTION block_decision_modification();
```

### 9.3 `disclosures` table

```sql
CREATE TABLE disclosures (
    id uuid PRIMARY KEY,
    deal_id uuid NOT NULL REFERENCES deals(id),
    decision_id uuid NOT NULL REFERENCES decisions(id),
    state_code text NOT NULL,
    template_path text NOT NULL,
    template_sha256 text NOT NULL,
    disclosure_type text NOT NULL CHECK (disclosure_type IN ('origination','renewal','defensive')),
    inputs jsonb NOT NULL,
    rendered_pdf_path text NOT NULL,
    rendered_pdf_sha256 text NOT NULL,
    delivered_at timestamptz,
    delivery_method text,
    merchant_signature_at timestamptz,
    merchant_signature_ip text,
    merchant_signature_hash text,
    created_at timestamptz NOT NULL DEFAULT now()
);
```

### 9.4 `overrides` table (operator-override flywheel)

```sql
CREATE TABLE overrides (
    id uuid PRIMARY KEY,
    deal_id uuid NOT NULL REFERENCES deals(id),
    decision_id uuid NOT NULL REFERENCES decisions(id),
    original_recommendation text NOT NULL,
    operator_decision text NOT NULL,
    reason_code text NOT NULL,                      -- categorical
    reason_detail text,                              -- freeform
    factors_disputed jsonb,                          -- which score factors operator weighted differently
    pattern_false_positive text[],                   -- which detectors operator said were wrong
    operator_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    outcome text,                                    -- populated later: funded | declined_by_funder | charged_off | paid_in_full
    outcome_recorded_at timestamptz
);
```

Reason codes: `score_too_conservative`, `score_too_aggressive`, `funder_specific_fit`, `merchant_context_external`, `data_quality_concern`, `pattern_false_positive`, `pattern_false_negative`, `gut`.

### 9.5 `compliance_obligations` table

```sql
CREATE TABLE compliance_obligations (
    id uuid PRIMARY KEY,
    obligation_type text NOT NULL,                  -- 'registration','annual_report','license_renewal'
    state_code text NOT NULL,
    authority text NOT NULL,                        -- 'VA SCC','TX OCCC',etc
    description text NOT NULL,
    deadline date,
    recurrence text,                                -- 'annual','biennial','one_time'
    status text NOT NULL CHECK (status IN ('not_started','in_progress','submitted','active','lapsed')),
    next_due_date date,
    evidence_file_path text,
    last_reviewed timestamptz,
    notes text
);
```

Seed:
- VA SCC sales-based broker registration (annual)
- CT DOB sales-based provider/broker registration (annual, Oct 1)
- UT DFI commercial financing registration via NMLS (annual)
- MO Div of Finance broker registration (one-time + maintain)
- TX OCCC sales-based broker registration (by 2026-12-31, annual by Jan 31)
- CA DFPI annual report (where applicable)

### 9.6 Extend `audit_log`

Add: `deal_id` (indexed), `state_change` jsonb, `actor` text, `aegis_version` text, `rule_pack_version` text. Create view `audit_log_by_deal(deal_id)` for the audit UI.

---

## 10. Per-state folder structure

For each Tier 1 state, `docs/compliance/states/{ST}/` contains:

```
01_statutes.md          # Citations + source URLs (regulator's official page + 2-3 legal commentaries)
02_disclosure_required.md   # When disclosure fires, what fields
03_disclosure_template.j2   # Locked Jinja, snapshot-tested
04_licensing.md         # Broker/provider licensing requirements
05_prohibited_practices.md  # Broker conduct rules (advance fee, deceptive reps, etc.)
06_overlays.md          # COJ, auto-debit, forum, AG enforcement notes
07_audit_meta.yaml      # date_audited, by_whom, sources, next_review_due
```

When operator promotes a watchlist state to Tier 1, AEGIS triggers a checklist: build folder, draft template, schedule counsel review.

---

## 11. Phase 1 — Foundation (no risk, no counsel)

**Goal:** Set up data model, state matrix, per-state folder skeleton. Nothing user-visible changes.

**Tasks:**
1. Create `docs/compliance/states.yaml` per §9.1 covering all 50 states + DC. Tier 1: complete. Tier 2/3: stub.
2. Create `aegis/compliance/state_matrix.py` — Pydantic v2 loader with strict-mode validation. Boot fails if `states.yaml` invalid.
3. Create folder structure `docs/compliance/states/{ST}/` for each Tier 1 state with all 7 files (stubs OK for now).
4. Create `aegis/compliance/router.py` — pure function `(state_code, deal_amount, product_type) → (tier, applicable_rules, template_path, hard_decline_rules)`. Fully testable.
5. Write 50 unit tests (one per state) asserting router returns expected tier.

**Acceptance:**
- `make check` passes
- Boot validates matrix
- All Tier 1 folders exist with all 7 files
- Every state present in `states.yaml`

**Watch out:** Do not invent statute citations. If a citation isn't in §8.2 of this document or its referenced URLs, mark `TODO: verify` in the state folder.

**Effort:** 2 days. No counsel gate.

---

## 12. Phase 2 — Decision snapshot table (regulator defense)

**Goal:** Every decision becomes immutable evidence.

**Tasks:**
1. Migrations: `decisions`, `disclosures`, `overrides` tables per §9.2–9.4.
2. Extend `audit_log` per §9.6.
3. `aegis/compliance/snapshot.py` exposing `record_decision(deal_id, decision, factors, ...)` writing immutable rows with: all score factors with weights, rule_pack_version hash, template SHA256, bank statement PDF SHA256, OFAC cache timestamp.
4. Wire into existing decision pipeline. **Every** approve/decline/manual_review calls `record_decision()` before returning.
5. One-time backfill: existing deals get `decisions` rows with `decided_by='backfill_2026_05'`.
6. `aegis/web/views/audit.py` route `/audit/deal/{deal_id}` returns all decisions chronologically, all disclosures with delivery proof, all audit_log events, all linked statements/analyses. Jinja+HTMX, no fancy UI required.

**Acceptance:**
- New deal flow writes `decisions` row
- `UPDATE decisions` raises `decisions table is append-only`
- `/audit/deal/{id}` returns full event log
- Snapshot test: deal's decision reproducible from snapshot data (rerun scoring against frozen inputs, get same result)

**Why this matters:** This is what answers "why did you decline Merchant X on 2026-03-14" six months later.

**Effort:** 3 days. No counsel gate.

---

## 13. Phase 3 — Anti-drift instrumentation (do early)

**Goal:** Make sure nothing rots over time. Done before subsequent phases so they plug into it.

**Tasks:**
1. Boot-time check: `states.yaml` version + every template SHA256 logged on startup. Mismatch = fail closed.
2. Per-state `07_audit_meta.yaml` has `next_review_due` date. Boot-time warning if overdue.
3. **Pre-commit hook** (revised from "CI check" — README.md documents the repo is no-CI by design): every commit touching `docs/compliance/states/**` must carry a `compliance-review:` annotation (`approved by <name>`, `pending`, or `not-applicable`) in the body. Hook lives at `.githooks/pre-commit`; installed by `make install-hooks` (idempotent, sets `core.hooksPath`). Rejected commits get a helpful message explaining the rule and the three valid forms.
4. Quarterly automated test: synthetic deal through every Tier 1 state, render disclosure, verify snapshot. Catches silent rendering bugs.

**Acceptance:** Anti-drift checks in place; overdue reviews surface as warnings, not silent. Pre-commit hook rejects unannotated state-folder commits.

**Effort:** 1 day. No counsel gate.

---

## 14. Phase 4 — State misclassification fixes (deal-flow win)

**Goal:** Stop declining what we should serve. Stop serving what we should hard-decline.

**Tasks:**
1. Audit current `state_not_served` list. Per prior gap analysis, VA, CT, MO, UT may be miscategorized.
2. For each of (VA, CT, UT, MO, TX): if currently `state_not_served`, move to Tier 1 served, **gated behind template existing in `docs/compliance/states/{ST}/03_disclosure_template.j2`**.
3. **TX specifically:** Hard-decline rule `tx_autodebit_without_first_priority_lien` fires for any TX-recipient deal using standard ACH-debit MCA. Decline message explains HB 700.
4. Migration to scoring rule pack: include new hard-decline rules.
5. **Broker advance-fee guard for FL & GA** at pre-submission gate. If broker arrangement involves upfront fee from merchant (other than independent-third-party payment for actual services like credit check), decline with `fl_ga_broker_advance_fee_prohibited`. Per-broker config — Commera almost certainly doesn't charge advance fees, but verify.
6. Snapshot tests on rule routing for each newly served state.

**Acceptance:**
- VA, CT, UT, MO deals no longer auto-declined
- TX standard ACH-debit MCA hard-declined with specific reason
- FL/GA deals with advance fees hard-declined
- Snapshot tests pass

**Watch out:** **Counsel gate before merging.** Confirm TX rule fits Commera's deal flow (some funders may have structures that *do* satisfy first-priority-lien). Confirm with operator: does Commera charge advance fees to merchants in FL/GA?

**Effort:** 2 days + counsel cycle (1 week elapsed).

---

## 15. Phase 5 — Disclosure templates (the locked Jinja layer)

**Goal:** Every Tier 1 state has a working, snapshot-tested disclosure template.

**Tasks per state in {CA, NY, FL, GA, UT, VA, CT, KS, MO, LA, TX}:**
1. Build Jinja template at `docs/compliance/states/{ST}/03_disclosure_template.j2` matching the regulator's published format.
2. Compute SHA256 at build, store in `states.yaml`. Drift = snapshot test fails = deploy blocked.
3. `aegis/compliance/render.py` exposes `render_disclosure(state, fields, disclosure_type='origination')` → WeasyPrint PDF.
4. Snapshot tests per state: canonical input → expected PDF hash.
5. Wire into deal flow: Tier 1 approval → generate disclosure → write `disclosures` row before submission can leave to funder.

**Special cases:**
- **CA (SB 362, eff 2026-01-01):** APR re-disclosure when terms change. Template supports `origination` and `renewal` variants. `renewal` includes double-dipping itemization.
- **NY (§600.17 + §600.21(f)):** Broker comp in writing (can be separate from main disclosure). Inline broker comp block.
- **TX (HB 700):** "Amount paid to broker" + prior-balance payoff itemization. Even if deal hard-declined for auto-debit, template ready.
- **LA (HB 470):** 6-field disclosure; no APR; no agency rulemaking — AEGIS responsible for form design. Use defensive comprehensive layout.

**Acceptance:**
- Every Tier 1 state renders without error
- Snapshot tests pass
- Template SHA256 in `states.yaml`
- Drift on any template fails CI

**Watch out:** **Counsel gate per template before shipping to production.** Snapshot test ensures template doesn't change unknowingly; only counsel can tell us *content* matches regulator's format. CA and NY publish exact formats — match them, don't improvise.

**Effort:** 5 days + counsel cycles in parallel (2–3 weeks elapsed).

---

## 16. Phase 6 — Renewal queue + re-disclosure surface

**Goal:** Close CA SB 362 / NY §600.17 compliance debt accruing every renewal.

**Tasks:**
1. Renewal detector: arq scheduled job (hourly):
   - Active funded deals with balance <50% paid
   - Cross-reference against new submissions for same merchant
   - Flag candidates
2. UI surface `/renewals` showing:
   - Original vs new submission terms diff
   - Computed double-dipping (new advance applied to prior balance + unpaid finance charges)
   - Disclosure-generation button → renewal variant
3. Gate: no submission packet assembled for renewal-flagged deal until renewal disclosure generated and acknowledged.
4. Snapshot test: renewal disclosure variant includes double-dipping fields.

**Acceptance:**
- Renewals detected automatically
- Renewal disclosure cannot be skipped — UI enforces gate
- Double-dipping calculation matches hand-computed example

**Watch out:** Detection threshold (balance <50%) is heuristic. Tune based on actual deal patterns. Lean false-positive (waste operator time) rather than false-negative (compliance failure).

**Counsel gate** for CA/NY renewal logic interpretation.

**Effort:** 3 days + counsel cycle (1 week elapsed).

---

## 17. Phase 7 — Audit-log query + retention enforcement

**Goal:** Make audit trail queryable, enforce retention policy.

**Tasks:**
1. Extend `/audit/deal/{id}` (from Phase 2): filtering by date, event type, actor.
2. Export: CSV + JSON for regulator submissions.
3. Retention enforcement: arq daily cron:
   - Per Tier 1 state, retain per state's requirement (NY=4yr from disclosure, CA=4yr, etc.)
   - Tier 3 default: 5 years (defensive)
   - **Archive** older records to cold storage. **Do not delete by default.** Deletion is manual after operator review.
   - Log every archive to `audit_log`
4. Compliance obligations UI `/compliance/obligations` showing registration deadlines, what's submitted, what's due.

**Acceptance:**
- Audit-log query UI returns full event log
- Retention job runs nightly, logs every archive
- Obligations page shows registration status

**Watch out:** Do not auto-delete. Always archive first. A regulator asking for a record we already deleted is worse than having the record.

**Effort:** 2 days. No counsel gate.

---

## 18. Phase 8 — Overlay enforcement (COJ, auto-debit, forum)

**Goal:** Enforce cross-cutting overlays regardless of CFDL tier.

**Tasks:**
1. Funder-matching stage: cross-check funder's contract template (if available) against merchant's state COJ status. Flag where funder uses COJ in COJ-banned state.
2. Pre-submission gate: enforce FL/GA broker advance-fee (verify Phase 4 wiring).
3. Pre-submission gate: enforce TX first-priority-lien rule (verify Phase 4 wiring).
4. Pre-submission gate: enforce VA forum-restriction (decline or warn on deals where funder's contract specifies non-VA forum).
5. Extend funder records: `uses_coj` (bool), `forum_state` (text), `autodebit_structure` (text).

**Acceptance:**
- COJ overlay fires in COJ-banned states
- Forum overlay fires for VA with non-VA forum
- Auto-debit overlay fires for TX (already Phase 4)

**Watch out:** Missing funder metadata → manual review, not silent skip.

**Counsel gate** for funder-data interpretation.

**Effort:** 2 days + counsel cycle (1 week elapsed).

---

## 19. Phase 9 — Industry-standard detectors + scoring expansion

**Goal:** Close the detector gaps vs Ocrolus / Heron / MoneyThumb.

**Tasks:**
1. Implement detectors from §6.4 in priority order:
   - `unreconciled_internal_transfer` (highest value — catches hidden bank accounts)
   - `mca_payoff_signature`
   - `customer_concentration`
   - `processor_holdback_detected`
   - `acceleration_clause_triggered` (new hard-decline rule)
   - `unauthorized_withdrawal_dispute` (new hard-decline rule)
   - `chargeback_velocity`
   - `payroll_absent`
   - `bank_account_changed_mid_period`
   - Forensics: `font_inconsistency`, `editor_signature`, `page_layer_anomaly`
   - `ai_generated_statement` (composite, score only — don't auto-decline)
2. Add scoring factors from §5.7 (counterparty signals): `payroll_present`, `customer_concentration`, `top_5_counterparty_revenue_share`, `top_5_counterparty_expense_share`.
3. Add hard-decline rules: `acceleration_clause_triggered`, `unauthorized_withdrawal_dispute_active`, `bank_statement_tampering_confirmed` (composite).
4. Update scoring snapshot tests against synthetic corpus.
5. Add per-deal **paper grade** (A/B/C/D) computation and surface prominently on dossier.

**Acceptance:**
- All new detectors pass synthetic-corpus tests
- Paper grade displays on dossier
- New hard-decline rules fire correctly

**Effort:** 5 days. No counsel gate.

---

## 20. Phase 10 — Operator-override flywheel + funder reply ingestion

**Goal:** Capture the data needed to tune AEGIS over time.

**Tasks:**
1. Wire `overrides` table from §9.4 into the dossier UI: one-click "operator override" button with modal (reason code dropdown + freeform note + optional per-pattern false-positive toggles).
2. Snapshot of `decisions` row at override time (so operator's override is against frozen recommendation).
3. **Funder reply ingestion:** parser for funder approval/decline/counter emails. When detected, stamp outcome onto matching deal's `overrides` row (if any) and onto the deal itself.
4. Quarterly report: confusion matrix per reason code (operator overrode AEGIS decline → outcome = funded vs declined-by-funder vs charged-off). This is the data that tunes thresholds.

**Acceptance:**
- Override button on dossier writes to `overrides`
- Funder reply ingestion populates outcomes
- Quarterly report generates

**Watch out:** Funder emails are unstructured. Use the same two-pass LLM-extract + deterministic-validate pattern. Don't trust LLM to invent outcomes.

**Effort:** 4 days. No counsel gate.

---

## 21. Phase 11 — Operational reliability + growth-ready infrastructure

**Goal:** Catch bad days hours sooner. Move from a single-box deployment to infrastructure that can scale with deal volume and operator count without a rewrite.

**Tasks:**
1. **Alerting (Healthchecks.io + ntfy/Pushover, or migrate to Grafana Cloud at higher volume):**
   - Heartbeats: `aegis-web` and `aegis-worker` ping every 5min via systemd timer
   - Event alerts (10min eval): Bedrock 5xx/throttle >3/10min; Zoho 401/403 immediate; Zoho HMAC fails >0/hr; `parse_status='manual_review'` >25% over last 20 deals; OFAC cache >6 days; arq queue depth >20; disk >80%
2. **Bedrock per-deal cost tracking:** log token counts + computed cost per analysis; weekly digest; unit-economics view (cost per deal, cost per funded deal, cost as % of revenue).
3. **Rate limiting verification:** confirm `CLAUDE.md` claim that "all endpoints rate limited" is actually true on every endpoint. Add tests. Per-user and per-IP buckets so multi-operator growth doesn't break the limits.
4. **Backup/DR plan (current → growth-ready):**
   - Daily Supabase logical dump to off-Hetzner storage (S3 or Backblaze)
   - Hourly WAL shipping when volume justifies it
   - Document recovery procedure (spin up new box from systemd configs in repo, restore Supabase, point Cloudflare Tunnel)
   - Test recovery quarterly
5. **Horizontal-scale readiness (do now, activate later):**
   - All worker code stateless — no in-process caches, no local-disk state beyond `/tmp` scratch
   - All session state in Redis (already true via arq)
   - File uploads to object storage (S3-compatible), not local disk
   - Document the path from 1 box → multi-worker on one box → multi-box behind a load balancer. Don't build the LB yet; just don't preclude it.
6. **Real-statement corpus expansion:** 2–3 statements per fintech bank (Mercury, Brex, Bluevine, Novo, Relay, Lili, Found) added to real corpus. Operator-validated manifests. Corpus grows as deal mix changes.
7. **Parser-drift regression loop:** CORPUS=1 includes real corpus, not just synthetic. Every parser change re-runs.
8. **Multi-operator readiness:**
   - Authentication via Cloudflare Access already supports multiple users; verify each AEGIS action records the actor
   - Add role concept: `underwriter`, `compliance_reviewer`, `admin` (even if only one role is used today, the schema supports more)
   - Audit log includes actor for every state change (already partially true; ensure 100%)

**Acceptance:**
- Alerts fire on simulated failures
- Bedrock cost + unit economics show in weekly digest
- DR procedure tested
- All worker code is stateless (no local-disk dependencies beyond scratch)
- Roles exist in the schema; every audit row has an actor
- Fintech-bank real corpus exists with >2 statements per bank
- Real corpus runs in CI

**Effort:** 5 days. No counsel gate.

---

## 22. Phase 12 — Watch list automation + §1071 prep

**Goal:** Stay ahead of pending state bills; be ready for federal data-collection if/when it applies.

**Tasks:**

**Watch list:**
1. Monthly arq cron generates digest of all watchlist states from `states.yaml`. Posts to operator's Slack/email. Includes bill status, last legislative action, projected likelihood, days since last review.
2. Operator promotion workflow: when operator marks watchlist state as "promoted to Tier 1," AEGIS triggers checklist (build folder, draft template, schedule counsel review).

**§1071:**
1. Add demographic/pricing data fields to application form + deal record.
2. Build §1071 report exporter (CSV per CFPB spec).
3. Threshold detector: when annual covered-transaction count crosses 100, warn that §1071 reporting may apply.

**Acceptance:**
- Monthly review digest delivered
- Promotion workflow tested
- §1071 data fields in schema
- Exporter generates correctly formatted CSV
- Threshold warning fires

**Watch out:** §1071 implementation has been delayed by litigation. Confirm current status with counsel before activating the exporter. The CFPB applies §1071 in volume-based phases, so Commera's reporting obligations will activate at a defined annual covered-transaction count — build the data capture now, monitor the threshold, and turn on reporting when the rule applies.

**Counsel gate** for §1071 status check.

**Effort:** 3 days + counsel cycle (1 week elapsed).

---

## 23. Execution sequence and time estimates

| Phase | Engineering | Counsel gate | Calendar |
|---|---|---|---|
| 1. Foundation | 2 days | No | 2 days |
| 2. Decision snapshot | 3 days | No | 3 days |
| 3. Anti-drift | 1 day | No | 1 day |
| 4. State misclassification | 2 days | **Yes** (TX, FL/GA) | 1 week |
| 5. Disclosure templates | 5 days | **Yes** (each template) | 2–3 weeks |
| 6. Renewal queue | 3 days | **Yes** (CA, NY logic) | 1 week |
| 7. Audit + retention | 2 days | No | 2 days |
| 8. Overlay enforcement | 2 days | **Yes** (funder data) | 1 week |
| 9. Detectors + scoring | 5 days | No | 5 days |
| 10. Override + funder reply | 4 days | No | 4 days |
| 11. Operational reliability + growth-ready infra | 5 days | No | 5 days |
| 12. Watch list + §1071 | 3 days | **Yes** (status) | 1 week |

**Total engineering:** ~37 days
**Total elapsed including counsel cycles:** ~8–10 weeks

**Recommended order (the one I'd actually do):**

1. Phases 1 + 2 + 3 first — foundation, snapshot, anti-drift. No counsel needed. Unblocks everything.
2. Phase 4 — state misclassification (deal-flow win, send to counsel in parallel with starting next phase).
3. Phase 11 — operational reliability. Catch failures while later phases roll out.
4. Phase 9 — detectors + scoring. No counsel needed; biggest underwriting-quality win.
5. Phase 5 — disclosure templates (long counsel cycles, run in parallel with 6).
6. Phase 6 — renewal queue.
7. Phase 7 — audit + retention.
8. Phase 8 — overlay enforcement.
9. Phase 10 — operator override + funder reply (flywheel).
10. Phase 12 — watch list + §1071.

---

## 24. Buy / build / skip decisions

| Capability | Buy | Build | Skip | Rationale |
|---|---|---|---|---|
| Bank statement parsing | | ✅ Build | | Already built. Differentiator. Two-pass + source-id more defensible than vendor. |
| Tampering detection | | ✅ Build | | Layers 1, 3, 5 are mandatory build. Layer 2 (visual) light. |
| AI-generated fake detection | Watch VerifyPDF/Inscribe | ✅ Build behavioral | | Layer 4 build; pure vision-based fine to outsource later if threat rises. |
| Bank-data API | ✅ Plaid (when offered) | | | Ingest as supplement. PDFs are ground truth. |
| OFAC SDN | | ✅ Have | | Free data, no vendor needed. |
| Identity/KYC | Watch ComplyAdvantage/Persona/Alloy | | Skip for now | Solo operator + commercial-only. Revisit if AML-program-required volume. |
| NAICS data | | ✅ Build (small lookup) | | Static data. |
| State disclosure templates | | ✅ Build | | The work IS the templates. Buying generic is more dangerous than not having. |
| Funder appetite DB | | ✅ Build (Phase 7B) | | Bespoke per broker. No vendor knows your funder list. |
| CRM / Servicing | ✅ Zoho | | | Done. Keep AEGIS out of this layer. |

---

## 25. Out of scope

Be explicit:
- Funder-side compliance (Phase 8 detects, doesn't fix)
- Marketing/advertising compliance (separate effort)
- Personal-guarantee variations by state
- State tax-treatment differences
- State bankruptcy treatment of MCAs
- State usury laws where MCA recharacterized as loan (add `usury_recharacterization_risk` factor later)
- FTC compliance beyond §1071

---

## 26. Open research questions (track quarterly)

1. AI-generated bank statement frequency at our volume — track via `ai_generated_suspected` quarantine pipeline
2. CFPB §1071 threshold — recheck quarterly with counsel
3. NJ S1760, MD SB754/HB693, PA HB1792 status — monthly watch list digest
4. Bedrock cost per deal — monitor continuously; review unit economics quarterly as volume grows
5. Funder appetite drift — funder PDF import endpoint on weekly cron with confidence-thresholded auto-update

---

## 27. Sources

**Industry research:**
- Ocrolus product docs (Detect, Forensic Analytics, Counterparty Detection)
- Heron Data, MoneyThumb, Inscribe, DocuClipper, Lido, LlamaParse, Statement Extract, Floowed, Klippa, VerifyPDF, Precisa
- LendSaaS, LendFoundry, Centrex, Cloudsquare, MCA Track, Onyx IQ — competitive landscape
- FundingEstimate, MCashAdvance, AMP Advance, Crestmont, United Capital Source, Liberty Capital, Nav — metric thresholds
- DailyFunder forum — practitioner signal

**State law (May 2026):**
- Venable LLP — State Commercial Financing Disclosure Laws (March 2026)
- Alston & Bird — Commercial Financing Disclosure Requirements (January 2026); GA/FL/CT analysis
- American Bar Association — State Survey of Standard Commercial Financing Disclosure Laws (Spring 2025)
- Holland & Knight — Texas HB 700 analysis (June 2025)
- Mayer Brown — TX HB 700 (September 2025); LA HB 470 (August 2025)
- Buchalter — CA SB 362 (February 2026); NY CFDL Regulation
- Sheppard Mullin — TX HB 700 (July 2025)
- Manatt — TX + LA Disclosure Laws (July 2025)
- DLA Piper — Multi-state commercial financing (January 2023)
- Onyx IQ — State disclosure laws reference (April 2025)
- Cobalt Intelligence — NJ S1760 (January 2026)
- Credible Law — MCA Laws by State (March 2026)
- NY DFS 23 NYCRR 600 official text
- TX OCCC HB 700 advance notice (November 2025)
- TX Capitol HB 700 analysis
- LA Legislature HB 470 text
- MD Legislature SB754/HB693

Each per-state folder's `01_statutes.md` should link to regulator's official page + 2–3 reputable legal commentaries.

End of master plan.
