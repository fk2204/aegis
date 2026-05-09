# California — AEGIS Compliance Research Dossier

**Researched: 2026-05-07** by Claude (web-search based) for operator verification.

This document is **not** ready to paste into AEGIS until the operator has clicked through the source URLs and confirmed the citations are real. The verification step is non-negotiable. Without it, this document is just LLM output and cannot ground a regulator-defensible compliance posture.

---

## TL;DR for AEGIS

- **California is Tier 1.** It has an enacted commercial financing disclosure statute that directly applies to MCAs (sales-based financing).
- **The relevant rule for AEGIS is `10 CCR § 914`** — Sales-Based Financing Disclosure Formatting and Contents. This is the prescribed form. AEGIS must produce a disclosure with the exact rows, columns, labels, and language specified there.
- **The APR is calculated under `10 CCR § 940` and `10 CCR § 942`.** This is consistent with the actuarial method that AEGIS already uses (`scipy.optimize.brentq` on the actuarial NPV equation).
- **There are TWO laws to comply with:** SB 1235 (effective Dec 9, 2022) is the disclosure framework. SB 362 (effective Jan 1, 2026) added new APR re-disclosure rules at every pricing communication.
- **Threshold:** Disclosure obligations apply when the financing offer is **$500,000 or less** AND the recipient's business is principally directed or managed from California.
- **Confidence-of-Judgment (CoJ):** Not specifically prohibited by SB 1235/SB 362, but California has separate restrictions on CoJs in the Code of Civil Procedure that I did not research in depth. **Flag this for follow-up before funding any CA deal.**

---

## Statute identification (Tier 1 entry for `compliance/states.py`)

```
state: California
abbreviation: CA
tier: 1
bill_number: SB 1235 (2018, Glazer)
effective_date_statute: 2018-09-30 (signed by Gov. Brown; codified as Cal. Fin. Code § 22800-22805)
effective_date_regulations: 2022-12-09 (DFPI Title 10 CCR § 900-956)
amendment_bill: SB 362 (2025, Grayson)
amendment_effective_date: 2026-01-01
disclosure_required: true
apr_calculation_method: actuarial_reg_z (per 10 CCR § 940)
threshold_amount_usd: 500000
threshold_test: financing_offer_amount <= 500000 AND recipient_principally_directed_or_managed_from_california
prescribed_form_section: 10 CCR § 914 (sales-based financing — applies to MCAs)
coj_allowed: NEEDS_FURTHER_RESEARCH
verified_date: <operator fills in after verifying>
```

---

## Source URLs (operator: click through these and confirm the citations are real)

1. **SB 1235 bill text (California Legislature)** — https://leginfo.legislature.ca.gov/faces/billTextClient.xhtml?bill_id=201720180SB1235
2. **DFPI's commercial financing disclosure landing page** — https://dfpi.ca.gov/regulated-industries/california-financing-law/about-california-financing-law/california-financing-law-commercial-financing-disclosures/
3. **Title 10 CCR Subchapter 3 index** — https://regulations.justia.com/states/california/title-10/chapter-3/subchapter-3/
4. **10 CCR § 914 (sales-based financing form, the one AEGIS must produce)** — https://www.law.cornell.edu/regulations/california/10-CCR-914
5. **10 CCR § 940 (APR calculation method)** — https://www.law.cornell.edu/regulations/california/10-CCR-940
6. **10 CCR § 942 (estimated APR for sales-based financing)** — https://regulations.justia.com/states/california/title-10/chapter-3/subchapter-3/section-942
7. **DFPI final regulations PDF (full 48-page text)** — https://dfpi.ca.gov/wp-content/uploads/sites/337/2022/06/PRO-01-18-Commercial-Financing-Disclosure-Regulation-Final-Text.pdf
8. **SB 362 bill text (2025 amendment, effective 2026-01-01)** — https://leginfo.legislature.ca.gov/faces/billTextClient.xhtml?bill_id=202520260SB362
9. **SB 362 status page** — https://leginfo.legislature.ca.gov/faces/billStatusClient.xhtml?bill_id=202520260SB362

**Verification protocol:** Open at least #1, #4, and #8 in your browser. Read the first paragraph of each. Confirm the bill number, the effective date, and that the substance matches what's summarized in this dossier. If any of those three don't match, stop and tell me — do not paste this into AEGIS.

---

## What 10 CCR § 914 actually requires (the prescribed form)

The disclosure for an MCA in California must be a **table with 9 rows and 3 columns** structured exactly as below. AEGIS's Jinja template for California must match this row-by-row.

### Row 1 — Funding Provided
- Col 1: literal text `"Funding Provided"`
- Col 2: amount financed (the gross advance, dollars)
- Col 3: in one paragraph and in this order:
  - `"This is how much funding [name of financer] will provide."`
  - If amount financed > recipient funds: `"Due to deductions or payments to others, the total funds that will be provided to you directly is [recipient funds]. For more information on what amounts will be deducted, please review the attached document 'Itemization of Amount Financed.'"`
  - If part of the financing pays down other obligations whose amounts may change: short explanation that the direct-to-recipient amount may change
  - If 3rd-party payoffs are required and amounts not known to provider: short explanation that the direct-to-recipient amount may change

### Row 2 — Estimated APR
- Col 1: `"Estimated Annual Percentage Rate (APR)"`
- Col 2: APR calculated per 10 CCR § 940
- Col 3: literal language:
  > "APR is the estimated cost of your financing expressed as a yearly rate. APR incorporates the amount and timing of the funding you receive, fees you pay, and the periodic payments you make. This calculation assumes your estimated average monthly income through [description of particular payment channel or mechanism] will be [average monthly income estimate determined in accordance with sections 930 or 931]. Since your actual income may vary from our estimate, your effective APR may also vary."
- Plus, if the finance charge is not based on an interest rate: append `"APR is not an interest rate. The cost of this financing is based upon fees charged by [financer] rather than interest that accrues over time."` (For typical MCAs, this clause applies — MCAs use a factor rate, not interest.)

### Row 3 — Finance Charge
- Col 1: `"Finance Charge"`
- Col 2: finance charge calculated per 10 CCR § 943
- Col 3: `"This is the dollar cost of your financing."`
  - Plus, if finance charge cannot increase under any circumstance: optionally append `"Your finance charge will not increase if you take longer to pay off what you owe."` (For most MCAs this is true — the total payback is fixed at funding.)

### Row 4 — Estimated Total Payment Amount
- Col 1: `"Estimated Total Payment Amount"`
- Col 2: total dollar amount of estimated payments over contract term
- Col 3: `"This is the total dollar amount of payments we estimate you will make under the contract."`

### Row 5 — Estimated Payment
- Col 1: `"Estimated Payment"`
- Cols 2+3 combined: average periodic payment (per § 942) followed by `/` and the frequency (e.g. `$487/business day`); plus dates+amounts of any irregular payments and reasonably anticipated true-ups

### Row 6 — Payment Terms
- Col 1: `"Payment Terms"`
- Cols 2+3 combined:
  - If daily payments: short explanation of when (weekdays only? every calendar day?)
  - If using a split rate: explanation like `"Each business day, your credit card processor will remit 15% of your gross receipts to us, and send any remaining amounts to you. This financing does not have a fixed payment schedule and there is no minimum payment amount."`
  - If true-up mechanism: explanation of how the preset payment was calculated AND how the true-up works, with cross-reference to the contract paragraph
  - If minimum payment terms: short explanation

### Row 7 — Estimated Term
- Col 1: `"Estimated Term"`
- Col 2: estimated term per § 942
- Col 3: explanation that term is based on income assumptions (e.g. `"This is our estimate of how long it will take to collect amounts due to us under the contract based upon the assumption that you will receive $6,000 in monthly income through your BrownPay account."`)

### Rows 8 & 9 — Prepayment (combined label in column 1)
- Col 1 (rows 8 and 9 combined): `"Prepayment"`
- Row 8 cols 2+3 combined:
  - If prepayment requires non-interest finance charges: `"If you pay off the financing faster than required, you still must pay all or a portion of the finance charge, up to $[maximum non-interest finance charge] based upon our estimates."`
  - Otherwise: `"If you pay off the financing faster than required, you will not be required to pay any portion of the finance charge other than unpaid interest accrued."`
- Row 9 cols 2+3 combined:
  - If prepayment requires additional fees: `"If you pay off the financing faster than required, you must pay additional fees of [amount and description of fees]."`
  - Otherwise: `"If you pay off the financing faster than required, you will not be required to pay additional fees."`

### Optional row (insert below Row 4) — Estimated Monthly Cost
- Required only if the contract provides for non-monthly periodic payments (i.e. virtually all MCAs, since they're daily or weekly)
- Col 1: `"Estimated Monthly Cost"`
- Col 2: estimated monthly cost per § 942 (or a list of costs for time periods if it varies)
- Col 3: short explanation, e.g. `"Although you do not make payments on a monthly basis, this is our calculation of your average monthly cost based upon the payment amounts disclosed below."`

### Footer (per § 901)
At the bottom of the disclosure, below all rows: `"California Applicable law requires this information to be provided to you to help you make an informed decision."`

### Term unit rule (per § 901)
- If term ≤ 1 year: express in days
- If term > 1 year: express in years and months (with remaining days as a fraction of a month)

### Signature requirement (per § 920)
The recipient must sign the disclosure before consummation of the financing transaction. Electronic signatures are acceptable.

---

## SB 362 — what changed effective 2026-01-01

This is the new layer on top of SB 1235. AEGIS workflow needs to handle these:

1. **Re-disclosure rule.** After a specific offer is extended, *every time* the provider communicates a charge, pricing metric, or financing amount to the recipient (in email, in the portal, in a sales conversation), the provider must also state the APR using the term `"annual percentage rate"` or `"APR"`. Practically: APR can no longer just appear on the closing disclosure — it must appear in every quote email and every term-sheet revision.

2. **No deceptive use of "interest" or "rate".** The provider may not use those terms in a way that could mislead the recipient.

3. **APR safe harbor preserved.** SB 362 also amends Section 22806 to confirm that providers are not liable when the actual APR diverges from the estimated APR, as long as the estimate was disclosed in conformity with DFPI regulations.

4. **Enforcement layer.** Violations of SB 362 / SB 1235 are now enforced under the California Financing Law (CFL) for licensees, and under the California Consumer Financial Protection Law (CCFPL) for non-licensees. Penalties include restitution, fines, and cease-and-desist authority for DFPI.

**For AEGIS:** the dashboard's match-preview, submission-package emails, and any auto-generated quote should include the APR alongside any factor rate, daily payment, or financing amount they reference. This is an architectural rule, not just a template change.

---

## What's still unknown / NEEDS FOLLOW-UP

1. **CoJ (Confession of Judgment) rules.** I did not find a definitive answer about whether SB 1235/SB 362 prohibit or restrict CoJs in commercial financing. California has separate restrictions in the Code of Civil Procedure on CoJs generally. **Before funding a CA deal that includes a CoJ, get a 30-minute attorney consultation.** Default `coj_allowed=false` in AEGIS until verified.
2. **Broker compensation disclosure.** SB 1235 mentions broker compensation must be disclosed where applicable. Detailed format requirements likely live in the regulations (possibly § 952 "Duties of Financers and Brokers"). Worth a focused read before AEGIS auto-generates broker-comp lines.
3. **DFPI does not publish a sample disclosure form.** Multiple law firm summaries note this explicitly. AEGIS must build the table from § 914 directly. There is no PDF to mimic — the regulation IS the form.
4. **APR tolerance and error-cure rules** (§ 955). If AEGIS's calculated APR diverges from the actual APR within DFPI tolerances, no liability attaches. There's also a 60-day error-cure safe harbor. Worth implementing as a tolerance window in the APR comparison logic.

---

## Recommendation to operator

1. **Verify the three URLs** above (#1 SB 1235 bill, #4 § 914 regulation, #8 SB 362 bill). 10 minutes.
2. **Spend 30 minutes** with an MCA-specialist attorney just to confirm CoJ posture for California. Cheaper than a regulator letter.
3. **Then paste this dossier into Claude Code** with the prompt I'll write below, and Claude Code will build the Tier 1 entry, the `ca_sb1235.html.j2` template (§ 914 row-by-row), the snapshot test, and the SB 362 re-disclosure hook in the dashboard.

If verification step 1 reveals any discrepancy, stop and tell me what you found. Don't paste anything into Claude Code until verification is clean.

---

## Confidence note (Claude's self-assessment)

- **High confidence:** SB 1235 is real, signed Sep 30 2018, codified at Cal. Fin. Code § 22800-22805.
- **High confidence:** Implementing regulations effective Dec 9 2022 at Title 10 CCR § 900-956.
- **High confidence:** § 914 is the rule for sales-based financing (MCAs) and has the structure described above. I have the verbatim text from the Cornell LII mirror.
- **High confidence:** SB 362 was enacted in 2025, effective Jan 1 2026, and adds re-disclosure + deceptive-term rules.
- **Medium confidence:** CoJ rules in CA. I did not deep-research this; flagged for follow-up.
- **Medium confidence:** Whether SB 362 has its own implementing regulations not yet adopted by DFPI (none found in this research; assume not yet).
- **Lower confidence:** The exact mechanics of broker-compensation disclosures under § 952. Did not deep-read.

Items at medium-or-lower confidence should NOT be copied into AEGIS as final values without separate verification.
