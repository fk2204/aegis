# New York — Complete AEGIS Compliance Dossier

**Researched: 2026-05-07** by Claude (web-search based) for operator verification.
**Status when verified: ready to paste into AEGIS.**

---

## TL;DR for AEGIS

- **Tier 1.** New York's Commercial Finance Disclosure Law (CFDL) is the second-strictest disclosure regime in the US, modeled on TILA/Reg Z and enforced by NY DFS.
- **Statute:** N.Y. Financial Services Law §§ 801-811 (Article 8). Enacted as SB 5470 (2020), substantially amended by S898 (2021).
- **Regulations:** 23 NYCRR Part 600. Adopted Feb 1, 2023, mandatory compliance Aug 1, 2023.
- **Prescribed form for MCAs:** 23 NYCRR § 600.6 — Sales-based financing disclosure. **10 rows × 3 columns** (one more row than CA — adds a Collateral Requirements row).
- **APR via 23 NYCRR § 600.3** — TILA/Reg Z actuarial method, with 1/8 % or 1/4 % tolerance.
- **Threshold:** $2,500,000 or less (much higher than CA's $500K).
- **CoJ permitted but only against NY residents.** Out-of-state debtors cannot be subject to NY-filed CoJs since CPLR § 3218 was amended August 30, 2019. Pending bill (S2305, 2025) would further restrict CoJs to debts ≥ $5M.
- **Broker compensation disclosure required.** § 600.21(f) requires the provider to inform the recipient in writing how, and by whom, the broker is compensated, when a broker is involved.

---

## Statute identification

```
state: New York
abbreviation: NY
tier: 1
bill_number: SB 5470 (2020), substantially amended by S898 (2021)
statute_citation: N.Y. Fin. Services Law §§ 801-811 (Article 8)
common_name: Commercial Finance Disclosure Law (CFDL)
regulation_citation: 23 NYCRR Part 600
regulations_adopted: 2023-02-01
mandatory_compliance_date: 2023-08-01
prescribed_form_section: 23 NYCRR § 600.6 (sales-based financing - applies to MCAs)
apr_calculation_method: actuarial_reg_z (23 NYCRR § 600.3)
apr_tolerance: 0.125% or 0.25% above/below per TILA Reg Z standards
threshold_amount_usd: 2500000
threshold_test: financing_offer_amount <= 2500000 AND provider_makes_specific_offer
disclosure_required: true
coj_allowed: conditional  # only against NY-resident debtors
coj_citation: N.Y. CPLR § 3218 (amended by chapter 311 of 2019, effective 2019-08-30)
broker_compensation_disclosure_required: true
broker_disclosure_section: 23 NYCRR § 600.21(f)
```

---

## Source URLs (verify these — 12 minutes)

1. **NY Financial Services Law Art. 8 (statute)** — https://www.dfs.ny.gov/industry_guidance/regulations/final_financial_services/rf_finservices_23nycrr600_text
2. **23 NYCRR Part 600 (regulations) on DFS site** — https://www.dfs.ny.gov/industry_guidance/regulations/final_financial_services/rf_finservices_23nycrr600_text
3. **23 NYCRR § 600.6 sales-based financing form** — https://www.law.cornell.edu/regulations/new-york/23-NYCRR-600.6
4. **23 NYCRR § 600.0 introduction/scope** — https://www.law.cornell.edu/regulations/new-york/23-NYCRR-600.0
5. **NYDFS press release announcing adoption** — https://www.dfs.ny.gov/reports_and_publications/press_releases/pr202302011
6. **CPLR § 3218 (CoJ statute, current text)** — https://law.justia.com/codes/new-york/cvp/article-32/3218/
7. **2019 amendment summary (Riker Danzig)** — https://riker.com/blog/banking-title-insurance-and-real-estate-litigation/new-york-amends-confession-of-judgment-statute-to-prohibit-confessions-filed-against-out-of-state-debtors/
8. **S2305 (2025 pending bill, would extend CoJ ban to MCAs <$5M)** — https://www.nysenate.gov/legislation/bills/2025/S2305

---

## What 23 NYCRR § 600.6 requires for MCA disclosures

Disclosure is a table with **10 rows and 3 columns** — one more row than California (NY adds a Collateral Requirements row). Structure below.

**Row 1 — Funding Provided.** (Same as CA but with an additional NY-specific clause.)
- Col 1: `"Funding Provided"`
- Col 2: amount financed
- Col 3, in this order:
  - `"This is how much funding [name of financer] will provide."`
  - If amount financed > recipient funds: deductions explanation + reference to "Itemization of Amount Financed"
  - If part pays down other obligations whose amounts may change: short explanation
  - If 3rd-party payoffs required and amounts not known: short explanation
  - **NY-specific double-dipping disclosure** (subdivision (b)(3)(v)): if any portion of amount financed is used to satisfy obligations under another financing with the same provider, in a second paragraph: `"Does the renewal financing include any amount that is used to pay unpaid finance charges or fees, also known as double dipping? {Yes, enter amount}. If the amount is zero, the answer would be No."`

**Row 2 — Estimated APR.**
- Col 1: `"Estimated Annual Percentage Rate (APR)"`
- Col 2: APR per § 600.3
- Col 3 literal: `"APR is the estimated cost of your financing expressed as a yearly rate. APR incorporates the amount and timing of the funding you receive, finance charges you pay, and the periodic payments you make. This calculation assumes your estimated average monthly income through [description] will be [estimate]. Since your actual income may vary from our estimate, your effective APR may also vary."`
- Plus, if no part of finance charge is interest-rate-based: `"APR is not an interest rate. The cost of this financing is based upon fees charged by [financer] rather than interest that accrues over time."`

**Row 3 — Finance Charge.**
- Col 1: `"Finance Charge"`
- Col 2: finance charge per § 600.2
- Col 3: `"This is the dollar cost of your financing."`
- Plus optional: `"Your finance charge will not increase if you take longer to pay off what you owe."`

**Row 4 — Estimated Total Payment Amount.**
- Same as CA row 4.

**Row 5 — Estimated Payment.**
- Cols 2+3 combined: average periodic payment per § 600.7 + frequency, dates+amounts of irregular payments and reasonably anticipated true-ups, and if necessary a short explanation of why estimated payments may differ from actual obligations.

**Row 6 — Payment Terms.**
- Same content rules as CA row 6.

**Row 7 — Estimated Term.**
- Same as CA row 7.

**Rows 8 & 9 — Prepayment.** (Col 1 of rows 8 and 9 combined: `"Prepayment"`)
- Same prepayment finance-charge and additional-fee logic as CA.

**Row 10 — Collateral Requirements** (this is the NY-specific additional row).
- Col 1: `"Collateral Requirements"`
- Cols 2+3 combined: description of any collateral requirements or security interests, or `"None"` if none.

**Optional row inserted below Row 4 if non-monthly payments (i.e. all MCAs):**
- Col 1: `"Estimated Monthly Cost"`
- Col 2: estimated monthly cost per § 600.7 (or list by period if varies)
- Col 3: short explanation of derivation

**APR re-disclosure rule (§ 600.1 / § 600.3):** During the application process AND after the specific offer is quoted, whenever the provider states a rate, finance charge, or financing amount, the provider must also state the APR. (This is the equivalent of CA's SB 362 rule but baked into NY's regulations from the start.)

**APR tolerance (§ 600.4 — "Allowed Tolerances"):** Verbatim regulatory text:
> An annual percentage rate disclosed pursuant to section 600.3 of this Part shall be considered accurate if:
> (1) it is not more than one-eighth of one percentage point above or below the annual percentage rate determined in accordance with section 600.3(b); or
> (2) in an irregular transaction, it is not more than one-fourth of one percentage point above or below the annual percentage rate determined in accordance with section 600.3(b).

In plain terms: 1/8% (0.125%) tolerance for regular transactions, 1/4% (0.25%) for irregular. Over-disclosure of finance charge is permitted. **60-day bona fide error cure** (§ 600.22): provider not liable if error discovered, recipient notified, and adjustment made within 60 days.

**NY Penalties:** $2,000 per violation; $10,000 per intentional violation. DFS may seek injunctions for knowing violations.

---

## Broker compensation: REQUIRED disclosure in NY (§ 600.21(f))

This is a critical difference from California. When a broker is involved, the **provider must inform the recipient in writing how, and by whom, the broker is compensated** for their role in the transaction.

Important nuances:
- Broker compensation is NOT included in the standardized disclosure table (rows 1-10).
- The disclosure of broker compensation is a **separate written communication** from the provider to the recipient.
- The regulation does NOT specify a form. NYDFS in its Assessment of Public Comments said: "This general provision does not specify a form of disclosure. Financers have the discretion to explain broking fees, in writing, so long as their disclosures are accurate."
- Brokerage fees treated as **prepaid finance charges** must additionally be reflected in the finance charge calculation per § 600.17.

**For AEGIS:** when matching a CA-or-NY merchant to a funder, the funder must be made aware that AEGIS is acting as a broker AND the funder must include in its disclosure package to the merchant a statement of how AEGIS is compensated. AEGIS should provide the funder with a standard text block stating its compensation structure, which the funder includes when transmitting the disclosure. This is operational rather than data-model.

### Other broker duties under § 600.21
- Pre-offer disclosure transmission duties parallel California § 952 (unaltered forwarding, evidence of transmission to financer).
- Pattern-of-noncompliance enforcement parallel.
- 4-year retention (§ 600.21(d)).

---

## CoJ status: PERMITTED but heavily restricted

Unlike CA and FL (which fully ban CoJs), NY permits CoJs against debtors who **resided in New York at the time the affidavit was executed**. Out-of-state debtors are protected.

### Current statutory framework (CPLR § 3218 as amended by chapter 311 of 2019, effective 2019-08-30)

- The confession affidavit must state the NY county where defendant resided when executed.
- The confession may only be filed in that NY county OR in the NY county where defendant resided at time of filing.
- Non-natural persons (LLCs, corps): "reside" in any county where they have a place of business.
- Confession is unenforceable if executed by a party with no NY residency or NY place of business at time of execution.
- Three-year filing window after execution.

### Pending legislation: S2305 (2025)
- Would prohibit CoJs entirely on amounts due from individuals for personal/family/household/consumer/investment/non-business purposes.
- Would prohibit CoJs on debts where principal amount < $5,000,000.
- This would effectively eliminate CoJs from typical MCA transactions (most MCAs are well under $5M).
- **Status: introduced January 2025, NOT yet enacted as of research date.** Track at link #8.

### Operational rules for AEGIS

- **CoJs allowed only when merchant principal place of business is New York.** Match log records `funder_requires_coj_blocked_by_state` for non-NY merchants where funder requires CoJ.
- **For NY-resident merchants, CoJ is permissible** today but treat as risk-flagged in match preview (recipient can challenge under various procedural grounds).
- **Track S2305.** If enacted, AEGIS should default `coj_allowed=False` for all NY MCA deals < $5M.

---

## Updated Tier 1 entry for `compliance/states.py`

```python
StateRegulation(
    state="New York",
    abbreviation="NY",
    tier=1,
    bill_number="SB 5470 (2020), amended by S898 (2021)",
    common_name="Commercial Finance Disclosure Law (CFDL)",
    statute_citation="N.Y. Fin. Services Law §§ 801-811",
    regulation_citation="23 NYCRR Part 600",
    regulations_adopted=date(2023, 2, 1),
    mandatory_compliance_date=date(2023, 8, 1),
    citation_url_statute="https://www.dfs.ny.gov/industry_guidance/regulations/final_financial_services/rf_finservices_23nycrr600_text",
    citation_url_regulation="https://www.law.cornell.edu/regulations/new-york/23-NYCRR-600.6",
    prescribed_form_section="23 NYCRR § 600.6",
    apr_calculation_method="actuarial_reg_z",
    apr_tolerance_percent=Decimal("0.125"),  # or 0.25% for irregular
    threshold_amount_usd=Decimal("2500000"),
    disclosure_required=True,
    coj_allowed="conditional_ny_resident_only",
    coj_citation="N.Y. CPLR § 3218 (amended chapter 311 of 2019)",
    coj_citation_url="https://law.justia.com/codes/new-york/cvp/article-32/3218/",
    coj_effective_date=date(2019, 8, 30),
    requires_unaltered_disclosure_transmission=True,
    transmission_record_retention_years=4,
    broker_compensation_disclosure_required=True,
    broker_disclosure_section="23 NYCRR § 600.21(f)",
    apr_re_disclosure_required=True,  # whenever rate/charge/amount stated
    notes=(
        "MCAs are 'sales-based financing' under § 803 / 23 NYCRR § 600.6. "
        "10-row disclosure (one more than CA - includes Collateral Requirements). "
        "Includes anti-double-dipping disclosure for renewal financing. "
        "APR re-disclosure required at every pricing communication (built into reg). "
        "CoJs allowed against NY-resident merchants only since 2019-08-30. "
        "Pending bill S2305 would ban CoJs on debts < $5M - track for status. "
        "Broker compensation disclosure REQUIRED - separate written notice from provider."
    ),
    pending_amendments=[
        PendingAmendment(
            bill_number="S2305",
            year=2025,
            status="introduced",
            summary="Would prohibit CoJs on debts < $5M and on consumer/non-business debts.",
            citation_url="https://www.nysenate.gov/legislation/bills/2025/S2305",
        ),
    ],
    verified_date=None,
)
```

---

## Confidence assessment

| Finding | Confidence |
|---|---|
| CFDL statute and effective dates | High — DFS press release + NYS Senate bill records |
| 23 NYCRR § 600.6 form structure (10 rows × 3 cols) | High — verbatim regulatory text retrieved |
| Threshold $2.5M | High — multiple law firm summaries + reg text |
| CoJ permitted only against NY residents since 2019-08-30 | High — statutory text + multiple law firm summaries |
| Broker compensation written disclosure required (§ 600.21(f)) | High — reg text + NYDFS Assessment of Public Comments cited |
| S2305 status (pending, not enacted) | High — current NYS Senate bill records |
| APR tolerance 1/8% or 1/4% (§ 600.4) | High — verbatim regulatory text retrieved during verification pass |
| NY penalties ($2,000 / $10,000 intentional) | High — verbatim regulatory text |
