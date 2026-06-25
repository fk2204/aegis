# AEGIS Flag Glossary

Source-of-truth reference for every flag the AEGIS parser emits. Used by operators reviewing deals, and as the content source for in-app tooltips, drill-down explanations, and onboarding material.

**Conventions**

- *Severity numbers are facts* — copied from the detector code in `src/aegis/parser/patterns.py`, `src/aegis/parser/metadata.py`, and `src/aegis/parser/aggregate.py`. Severity is the contribution to `fraud_score` (capped at 100), NOT an automated decline.
- *Severity bands the UI uses* — `≥30 = red (neg)`, `15-29 = amber (warn)`, `<15 = green (pos)`. Set by `src/aegis/web/_pattern_cards.py:_severity_band`.
- *"How serious" framing is operator synthesis* — informed by code comments and severity numbers, but the underwriting call belongs to the operator. Items marked **[PENDING REVIEW]** are awaiting operator pressure-test against real funder appetite before the framing is treated as authoritative.
- No flag in this system triggers an automated decline. The operator decides.

**Maintenance** — when a new detector lands in `patterns.py`, `metadata.py`, or `aggregate.py`, add its entry here in the same commit. The matching operator copy in `src/aegis/web/_pattern_cards.py:PATTERN_COPY` also needs an entry (there's a regression test enforcing this for pattern codes).

---

## Baseline intake requirements

Before any flag interpretation: every deal must clear our baseline intake bar before underwriting effort goes into it.

- **~6 months minimum business history.** The business itself, not just the bank account.
- **3–4 bank statements** covering the trailing months.

Several flags below reference this baseline. Most importantly, read `recent_account_opening` in light of this rule: if a flag interacts with "established business + recent bank switch," the operator's first move is to ask the merchant for the prior bank's statements to satisfy the 6-month rule. If the merchant can supply them, continue review on those. If they can't, or the business itself is under 6 months, decline.

---

## 1. Stacking & funder-position flags

### `mca_stacking` — severity 15 per position, capped at 50

- **Detects:** Groups debits by normalized description. Either (a) ≥3 occurrences containing a known funder name (OnDeck, Credibly, Kapitus, etc. — full list in `KNOWN_FUNDERS`), OR (b) ≥10 occurrences with a generic MCA term ("daily pmt", "advance", "remit", etc.) AND median day-spacing ≤2 (daily cadence). Generic single words alone do NOT fire.
- **Why it matters:** Active MCAs on a deal you're about to fund = stacking risk. Funders price-kill on this.
- **How serious:** Material — this is the deal-defining flag. 1 position is normal renewal territory; 2+ active positions is hard pause.
- **Do:** Confirm with the merchant which funders are still active, request balances + payoff letters, count against funder stacking limits before any submission. Drill into the stacking card to see per-position daily equivalents.

### `mca_payoff_signature` — severity 15

- **Detects:** Any single debit > $5,000 whose description contains a known funder token. Fires regardless of whether it's already part of a recurring position.
- **Why it matters:** Recently-paid-off MCA still counts in renewal-likelihood scoring even if not currently active.
- **How serious:** Look-closer. Context, not a decline.
- **Do:** Ask the merchant when this funder was paid off and confirm no balance remains. If recent, factor into renewal narrative.

### `paydown_mca_suspected` — severity 25

- **Detects:** Same-payee debits with monotonically descending amounts (≥5 events, ≤5% noise on the way down, ending ≤85% of starting amount).
- **Why it matters:** Existing MCA being paid down → renewal-stage merchant.
- **How serious:** Material — not a kill, but reframes the deal.
- **Do:** Confirm with broker which MCA is being paid down, get remaining balance, and decide whether to wait for full payoff before stacking a new position.

### `withdrawal_acceleration` — severity 20

- **Detects:** Last-7-day MCA-debit COUNT > 1.5× prior weekly average. Requires statement ≥21 days and ≥4 `mca_debit` rows total.
- **Why it matters:** Catches stacking *accelerating* in the trailing week — more frequent debits, not bigger.
- **How serious:** Material. Often paired with stacking-count creep.
- **Do:** Ask: "Did you take any new advances in the last 2 weeks?" If yes, get the new funder and balance before submission.

### `acceleration_clause_triggered` — severity 50

- **Detects:** A recurring MCA position (≥3 prior occurrences) where the latest debit is 5–10× the median of priors, no recurring occurrences after, and ≥7 days have passed since the latest.
- **Why it matters:** Funder called the loan after default — they yanked the full remaining balance. The merchant defaulted on a prior funder.
- **How serious:** **Decline.** Outside our risk appetite. Don't invest time submitting to funders.
- **Do:** Quick confirmation of what the flag shows — drill into the position to verify the latest-debit-vs-median ratio genuinely looks like an acceleration (not a one-off large invoice to the same payee) — then decline.

### `unauthorized_withdrawal_dispute` — severity 35

- **Detects:** A credit row containing reversal/dispute keywords ("reversal", "unauthorized", "ach return credit", etc.) that pairs with a prior `mca_debit` within 14 days at near-equal amount.
- **Why it matters:** Merchant fought (and won) a funder withdrawal. Highly material — merchants who dispute their funders are unfundable for the next funder.
- **How serious:** Near-kill. Single-event fire is high specificity.
- **Do:** Ask the merchant directly which funder this was, why the dispute, and how it resolved. Most funders won't touch this.

---

## 2. Revenue-fabrication flags

### `wash_deposit_suspected` — severity 35

- **Detects:** Deposit/withdrawal pairs of near-equal amount (within 2%) within 5 calendar days. Fires at ≥2 pairs.
- **Why it matters:** Money moving in then back out inflates apparent deposit volume without real revenue. Classic statement-padding tactic.
- **How serious:** Material — pause and verify. False positives possible (legitimate inter-account moves) but each pair pushes the read toward fabrication.
- **Do:** Examine the paired transactions in drill-down. If pairs are owner transfers between merchant's own accounts, ask for both account statements. If unexplained, decline.

### `duplicate_deposits_detected` — severity 30

- **Detects:** Same posted_date + exact-equal Decimal amount appears in ≥2 deposit rows.
- **Why it matters:** Legitimate when a multi-channel merchant has truly identical sales (rare); usually a sign of fabricated row insertion.
- **How serious:** Material. Real merchant statements rarely have penny-exact same-day duplicates.
- **Do:** Look at the duplicate rows — if they have different counterparty descriptions, lower suspicion; if identical, request the original PDF and verify against the bank portal directly.

### `synthetic_low_variance` — severity 25

- **Detects:** Coefficient of variation < 15% across deposit amounts (requires ≥10 deposits).
- **Why it matters:** Real merchant cash flow varies. Uniformly-sized deposits often indicate fabrication or ACH-padding.
- **How serious:** Look-closer. Some legitimate businesses (subscription, fixed retainers) have low variance — don't auto-decline.
- **Do:** Ask what their revenue model is. Subscription/recurring-billing makes this benign; cash retail with low-variance deposits is a red flag.

### `round_number_deposits` — severity 15

- **Detects:** > 75% of deposits are exact multiples of $100 (requires ≥10 deposits).
- **Why it matters:** Real revenue rarely lands on clean multiples.
- **How serious:** Look-closer; weak signal on its own, stronger when paired with other revenue-fabrication flags.
- **Do:** If this fires alone, low concern (B2B invoicing with round invoices is plausible). Combined with `synthetic_low_variance` or `duplicate_deposits`, treat the combo as material.

### `preloan_spike` — severity 25

- **Detects:** Last-7d OR last-14d deposit totals exceed 2.5× the prior-period weekly average. Requires statement ≥21 days. Bug fix from TS: only scores once even if both windows fire.
- **Why it matters:** Classic pad-the-account-before-applying pattern.
- **How serious:** Material — pause and verify.
- **Do:** Ask the merchant directly about the trailing-period deposits. Request prior-month statements (the spike should not appear in earlier months if revenue is real).

### `deposit_velocity_spike` — severity 20

- **Detects:** A 7-day rolling window contains > 3× the period-average daily deposit COUNT. Requires statement ≥21 days, ≥10 deposits.
- **Why it matters:** Different signal from dollar-spike — catches deposit-row stuffing to look busier than reality.
- **How serious:** Look-closer.
- **Do:** Drill into the high-count window — if those rows are individually small and look like manual entries, raise concern. If they're legitimate processor batches, benign.

---

## 3. Cashflow-stress flags

### `nsf_clustering_short` — severity 20

- **Detects:** > 3 NSF fees in a statement < 20 days long.
- **Why it matters:** Short-window NSF clustering = structural cashflow stress, not a one-off accident.
- **How serious:** Material — informs deal grade, not necessarily kill.
- **Do:** Factor into grade. Request a longer history (90 days) to confirm whether this is structural.

### `nsf_late_concentration` — severity 20

- **Detects:** ≥3 NSFs in the final 30 days of a statement ≥30 days long.
- **Why it matters:** Cash position is deteriorating, not improving. Late-period decline is worse than early-period bumps that resolved.
- **How serious:** Material.
- **Do:** Look at the trend across the full period — if NSFs trend up, decline-leaning. If clustered around a known event (lost large customer), ask merchant for context.

### `chargeback_velocity` — severity 10/15/20 by path

- **Detects:** Debits containing chargeback/refund keywords ("chargeback", "refund", "return ach", "dispute", "merchant return", "credit reversal"). Three paths:
  - Statement < 21 days: fires at severity 15 when ≥5 chargeback rows.
  - Statement ≥ 21 days, last-14d count > 1.5× prior-fortnight average AND total ≥ 3: fires at severity 20 (acceleration).
  - Statement ≥ 21 days, no acceleration, but total ≥ 6: fires at severity 10 (static-count signal).
- **Why it matters:** Chargeback frequency is a leading indicator of B2C merchant distress and dispute risk on the funder's holdback.
- **How serious:** Acceleration path = material. Static-count = look-closer.
- **Do:** Cross-check against revenue scale (a 1% chargeback rate is normal; > 3% is alarming). For e-commerce merchants, ask for their processor's dispute report.

### `payroll_absent` — severity 10

- **Detects:** Period ≥21 days AND total revenue ≥ $50k AND no payroll-processor activity (no ADP/Gusto/Paychex/etc., no payroll-categorized rows).
- **Why it matters:** Real operating businesses at $50k+/mo revenue almost always have payroll. Absence suggests revenue may not represent real operating activity.
- **How serious:** Look-closer.
- **Do:** Ask how they pay employees/contractors. 1099-only businesses are plausible explanations. If they claim W2 employees with no payroll trace, raise concern.

---

## 4. Concentration & business-fundamentals

### `customer_concentration` (scored Pattern) — severity 10/20/30 by tier

- **Detects:** Top single counterparty's share of revenue. Tiers: 31–40% = 10, 41–60% = 20, ≥61% = 30. Only fires above 30%.
- **Why it matters:** Single-customer dependency. Lose that customer and the merchant can't service the advance.
- **How serious:** Material above 50%, look-closer 31–50%.
- **Do:** Ask who the customer is, contract length, and whether it's renewable. > 70% concentration = pause regardless of other signals.

### `processor_holdback_detected` — severity 20

- **Detects:** ≥10 deposits from a known card processor (Stripe/Square/Toast/etc.) AND daily-summed coefficient of variation ≥ 0.50. Requires period ≥14 days.
- **Why it matters:** Variable processor payouts suggest an in-place MCA holdback (funder taking a cut before payout reaches the bank).
- **How serious:** Material — strongly implies an undisclosed active MCA.
- **Do:** Ask directly: "Is any of your card processor payout split or held by a funder?" Cross-check against `mca_stacking` count.

---

## 5. Hidden-account flags

### `unreconciled_internal_transfer` — severity 15 base, scales up to 40

- **Detects:** Transfer-OUT > $500 with no matching transfer-IN (within $1, within ±3 days) in the bundle. Severity = `15 + 5×(unmatched_count − 1)`, capped at 40.
- **Why it matters:** Highest-value detector per master plan §6.4. Money leaving the visible accounts to an undisclosed account — often hosting an undisclosed MCA.
- **How serious:** Material — possibly hard pause. Caveat in code: in single-statement mode, every transfer-out is unreconciled by definition, so single-statement fires are softer.
- **Do:** Request all bank account statements. If merchant has multiple accounts they didn't submit, find out why — they may be hiding an MCA-debited account.

---

## 6. Recency / account-age

### `recent_account_opening` — severity 15 (only fires when account age < 60 days)

- **Detects:** Period start is < 60 days before today. Fires at severity 15; between 60–90 days returns nothing.
- **Why it matters:** A sub-60-day account usually can't satisfy our 6+ months baseline on its face.
- **How serious:** Likely decline, with one specific exception below.
- **Do:** Determine whether the *business* is also under 6 months, or just the *bank account*:
  - **Business < 6 months → decline.** Fails our baseline; no path forward.
  - **Established business (≥ 6 months) that recently switched banks → request the prior bank's statements before declining.** With 3–4 statements from the prior bank, the deal can still meet our baseline. Continue review on those.
- **Note:** This flag renders on the score breakdown panel as a hard-decline reason, not in the pattern-cards block (`_RENDERED_ELSEWHERE` in `_pattern_cards.py`).

---

## 7. PDF tampering (metadata layer)

These come from `pikepdf` inspection of the PDF binary, before transaction extraction. They contribute to a separate `metadata.fraud_score`.

### `incremental_saves: N EOF markers` — score +40

- **Detects:** More than 1 `%%EOF` trailer marker (regex-anchored to genuine trailer ends), **on PDFs without a digital signature**. Digitally-signed PDFs are suppressed by the detector — it walks the PDF's `AcroForm.Fields` (with `/Kids` descent) for a `/FT == /Sig` field; presence of any signature means the additional EOFs are explained by the signature's incremental update and the flag does not fire.
- **Why it matters:** Multiple EOFs on an *unsigned* PDF mean the document was saved incrementally — content added after the original was produced. Almost never legitimate on a bank-printed statement.
- **How serious:** Material — strong tampering signal when it fires. The false-positive class on signed bank exports / KYC documents is eliminated by the signature-aware check, so when this flag does fire, take it seriously.
- **Do:** Reject the PDF. Request the original from the bank portal directly (not a re-saved copy).
- **Limitation (v1):** Signature detection is *presence-based*, not cryptographic. A malicious actor could forge a `/Sig` object to bypass the check. Cryptographic signature validation is v2 work (would need a new dep — pyhanko or equivalent). The realistic-threat-model tradeoff is the right one for now: legitimate signed exports stop dominating the manual_review queue, real tampering still surfaces on the (unsigned) cases that matter.

### `modified_Nmin_after_creation` — score +15 (5–120 min) or +30 (> 120 min)

- **Detects:** Modification date significantly after creation date.
- **Why it matters:** Banks generate statements once. Significant gap between creation and modification = somebody opened and re-saved it.
- **How serious:** Look-closer if minutes, material if hours.
- **Do:** Get the original from the bank portal.

### `editor_detected: <name>` — score +35 (hard editors) or +15 (medium)

- **Detects:** PDF Producer string matches a known editing tool.
  - Hard list: Foxit PhantomPDF, Nitro Pro, PDFescape, Smallpdf, iLovePDF, Sejda, PDF-XChange Editor, CutePDF, PDFill, iText, PyPDF, Ghostscript.
  - Medium list: Adobe Acrobat, Preview, Word, LibreOffice, Google Docs, Pages.
- **Why it matters:** Banks don't produce PDFs with editing tools. Hard editors with PDF-modify capability are red flags.
- **How serious:** Hard-list = material. Medium-list = look-closer (Acrobat/Preview legitimately show up when a merchant print-to-PDFs from their bank portal).
- **Do:** Hard-editor → request original. Medium-editor + other signals → request original; medium-editor alone → context only.

### `personal_author: <name>` — score +20

- **Detects:** PDF `/Author` field contains a personal-name pattern (First Last, etc.) AND is not an institution name (banks excluded via token list).
- **Why it matters:** Bank-generated PDFs don't list a person's name as author.
- **How serious:** Material — strong tampering signal.
- **Do:** Reject the PDF.

### `stripped_metadata` — score +28

- **Detects:** No creation date, no producer, no creator strings in docinfo.
- **Why it matters:** Either an unusually privacy-scrubbed export or someone wiping metadata to hide tampering.
- **How serious:** Material on its own; severe combined with any other tampering flag.
- **Do:** Request the original direct from the bank portal.

### `page_size_inconsistency` — score +30

- **Detects:** Pages have different MediaBox sizes.
- **Why it matters:** Real bank statements are single-template. Mixed page sizes = pages from different sources stitched together.
- **How serious:** Material.
- **Do:** Reject.

### `xref_offset_mismatch` — score +25

- **Detects:** Last `startxref` offset doesn't point at `xref` or a valid object header.
- **Why it matters:** Hex-edited PDFs frequently break this. Real PDFs don't.
- **How serious:** Material.
- **Do:** Reject.

### `font_inconsistency: N page(s) have no font overlap` — score +20

- **Detects:** Pages whose embedded fonts share zero overlap with all other pages.
- **Why it matters:** Pasted-in content from an external source uses different fonts.
- **How serious:** Material when N is large; weak when N = 1 (could be a watermarked page).
- **Do:** Inspect flagged pages. If they contain transaction rows, reject.

### `page_layer_anomaly: N page(s) off-mode /Contents stream count` — score +15

- **Detects:** Pages with a different number of `/Contents` streams than the document mode.
- **Why it matters:** Pages assembled by PDF-overlay often carry multiple Contents streams. Not conclusive — some legit exports also do this.
- **How serious:** Look-closer; contributes to the composite, not standalone.
- **Do:** Combined with other tampering flags = reject. Alone = note and proceed.

---

## 8. Soft signals (aggregate-emitted, non-scoring)

These appear in `all_flags` as strings, surfaced on the merchant detail soft-signals card. They do NOT add to `fraud_score`.

### `top_counterparty_concentration:NN%_(payee)`

- **Detects:** Top deposit counterparty as % of revenue. Requires ≥3 distinct counterparties.
- **Why it matters:** Different from the scored `customer_concentration` Pattern — this is informational for *every* deal. The scored Pattern only fires above 30%.
- **Do:** Read for context. If > 50%, expect funders to discount.

### `payroll_cadence:weekly_NN%_of_revenue` (or biweekly/monthly/irregular)

- **Detects:** Median day-spacing between payroll rows. Buckets: weekly (6–8d), biweekly (13–16d), monthly (27–32d), else irregular. Also reports payroll dollars as % of revenue when revenue > 0.
- **Why it matters:** Confirms real-business operating signal. Irregular cadence on a "12 employees" business is a tell.
- **Do:** Use as supporting evidence the business is real. Irregular + claimed-W2-employee count is a red flag.

### `nsf_on_negative_days:N_of_M`

- **Detects:** How many of M total NSFs landed on a day where end-of-day balance was negative.
- **Why it matters:** Distinguishes processing anomalies (NSFs scattered) from real cashflow stress (NSFs cluster on negative-balance days).
- **Do:** High overlap ratio = stress-driven NSFs. Affects grade more than decline decision.

### `adb_partial_coverage:N/M`

- **Detects:** ADB calculation skipped N of M days because the statement was in printed-balance mode but those days lacked a printed running balance.
- **Why it matters:** Data-quality flag, not deal-quality. Means ADB shown is averaged over fewer days than the full period.
- **Do:** If N small relative to M, ignore. If N > 20% of period, ADB is less reliable.

---

## 9. Composite / signal-only

### `ai_generated_score` (0–100, stored on `PatternAnalysis`, never a Pattern)

- **Detects:** Weighted composite of three components: (1) share of descriptions with no all-caps tokens, (2) share of rows lacking long digit runs (real statements have trace IDs), (3) round-deposit share. Weighted 0.4 / 0.4 / 0.2.
- **Why it matters:** LLM-generated fake statements are "too clean" — full sentences, no trace IDs, generic counterparty names, rounded amounts.
- **How serious:** Signal-only, never auto-decline. High score (> 70) is a strong tell when combined with metadata tampering or revenue-fabrication flags.
- **Do:** Score > 70 + any metadata flag = reject. Score > 70 alone + clean metadata = unusual but possible (privacy-scrubbed export); request original.

---

## 10. Shadow-mode flags (severity 0, operator-validation only)

These detectors emit `Pattern(severity=0)` into `PatternAnalysis.shadow_patterns` — a separate list from the live `patterns` field. They do NOT add to `fraud_score`, do NOT alter hard-decline reasons, and do NOT change `parse_status`. Per CLAUDE.md "Decision-boundary changes — deliberate + shadow-first," each new detector lands shadow-first; the operator corpus-validates false-positive rate, then a follow-up commit flips it into the scored path behind a config gate.

### `structured_deposit_cluster:N_deposits_in_14_day_window_dates=YYYYMMDD,...` — shadow, severity 0

- **Detects:** ≥3 deposits classified as `deposit`, `ach_credit`, or `wire_in` in the BSA-avoidance band ($8,500 to $9,999.99) within any 14-day rolling window.
- **Statutory context:** 31 USC § 5324 (structuring is a federal crime) and 31 CFR § 1010.311 ($10K Currency Transaction Report threshold). FinCEN treats repeated just-under-$10K deposits — "smurfing" — as the textbook structuring pattern.
- **Why it matters:** Repeat near-threshold deposits indicate the merchant may be intentionally evading CTR reporting. A federal-crime signal in MCA underwriting context: even funders who would otherwise touch the deal typically will not.
- **Cash-only caveat:** AEGIS cannot distinguish cash from check / wire from a bank statement row. The detector fires on ANY in-band deposit; the operator interprets context. A $9,500 wire is almost never structured; a $9,500 over-the-counter deposit on a 3-in-14d cluster is the textbook signal.
- **Threshold rationale:**
  - Band floor $8,500 — FinCEN-typical smurfing floor. Catches the band without flagging routine business deposits below $8,500.
  - Band ceiling $9,999.99 — last cent under the CTR threshold. $10,000 exact is reported by the bank, so no avoidance.
  - ≥3 in 14 days — FinCEN "pattern" floor. Two in two weeks is not a pattern.
- **How serious:** Shadow-only on first ship. Operator validates against corpus before any decline-path wiring. A confirmed cluster of cash-style descriptors is material; a cluster of wires is usually a false positive.
- **Do:** Drill into the source rows. If they look like over-the-counter cash deposits, ask the merchant directly about the deposits' origin. If wires from known business counterparties, dismiss. If unexplained, decline-leaning and document the rationale.
- **Source:** `src/aegis/parser/patterns.py:_detect_structured_deposit_cluster`. Source-ids on the emitted flag are the exact UUIDs of the cluster's contributing transactions.

### `lender_proceeds_excluded:{count}_${total}_({names})` — shadow

- **Lender proceeds excluded from revenue.** Deposits classified as MCA / SBA / LOC funder proceeds were filtered out of `true_revenue` so the metric reflects merchant-generated cash flow. Verify the funder names match disclosed obligations.
- **Companion:** `lender_proceeds_excluded_row:{name}_${amount}_{uuid}` — one per excluded transaction; the source-id ties back to the audit CSV.
- **Source:** `src/aegis/parser/aggregate.py`.

### `mca_position_fuzzy_candidate:{funder}_{ratio}_{count}_{first}_{last}` — shadow

- **Possible MCA stacking — descriptor variant.** Debits with descriptors that fuzzy-match a known MCA funder name (similarity ≥85%) without an exact substring hit. Likely a typo / abbreviation variant of a real position.
- **Source:** `src/aegis/parser/patterns.py:_detect_fuzzy_mca_candidates`.

### `mca_disguise_candidate:{term}_{count}_{median_days}` — shadow

- **Possible MCA — generic descriptor cadence.** A product-neutral phrase ("settlement advance", "revenue based financing") appears on 10+ debits with ≤2-day median spacing — the cadence of a real MCA holdback behind generic language.
- **Source:** `src/aegis/parser/patterns.py:_detect_disguise_candidates`.

### `mca_same_day_cluster:{date}_{funder_count}_({A|B|C})` — shadow

- **Multiple funders same day.** Three or more distinct MCA funders debited on the same business day — strong indicator of late-stage stacking.
- **Source:** `src/aegis/parser/patterns.py:_detect_same_day_cluster`.

### `daily_balance_continuity_break:{date}_expected_{x}_actual_{y}_diff_{d}` — shadow

- **Daily balance off by cents.** A day's expected closing balance and the next day's opening balance disagree by ≥$0.01 (the routing-level check uses $1.00). Often benign rounding; can flag surgical row swaps that shift cents without breaking the looser gate.
- **Companion:** `daily_balance_continuity_breaks_count:{N}` — summary count across the statement.
- **Source:** `src/aegis/parser/validate.py:_shadow_check_daily_balance_continuity`.

### `transaction_id_sequence_gap:{from}_{to}_{missing}` — shadow

- **Transaction-id sequence gap.** A populated sequential id / reference / confirmation column skips one or more numbers. Possible evidence of deleted rows in the source PDF.
- **Source:** `src/aegis/parser/validate.py:_shadow_check_transaction_id_sequence_gaps`.

### `adb_coverage_thin:skip_ratio={n}pct_threshold={t}pct_would_route_review` — shadow

- **Average daily balance — thin coverage.** More than 10% of days in the window are missing a daily-balance anchor, so `avg_daily_balance` is computed over too few days to be trusted. Under the proposed policy this would route to manual review.
- **Source:** `src/aegis/parser/pipeline.py:_adb_coverage_thin_flag`.

### `nsf_corroboration_missing:{date}_${amount}_{snippet}_would_route_review` — shadow

- **NSF lacks corroboration.** An NSF-fee row fired but the surrounding evidence (negative running balance, same-day chargeback / return token) is absent. Could be misclassification.
- **Companion:** `nsf_low_confidence:{date}_${amount}_conf{N}_{snippet}_would_route_review` — independent signal for NSF rows with classifier confidence <80.
- **Source:** `src/aegis/parser/nsf_secondary.py`.

### `state_enforcement_concern:{TX_HB700_tx_merchant_review|FL_GA_advance_fee_prohibition|FL_GA_advance_fee_prohibition_for_this_funder}` — shadow

- **State enforcement concern.** Merchant state + funder profile lands on a known regulatory watchlist. Operator-side review hint only; no tier or recommendation change.
- **Source:** `src/aegis/scoring/score.py` (R3.4); also `src/aegis/scoring/match_funders.py` for the per-funder variant.

### `seasonality_recategorized:cv={cv}_naics={naics}_would_skip_volatility_penalty` — shadow

- **Seasonality — penalty would be skipped.** Revenue CV is high but the merchant's NAICS prefix is on the known-seasonal list AND the CV sits inside the seasonal ceiling. Under the proposed policy the volatility penalty would be skipped.
- **Companion:** `seasonality_observed_but_volatility_extreme:cv={cv}_naics={naics}_penalty_still_applied` — seasonal industry but CV exceeds even the seasonal ceiling; penalty stays in force.
- **Source:** `src/aegis/scoring/score.py` (R4.4).

### `eof_policy_mismatch:scorer_declines_at_2_pipeline_routes_review` — shadow

- **EOF policy mismatch.** Legacy scorer hard-declines at >1 EOF marker while pipeline treats 2 EOFs as review-routing. Flag documents the divergence so the operator can flip the scorer side via config without re-deploy.
- **Source:** `src/aegis/scoring/score.py` (R4.6).

### `tib_ramp_shadow:months={N}_current_delta={X}_graduated_delta={Y}` — shadow

- **Time-in-business — graduated penalty.** Documents what a graduated TIB penalty (-15 / -8 / -5 / -2 / 0 across 3-23 months) would deduct vs. the live -15 / -8 / 0 bands.
- **Source:** `src/aegis/scoring/score.py:_tib_ramp_shadow_flag` (H8).

### `unreconciled_internal_transfer_v2` — shadow, monotonic ramp 25 / 35 / 45 / 55 / 60

- **Transfer-out with no matching transfer-in in the submitted bundle — possible hidden bank account or undisclosed MCA funding.** Distinct from the live `unreconciled_internal_transfer` Pattern in §5 above: this shadow detector loosens pair tolerance to `max($50, 0.1% × magnitude)` / ±5 days and widens scope to the entire upload bundle (the matching transfer-in may live on a different statement), whereas the live detector is ±$1 / ±3 days and single-statement-scope. The `_v2` suffix on the code disambiguates the two so both can fire in parallel during shadow validation.
- **Detects:** Transfer-OUT rows with `abs(amount) > $500` where the counterparty classifier labels the row `own_account` OR the description starts with `TRANSFER TO` / `WIRE TO` / `ACH TO` / `ZELLE TO`. A matching transfer-in (same magnitude within `max($50, 0.1% × magnitude)`, opposite sign, posted within ±5 days, anywhere in the bundle) clears the row. The proportional tolerance keeps a routine $50–100 wire fee on a $100k transfer from manufacturing a false positive. Unmatched rows fire individually so each one renders as its own drill-down.
- **Severity curve:** `min(60, 25 + (n - 1) * 10)` where `n` = unmatched count. n=1 → 25; n=2 → 35; n=3 → 45; n=4 → 55; n=5+ → 60 (cap). Monotonic non-decreasing — more evidence never reduces confidence. Replaces an earlier compound-floor design (`min(60, 40 if n >= 3 else 25 * n)`) that dropped 10 points from n=2 → n=3.
- **Shadow mode:** Emits to `PatternAnalysis.shadow_patterns` only; `FRAUD_WEIGHTS["shadow_unreconciled_internal_transfer_v2"] == 0`. Does NOT contribute to `fraud_score`, does NOT change `parse_status`, does NOT alter hard-decline reasons. The pipeline surfaces each row as `[SHADOW] unreconciled_internal_transfer_v2:...` in `all_flags`. Operator validates false-positive rate against the corpus before a future config flip to live.
- **Source:** `src/aegis/parser/patterns.py:detect_unreconciled_internal_transfers`.

### `ai_generated_statement` — shadow, composite 40..100 (emit threshold = 40)

- **Composite AI-generated-statement signal.** Fuses four orthogonal "too clean to be a real bank export" indicators into one 0..100 score: math perfection (weight 30 — zero reconciliation failures, no period-level errors, no transaction-level running-balance disagreement), description-uniformity (weight 25 — character-level Shannon entropy of all descriptions concatenated falls below 2.5 bits/char), round-number clustering (weight 25 — whole-dollar amount fraction; 0 below 20%, scaled linearly through 40%, capped at 25 above), and font uniformity (weight 20 — the document-level `FontConsistencyResult` from `forensic.font_consistency` ran successfully AND reported zero inconsistent pages).
- **Detects:** Wholly-fabricated PDFs from LLM template generators (ChatGPT-export style, "fake bank statement template" sites, reportlab/jspdf scripts). The signature is the convergence of "no math noise" + "uniform descriptions" + "rounded amounts" + "single-font rendering" — each of which can occur singly on a real statement, but the four together produce a composite that real exports almost never reach. Threshold 40 means at least two strong signals (or one strong + two moderate) must converge before emit.
- **Severity:** equal to the composite score (40..100). Source-ids is empty by design — this is a document-level judgment, not per-row.
- **Shadow mode:** Emits to `PatternAnalysis.shadow_patterns` only; `FRAUD_WEIGHTS["shadow_ai_generated_statement"] == 0`. Does NOT contribute to `fraud_score`, does NOT change `parse_status`, does NOT alter hard-decline reasons. The pipeline surfaces each emit as `[SHADOW] ai_generated_statement: score=N/100 signals=[...]` in `all_flags`. Operator validates false-positive rate against the corpus before a future config flip to live.
- **Composition vs. the existing `_ai_generated_statement_score` heuristic in `patterns.py`:** That heuristic is description-style only (uppercase fraction, digit-noise fraction, round-share). The composite here mixes those style signals with cross-layer evidence (validation failures, font-consistency analyzer). Kept separate because the composite reads from layers `patterns.py` does not have access to — moving it into `analyze_patterns()` would require plumbing `ValidationResult` and `FontConsistencyResult` down through the per-statement function signature.
- **Source:** `src/aegis/parser/forensic/ai_statement.py:detect_ai_generated_statement`.

### `duplicate_pdf_upload:sha256_match_with_doc={uuid}:uploaded={iso}[:total_prior_copies={n}]` — shadow

- **Duplicate PDF upload.** Same SHA-256 already uploaded for this merchant. The second parse re-computes aggregates against byte-identical data — the dashboard then shows 2x deposits for that period.
- **Source:** `src/aegis/merchants/cross_statement_detector.py:detect_duplicate_pdf_upload` (U12).

### `related_account_suspected:holder={name}:existing_last4={a,b}:new_last4={c}` — shadow

- **Related account suspected.** Same legal account holder appears with a new last-4 — either an undisclosed sibling account ("revenue hide") or an MCA-debit hideout ("solvency hide"). Request all bank account statements before submitting.
- **Source:** `src/aegis/merchants/cross_statement_detector.py:detect_related_account_holder` (U12).

### `apr_not_computable` — soft concern (not a flag; appears in `score_result.soft_concerns`)

- **APR could not be computed.** The IRR solver could not bracket a root for the recommended factor / holdback / term combination. The deal still scores and tiers; APR disclosure is unavailable until pricing is tightened.
- **Source:** `src/aegis/scoring/score.py` (U8).

---

## 11. Scoring-engine cutover (U30)

These flags land on `ScoreResult.shadow_flags` from `src/aegis/scoring/score.py:_check_hard_declines`. They document which scoring engine produced the result and surface the Track A / Track B verdicts from `scoring_v2` regardless of whether the active engine consumed them. Per CLAUDE.md scoring discipline: document integrity (Track A) and business risk (Track B) stay separate forever — the engine flip is the operator-validated path from the legacy blended `fraud_score` to the three-track design.

The active engine is read from `settings.aegis_scoring_engine` (env var `AEGIS_SCORING_ENGINE`); the operator flips it via `/etc/aegis/aegis.env` without a code deploy.

### `scoring_engine_active:legacy` / `scoring_engine_active:track_abc` — shadow (always emitted)

- **Records which scoring engine fired.** Every scoring pass appends this flag. `legacy` uses the existing `fraud_score >= 65` hard-decline rule (audit §A.2 fix — aligned with parser `HARD_DECLINE_THRESHOLD`); `track_abc` makes `fraud_score` informational and moves the decline path to Track A (integrity verdict) + Track B (business-risk band).
- **Operator action:** Read for context — answers "is this deal being judged by the old or new engine?" without grepping config. No action required when it matches the operator's expected posture; investigate if it doesn't (someone flipped the env var on the box).
- **Source:** `src/aegis/scoring/score.py:_check_hard_declines` (U30, audit B2 Step 2 cutover).

### `track_a_integrity_review:branch={branch}` — shadow

- **Track A — document integrity flagged for review.** Track A is a near-binary integrity gate; `review` is the soft middle verdict that doesn't auto-decline but does route the parse to manual review elsewhere in the pipeline. The branch identifies which integrity signal triggered.
- **Known branch values** (from `src/aegis/scoring_v2/track_a/compute.py`):
  - `strong_metadata` — high-confidence metadata signal (multi-EOF on unsigned, page-size inconsistency, etc.)
  - `drift_plus_editor` — reconciliation drift combined with a known PDF editor in the metadata
  - `medium_corroborated` — medium-strength signal corroborated by a second independent indicator
  - `drift_alone` — reconciliation drift without a corroborating signal
  - `clean` — emitted as the soft verdict when nothing fired (informational baseline)
- **Operator action:** Read the branch label — it tells you which Track A pathway fired. Reconciliation drift alone is softer than strong_metadata. Apply the existing tampering-flag review discipline (request the original from the bank portal, etc.).
- **Source:** `src/aegis/scoring/score.py:_check_hard_declines` (U30); branch labels from `src/aegis/scoring_v2/track_a/compute.py`.

### `track_b_elevated_risk` — shadow (no detail body)

- **Track B placed the deal in the 'elevated' business-risk band.** Measurably weaker than the 'standard' baseline but below the auto-decline 'high' band.
- **Operator action:** Underwriter call — consider tighter pricing or stricter stipulations before submission rather than treating it as a clean deal. Not a decline reason; not a kill.
- **Source:** `src/aegis/scoring/score.py:_check_hard_declines` (U30); band values from `src/aegis/scoring_v2/track_b`.

### `track_a_integrity_fail:branch={branch}` — shadow chip, **decline** severity

- **Track A's near-binary integrity gate failed.** Under the `track_abc` engine this is a hard-decline reason and the same code appears in `ScoreResult.hard_decline_reasons`. Under the `legacy` engine it appears in `shadow_flags` only — informational until the cutover flip.
- **Operator action:** Statement should not be submitted to funders. Branch label tells you which integrity pathway tripped (see the `track_a_integrity_review` branch list above).
- **Source:** `src/aegis/scoring/score.py:_check_hard_declines` (U30).

### `track_b_high_risk` — shadow chip, **decline** severity (no detail body)

- **Track B placed the deal in the 'high' business-risk band.** Under the `track_abc` engine this is a hard-decline reason (matches `BAND_TO_ACTION`'s `review_decline_default`). Under the `legacy` engine it is informational until cutover.
- **Operator action:** Business cannot reasonably support repayment given the merged cashflow + concentration + history signals. Decline path; do not submit.
- **Source:** `src/aegis/scoring/score.py:_check_hard_declines` (U30).

---

## 12. Tier-aware funder matching (U28)

`tier_matches` is **structured data, not a flag.** It lives on `FunderMatch.tier_matches: list[TierMatch]` (see `src/aegis/scoring/models.py`), populated by `scoring/match_funders.py:evaluate_tier_matches` for funders whose `funders.tiers` JSONB is populated (operator-curated underwriting matrices like Logic Advance's Elite/Premium/Standard/High-Risk or UCS's seven product lines).

Per CLAUDE.md "Decision-boundary changes — shadow-first": tier-match results are annotation-only — they do NOT change `FunderMatch.match_score`, `soft_concerns`, or `reasons`. The operator validates per-tier results against the corpus, then a future code change promotes tier-aware matching to drive live decisions.

**Rendering lives in the template, not the flag humanizer.** Per-tier data (qualifies / disqualifying_reasons / per-tier factor range / holdback / advance) is structured enough that a one-line summary in `_flag_labels.py` would discard the economics that are the point of U28. The match panel's existing component patterns (per-row chip, criteria table, estimated-terms grid) are the right surface — tier matches naturally fit as a nested expandable list under each funder card in `src/aegis/web/templates/merchant_match.html.j2`.

Fields on each `TierMatch`:

- `tier_name` — verbatim from `FunderTier.name` (e.g. `"Elite"`, `"MCA"`)
- `qualifies` — True when the merchant satisfies every constraint this tier publishes
- `disqualifying_reasons` — per-axis failure codes (e.g. `"credit 620 < min 700"`, `"tib 8mo < min 12mo"`). Same shape as `FunderMatch.soft_concerns` so the UI can reuse the same component.
- `estimated_factor_low` / `estimated_factor_high` — tier's `buy_rate_low`/`buy_rate_high`
- `estimated_holdback` — tier's `max_holdback` as a fraction
- `estimated_advance` — `ScoreResult.suggested_max_advance` clamped to the tier's `max_advance`

Missing merchant data on a tier-constrained axis surfaces as an explicit `*_unknown` disqualifying reason and conservatively fails the tier.

---

## Action ladder [PENDING REVIEW]

This is the suggested order for reviewing a deal. *Order is operator synthesis* — needs pressure-testing against the operator's real workflow before treated as authoritative.

1. **Metadata flags first.** Any single hard-list editor / `personal_author` / `page_size_inconsistency` / `xref_offset_mismatch` / `incremental_saves` → reject before reading further. The transactions could be fabricated. The detector is signature-aware as of 2026-05-28, so `incremental_saves` no longer false-positives on legitimately-signed bank exports — when it fires, it's a real tampering signal.
2. **Then `acceleration_clause_triggered` and `unauthorized_withdrawal_dispute`.** Either alone is near-decline territory.
3. **Then `mca_stacking` count and `processor_holdback_detected`.** Tells you how many funders are in the deal and whether there's a hidden holdback.
4. **Then the revenue-fabrication trio** (`wash_deposit_suspected`, `duplicate_deposits_detected`, `synthetic_low_variance`). Any two firing together = decline-leaning.
5. **Soft signals and concentration** inform grade, not decline.

---

## Appendix: EOF marker detector — history and current behavior

**Status: signature-aware detector shipped 2026-05-28.**

This section records the false-positive class that was eliminated and the v1 → v2 path for the EOF detector. The flag's user-facing semantics live in section 7 above; this appendix is here so future operators and engineers can read why the detector looks the way it does today.

**The original problem (pre-fix):**

Live Review Queue surfaced `incremental_saves` flags on documents that looked like real deals (Know Your Collectibles, per operator observation 2026-05-28). The detector treated any `eof_markers > 1` as +40 score, which trains operators to ignore a reject-class flag.

**The technical reality of EOF markers in PDFs:**

- PDF digital signatures are implemented as *incremental updates*. Adding a signature appends a new `%%EOF` trailer rather than rewriting the file. Every digitally signed PDF therefore has ≥2 EOFs by design.
- Major bank statement exports (Chase, Bank of America, Wells Fargo, JPMC) are commonly digitally signed.
- Many KYC and corporate-records documents (state filings, certified business records) are digitally signed by the issuing authority.
- Re-saving in some PDF viewers (older Preview, certain Acrobat versions) creates incremental updates even without intentional modification.

**The v1 fix (shipped):**

`src/aegis/parser/metadata.py` now calls `_has_pdf_signature(pdf)` before emitting the flag. The helper walks the PDF's `AcroForm.Fields` (with `/Kids` descent) looking for a `/FT == /Sig` field. The flag now only fires when `eof_markers > 1` AND no signature object is present:

```python
eof_markers = _count_eof_markers(raw)
if eof_markers > 1 and not _has_pdf_signature(pdf):
    flags.append(f"incremental_saves: {eof_markers} EOF markers")
    score += 40
```

**v1 scope and tradeoffs:**

- Detection is *presence-based*, not cryptographic. We check for the existence of a `/Sig` object; we do not verify the signature's cryptographic validity.
- The realistic threat model favors this tradeoff: real signed bank exports stop being false-positively flagged, real tampering on unsigned PDFs still fires the flag at the original severity.
- A malicious actor could forge a `/Sig` object to bypass detection. This is the trade we make for v1 — without it, every signed bank export would continue to dominate the manual_review queue.

**v2 (deferred):**

Cryptographic signature validation. Would require adding a new dependency (pyhanko or equivalent) and a small amount of validation code. When v2 lands, the right shape is:

1. Signed + signature valid → no flag.
2. Signed + signature invalid → flag at *higher* severity than the current `incremental_saves` (genuine tampering attempt; the actor signed it and then modified it).
3. Unsigned + multi-EOF → flag at current severity (unchanged from v1).

---

## 11. WARN-prefixed flags (operator caveats, never decline)

These detectors emit one entry into `PipelineResult.all_flags` with the `[WARN]` prefix. They are surface-only — `parse_status`, `fraud_score`, `FRAUD_WEIGHTS`, and Track A / Track B verdicts are unchanged. They exist so the operator + downstream UI (funder-match grid soft concerns, dossier flag list) see funder-dependent context that AEGIS does not have authority to act on unilaterally.

### `fintech_bank_detected: <Name> — many funders decline fintech bank accounts` — WARN

- **Detects:** Extracted `bank_name` matches a known fintech / neobank — Mercury, Brex, Bluevine, Novo, Relay, Lili, Found, Rho, Arc, Nearside, Oxygen, NorthOne. Case-insensitive substring match; the static list lives in `src/aegis/parser/fintech_banks.py:FINTECH_BANK_IDENTIFIERS`.
- **Why it matters:** Funder appetite for fintech bank accounts varies. Some funders decline them outright because fintech banks lack the traditional ACH-debit controls / daily-remit guarantees that traditional banks provide; others accept them. The decision is per-funder, not per-deal — so AEGIS does not decline.
- **Surface:** The funder-match grid attaches "Merchant banks with `<Name>`. Verify funder accepts fintech bank accounts before submitting." as a soft concern on every match card. Cards that are otherwise green become amber when the warning lands; cards that are otherwise red still hard-fail on the underwriting reason, with the bank caveat alongside.
- **Adding entries:** New fintech banks land in `FINTECH_BANK_IDENTIFIERS` when the operator confirms an upstream merchant banks with them. Substring matching automatically captures sub-brands ("Mercury Treasury", "Brex Cash") without an explicit list entry.
- **Source:** `src/aegis/parser/pipeline.py` (`detect_fintech_bank` call, appended after the tampering-persistence flag), `src/aegis/parser/fintech_banks.py`.

---

**Last updated:** 2026-05-28
**Maintainer:** Filip (with the AEGIS engineering process — re-verify against `patterns.py`, `metadata.py`, `aggregate.py` when those files change)
