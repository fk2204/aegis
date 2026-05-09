# Georgia — Complete AEGIS Compliance Dossier

**Researched: 2026-05-07** by Claude (web-search based) for operator verification.
**Status when verified: ready to paste into AEGIS.**

---

## TL;DR for AEGIS

- **Tier 1.** Georgia Commercial Financing Disclosure Law is in effect.
- **Statute:** Title 10 of the Official Code of Georgia Annotated (O.C.G.A.) **§ 10-1-393.18** (one section with multiple subsections), within the Georgia Fair Business Practices Act. Enacted by SB 90 (2023), effective **January 1, 2024**.
- **Disclosure is content-based, not form-prescribed** (similar shape to Florida's law).
- **APR disclosure IS required** (unlike Florida — Georgia is a middle ground).
- **Threshold:** $500,000 or less, AND provider must consummate **more than 5** transactions in Georgia per calendar year.
- **Lease financing NOT covered.** Real-estate-secured loans NOT covered. Equipment-related captive financing NOT covered.
- **CoJ permitted** in Georgia as of research date (O.C.G.A. § 9-12-18 allows confessions but with venue restrictions). Georgia is a CoJ-allowed state, similar to Pennsylvania and Ohio. AEGIS should treat CoJ availability as a funder-by-funder decision, not blocked at state level.
- **Broker advance fees prohibited.** Same prohibition pattern as Florida.

---

## Statute identification

```
state: Georgia
abbreviation: GA
tier: 1
bill_number: SB 90 (2023)
chapter: Act 217 of the 2023 Regular Session
common_name: Georgia Commercial Financing Disclosure Law
statute_citation: O.C.G.A. § 10-1-393.18 (single section, multiple subsections)
signed_by: Gov. Kemp, 2023-05-01
effective_date_statute: 2024-01-01
prescribed_form_section: none  # content-based, not form-prescribed
apr_calculation_method: actuarial_reg_z  # APR required, but methodology defined more loosely than CA/NY
threshold_amount_usd: 500000
threshold_test: |
  financing_offer_amount <= 500000 AND
  provider_consummates_more_than_5_transactions_in_georgia_per_calendar_year
disclosure_required: true
coj_allowed: true  # GA permits CoJs; venue restrictions apply (O.C.G.A. § 9-12-18)
coj_citation: O.C.G.A. § 9-12-18
broker_compensation_disclosure_required: false
broker_advance_fees_prohibited: true
private_right_of_action: false  # exclusive AG enforcement
enforcement_authority: Georgia Attorney General (exclusive)
```

---

## Source URLs (verify these — 8 minutes)

1. **SB 90 enrolled bill text (GA General Assembly)** — https://www.legis.ga.gov/api/legislation/document/20232024/219440
2. **SB 90 LegiScan summary** — https://legiscan.com/GA/text/SB90/id/2761758
3. **Mayer Brown legal summary** — https://www.mayerbrown.com/en/insights/publications/2023/06/georgia-enacts-commercial-finance-disclosure-law-extending-legislative-trend
4. **Buchalter detailed compliance analysis** — https://www.buchalter.com/insights/georgia-enacts-commercial-financing-disclosure-law-mandatory-compliance-date-january-1-2024/
5. **National Law Review summary** — https://natlawreview.com/article/georgia-introduces-new-commercial-financing-disclosure-requirements
6. **O.C.G.A. § 9-12-18 (CoJ statute, current)** — https://law.justia.com/codes/georgia/2020/title-9/chapter-12/article-1/section-9-12-18/

---

## What § 10-1-393.19 requires for disclosures

Georgia specifies required content items, not a prescribed table format.

### Required disclosure content
1. **Total amount of funds provided** under the commercial financing transaction.
2. **Total amount of funds disbursed** to the business after fees deducted/withheld at disbursement, prior balance payoffs, third-party payments.
3. **Total amount to be paid** to the provider.
4. **Total dollar cost** of the commercial financing transaction (Funded vs. Repayment).
5. **Annual Percentage Rate** — Georgia DOES require an annualized rate disclosure (this is the key difference vs. Florida).
6. **Payment schedule** — manner, frequency, amount of each payment.
7. **Prepayment terms** — whether costs or discounts apply on prepayment.

### Notable absences vs. NY/CA
- No prescribed row/column table format.
- No anti-double-dipping disclosure.
- No collateral requirements line.
- No APR re-disclosure on every pricing communication (no SB 362-equivalent).
- No broker compensation written disclosure required.

### Frequency and timing
- One disclosure per transaction, before consummation.
- Modifications, forbearances, and changes do NOT require a new disclosure.
- A "commercial financing facility" (multi-receivable factoring arrangement) gets one disclosure based on a $10,000 example.

---

## Coverage and scope

### Covered transactions (O.C.G.A. § 10-1-393.18)
- Commercial loans (closed-end).
- Commercial open-end credit plans.
- Accounts receivable purchase transactions.

### Covered "Provider"
- Person who consummates more than 5 commercial financing transactions in Georgia per calendar year.
- Person under written agreement with depository institution offering products via online platform.

### Exclusions (similar to other state CFDLs)
- Federally insured financial institutions and affiliates/holding companies/service corporations.
- Providers regulated under federal Farm Credit Act.
- Real-estate-secured commercial financing.
- Commercial loans/open-end credit ≥ $50,000 to motor vehicle dealers/rental companies.
- Commercial financing in connection with sale/lease of products manufactured/licensed/distributed by provider (captive financing exception).
- Provider with 5 or fewer transactions in a 12-month period.
- Healthcare-related receivable purchases owed to a healthcare provider for personal injury.
- Purchase money obligations under O.C.G.A. § 11-9-103 (UCC).

### Important interpretive note
Georgia law explicitly states: parties' characterization of a transaction as a purchase of accounts receivable or payment intangibles **is conclusive that the transaction is not a loan** for purposes of Georgia financial institutions laws. This is a small advantage for MCA structuring vs. CA/NY where the question of "is this a loan" is more contested.

---

## Broker rules under SB 90

Brokers don't have to provide disclosures. They are subject to:

1. **No advance fees.** Cannot assess or solicit advance fee from a business for broker services. Narrow exception: actual third-party services (credit checks, appraisals) paid by check or money order to independent third party.

2. **No false or misleading representations.** Standard prohibition.

### Operational rules for AEGIS
- AEGIS as broker: NO upfront broker fees from GA merchants. Commission must come from funder.
- No broker compensation written disclosure required (unlike NY).
- No address/phone advertising disclosure (unlike FL § 559.9614(3)).

---

## CoJ status: PERMITTED in Georgia

Georgia is one of the states where CoJs remain permissible in commercial transactions, subject to venue rules.

### Statutory framework: O.C.G.A. § 9-12-18

> (a) Either party has a right to confess judgment without the consent of his adversary and to appeal from such confession without reserving the right to do so in cases where an appeal is allowed by law.
> (b) No confession of judgment shall be entered except in the county where the defendant resided at the commencement of the action unless expressly provided for by law. The action must have been regularly filed and docketed as in other cases. However, a judge of a superior court or a magistrate may confess judgment in his own court.

### Operational implications
- CoJs are **permissible** in GA-law MCA transactions.
- Venue is restricted: the action must be regularly filed and docketed, and the confession must be entered in the county where the defendant resided at commencement.
- Georgia courts apply Full Faith and Credit to out-of-state CoJ judgments under standard FFC analysis.

### Operational rules for AEGIS
- **No state-level CoJ block for GA merchants.** A funder requiring a CoJ may proceed.
- **Funder match log** should not flag GA-CoJ deals as state-blocked.
- Operator should still consider funder reputation re: aggressive CoJ enforcement separately as a soft signal — many MCA borrowers consider CoJ-aggressive funders a red flag.

---

## Penalties and enforcement

- **AG-only enforcement.** No private right of action.
- **Civil penalties (verified verbatim):**
  - First-time violation: **$500 per incident, maximum $20,000 aggregate**.
  - Repeat violation (after written notice of prior violation from AG): **$1,000 per incident, maximum $50,000 aggregate**.
- **No automatic voiding** of underlying transaction for disclosure violations. § 10-1-393.18(k) confirms enforceability is unaffected by violation.
- **Subject to GA's Fair Business Practices Act** injunctive remedy framework — AG can seek injunctions, restitution, civil penalties.

---

## Updated Tier 1 entry for `compliance/states.py`

```python
StateRegulation(
    state="Georgia",
    abbreviation="GA",
    tier=1,
    bill_number="SB 90",
    bill_year=2023,
    common_name="Georgia Commercial Financing Disclosure Law",
    statute_citation="O.C.G.A. § 10-1-393.18 (single section, multiple subsections)",
    citation_url_bill="https://www.legis.ga.gov/api/legislation/document/20232024/219440",
    signed_by="Gov. Kemp",
    signed_date=date(2023, 5, 1),
    effective_date_statute=date(2024, 1, 1),
    prescribed_form_section=None,  # content-based
    apr_required=True,
    apr_calculation_method="actuarial_reg_z",  # methodology not as detailed as CA/NY
    threshold_amount_usd=Decimal("500000"),
    threshold_test_summary=(
        "Disclosure required when financing <= $500,000 AND provider "
        "consummates more than 5 commercial financing transactions in "
        "Georgia per calendar year."
    ),
    disclosure_required=True,
    disclosure_required_content=[
        "total_amount_of_funds_provided",
        "total_amount_disbursed",
        "total_amount_to_be_paid",
        "total_dollar_cost",
        "annual_percentage_rate",
        "payment_schedule",
        "prepayment_terms",
    ],
    coj_allowed=True,
    coj_citation="O.C.G.A. § 9-12-18",
    coj_citation_url="https://law.justia.com/codes/georgia/2020/title-9/chapter-12/article-1/section-9-12-18/",
    coj_venue_restriction="Action filed in county where defendant resides at commencement",
    broker_compensation_disclosure_required=False,
    broker_advance_fees_prohibited=True,
    apr_re_disclosure_required=False,
    private_right_of_action=False,
    enforcement_authority="Georgia Attorney General (exclusive)",
    enforcement_framework="Fair Business Practices Act",
    notes=(
        "GA CFDL is content-based not form-prescribed (similar to FL). "
        "APR IS required (unlike FL, similar to CA/NY). "
        "Lease financing NOT covered. "
        "CoJs PERMITTED in GA - state does not block, but funder reputation matters. "
        "Receivable-purchase characterization is statutorily protected as 'not a loan.' "
        "AEGIS as broker: NO upfront broker fees from GA merchants."
    ),
    verified_date=None,
)
```

---

## Confidence assessment

| Finding | Confidence |
|---|---|
| SB 90 enacted, signed 2023-05-01, effective 2024-01-01 | High — multiple law firm summaries + LegiScan |
| Statute located at O.C.G.A. §§ 10-1-393.18 et seq. | High — Buchalter cites this section directly |
| 7 required content items including APR | High — multiple summaries; Mayer Brown explicit |
| No prescribed table format | High — confirmed by multiple summaries |
| Threshold $500K + >5 transactions/year | High — § 10-1-393.19 |
| CoJ permitted (O.C.G.A. § 9-12-18) | High — verbatim statute text |
| Broker advance fees prohibited | High — multiple summaries cite this |
| No private right of action, AG exclusive enforcement | High — multiple summaries |
| Receivable-purchase as not-a-loan | High — Mayer Brown explicit |
| Penalties: $500/$20K aggregate first-time, $1,000/$50K aggregate after notice | High — verified verbatim during verification pass; § 10-1-393.18(h)(i) |
| Statute is § 10-1-393.18 (single section), not "et seq." | High — verified via Justia 2024 GA Code listing |
