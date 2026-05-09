# Florida — Complete AEGIS Compliance Dossier

**Researched: 2026-05-07** by Claude (web-search based) for operator verification.
**Status when verified: ready to paste into AEGIS.**

---

## TL;DR for AEGIS

- **Tier 1.** Florida Commercial Financing Disclosure Law (FCFDL) is in effect.
- **Statute:** Florida Statutes Chapter 559, Part XIII, §§ 559.961-559.9615. Enacted by HB 1353 (2023), effective July 1, 2023, mandatory compliance for transactions consummated **on or after January 1, 2024**.
- **Disclosure is content-based, not form-prescribed.** Florida specifies *what must be disclosed* (six items) but does NOT prescribe a row/column table format like CA or NY. The disclosure is a written document containing the required content.
- **No APR disclosure required.** Florida is more limited than CA/NY in this respect.
- **Threshold:** $500,000 or less, AND provider must consummate **more than 5** transactions in Florida in any calendar year (the small-volume safe harbor).
- **CoJ banned outright** by Fla. Stat. § 55.05 — historic statute declaring all powers of attorney to confess judgment, before action brought, "absolutely null." Florida courts treat CoJ clauses in MCA contracts as unenforceable.
- **Broker advance fees prohibited.** Brokers cannot collect advance fees (with narrow exceptions for actual third-party services like credit checks).
- **Enforcement:** Florida AG only. No private right of action. Fines: $500/violation up to $20K aggregate; $1,000/violation up to $50K aggregate after written notice of prior violation.

---

## Statute identification

```
state: Florida
abbreviation: FL
tier: 1
bill_number: HB 1353 (2023)
chapter: Chapter 2023-290, Laws of Florida
common_name: Florida Commercial Financing Disclosure Law (FCFDL)
statute_citation: Fla. Stat. §§ 559.961 - 559.9615 (Part XIII of Ch. 559)
signed_by: Gov. DeSantis, 2023-06-26
effective_date_statute: 2023-07-01
mandatory_compliance_date: 2024-01-01  # for transactions consummated on/after this date
prescribed_form_section: none  # content-based, not form-prescribed
apr_calculation_method: not_required  # FL does not require APR disclosure
threshold_amount_usd: 500000
threshold_test: |
  financing_offer_amount <= 500000 AND
  business_located_in_florida AND
  provider_consummates_more_than_5_transactions_in_florida_per_calendar_year
disclosure_required: true
coj_allowed: false
coj_citation: Fla. Stat. § 55.05
broker_compensation_disclosure_required: false
broker_advance_fees_prohibited: true
private_right_of_action: false
enforcement_authority: Florida Attorney General (exclusive)
penalty_per_violation_usd: 500  # initial; 1000 after prior notice
penalty_aggregate_max_usd: 20000  # initial; 50000 after prior notice
```

---

## Source URLs (verify these — 8 minutes)

1. **Fla. Stat. § 559.9613 (the disclosure section, the most important one)** — https://www.flsenate.gov/Laws/Statutes/2024/0559.9613
2. **Fla. Stat. § 559.961 (short title)** — https://www.flsenate.gov/Laws/Statutes/2024/0559.961
3. **HB 1353 bill page (FL Senate)** — https://www.flsenate.gov/Session/Bill/2023/1353
4. **HB 1353 enacted bill text PDF** — https://www.flsenate.gov/Session/Bill/2023/1353/BillText/e1/PDF
5. **HB 1353 staff analysis (good summary)** — https://www.flsenate.gov/Session/Bill/2023/1353/Analyses/h1353e.COM.PDF
6. **Buchalter detailed compliance summary** — https://www.buchalter.com/insights/florida-enacts-commercial-financing-disclosure-law-mandatory-compliance-date-january-1-2024/
7. **Fla. Stat. § 55.05 (CoJ ban)** — https://www.flsenate.gov/Laws/Statutes/2018/Chapter55/All
8. **Trauger v. AJ Spagnol Lumber (1983, FL Supreme Court CoJ caselaw)** — https://law.justia.com/cases/florida/supreme-court/1983/63130-0.html

---

## What § 559.9613 requires for disclosures

Unlike CA's row-by-row table or NY's structured columns, Florida specifies **six required content items** but does NOT prescribe a specific format. The provider must deliver one written disclosure at or before consummation.

### Required disclosure content (Fla. Stat. § 559.9613(2))

1. **Total amount of funds provided** to the business under the agreement.
2. **Total amount of funds disbursed to the business** if less than (1), as a result of fees deducted/withheld at disbursement, amounts paid to provider to satisfy a prior balance, and amounts paid to a third party on behalf of the business.
3. **Total amount that the business must pay** to the provider.
4. **Total dollar cost** of the commercial financing transaction (= (3) − (1)). [This is the finance charge equivalent.]
5. **Manner, frequency, and amount of each payment.** If payment amounts may vary, the manner and frequency of payments and an estimate of the amount of the first payment.
6. **Whether there are any costs or discounts associated with prepayment**, including a reference to the provision in the agreement that creates the contractual rights related to prepayment.

### What's NOT required (notable absences vs. CA/NY)
- No APR or annualized rate calculation.
- No average monthly cost calculation.
- No itemization-of-amount-financed attached document.
- No collateral requirements line.
- No anti-double-dipping disclosure (NY-specific).
- No prescribed table format (rows/columns).
- No broker-compensation written notice (NY-specific).

### Frequency rule
- Only one disclosure per transaction.
- A modification, forbearance, or change to a consummated transaction does NOT trigger a new disclosure.
- For "commercial financing facilities" (multi-receivable factoring arrangements), one disclosure based on a $10,000 example transaction satisfies the requirement.

### Timing
- "At or before consummation of the transaction." No specific lead-time prescribed.

---

## Coverage and scope

### Covered transactions (Fla. Stat. § 559.9611)
- Commercial loan (closed-end loan to a business, secured or unsecured).
- Accounts receivable purchase transaction.
- Commercial open-end credit plan.
- **NOT included:** commercial lease financing transactions (Florida is narrower than CA/NY in this respect).

### Covered "Provider" (§ 559.9611)
- Person who consummates more than 5 commercial financing transactions in Florida per calendar year.
- Person who under a written agreement with a depository institution offers commercial financing products via an online platform that the person administers.

### Exclusions
- Federally insured depository institutions and their affiliates/subsidiaries/holding companies/service corporations.
- Providers regulated under federal Farm Credit Act.
- Real-estate-secured commercial financing.
- Commercial loans/open-end credit ≥ $50,000 to motor vehicle dealers/rental companies.
- Provider that consummates 5 or fewer transactions in Florida per 12-month period.
- Sale or lease of products manufactured/licensed/distributed by provider.

---

## Broker rules under FCFDL (§ 559.9614)

Brokers do NOT have to provide disclosures (unlike CA's § 952 or NY's § 600.21 transmission duties). But brokers are subject to specific prohibitions:

1. **No advance fees.** Cannot assess, collect, or solicit an advance fee from a business to provide services as a broker. Narrow exception: actual services necessary to apply for the financing (credit check, appraisal of security) where payment is by check or money order to an independent third party.

2. **No false or misleading representations.** Cannot make false statements or omit material facts in offering brokering services or, indirectly, engage in fraud or deception.

3. **Advertising disclosure.** Brokers offering services in any advertisement must disclose the actual address and telephone number of the broker's business, and the address and telephone number of any forwarding service used.

### Operational rules for AEGIS
- AEGIS itself, as a broker, must NOT collect upfront broker fees from FL merchants. Commission must come from funder side.
- AEGIS marketing/web pages directed at FL merchants must include the address-and-phone disclosure.
- No transmission/retention requirements parallel to CA § 952.

---

## CoJ status: BANNED in Florida (Fla. Stat. § 55.05)

### Statutory text (verbatim — historic statute, predates MCAs by ~century)

> All powers of attorney for confessing or suffering judgment to pass by default or otherwise, and all general releases of error, made by any person within or outside this state, before such action is brought, shall be absolutely null and void.

This statute is well-settled FL law. Florida courts uniformly hold pre-suit CoJ clauses void as against due process and Florida public policy.

### Important nuance: Out-of-state CoJ enforcement under Full Faith and Credit
Per *Trauger v. AJ Spagnol Lumber Co.*, 442 So.2d 182 (Fla. 1983), the FL Supreme Court held that § 55.05 does not allow Florida courts to refuse enforcement of an *out-of-state* judgment validly entered in a state that permits CoJs. This was a Pennsylvania CoJ enforced in Florida under the Full Faith and Credit Clause.

For AEGIS purposes, this is largely irrelevant: AEGIS isn't entering CoJ-based judgments in Florida. The operational rule is simpler:

### Operational rules for AEGIS
- **Hard-decline rule:** if a funder agreement includes a CoJ AND merchant principal place of business is Florida, score-time hard fail with reason `coj_invalid_in_state` (the CoJ clause itself is void in FL).
- Any FL-based MCA executed under FL law with a CoJ has an unenforceable CoJ provision.
- Funders trying to use NY CoJs against FL merchants must first qualify under NY's residency rule (CPLR § 3218) — which they generally cannot, since the merchant isn't a NY resident.

---

## Penalties

- **First violation:** $500 per incident, max $20,000 aggregate.
- **Repeat violations** (after written notice of prior violation from AG): $1,000 per incident, max $50,000 aggregate.
- **Enforcement is exclusive to FL Attorney General.** No private right of action.
- **Underlying transaction remains valid** — a violation does not void the financing contract. (Notable distinction from some other states.)

---

## Updated Tier 1 entry for `compliance/states.py`

```python
StateRegulation(
    state="Florida",
    abbreviation="FL",
    tier=1,
    bill_number="HB 1353",
    bill_year=2023,
    chapter="Chapter 2023-290, Laws of Florida",
    common_name="Florida Commercial Financing Disclosure Law (FCFDL)",
    statute_citation="Fla. Stat. §§ 559.961 - 559.9615 (Part XIII of Chapter 559)",
    citation_url_statute="https://www.flsenate.gov/Laws/Statutes/2024/0559.9613",
    citation_url_bill="https://www.flsenate.gov/Session/Bill/2023/1353",
    signed_by="Gov. DeSantis",
    effective_date_statute=date(2023, 7, 1),
    mandatory_compliance_date=date(2024, 1, 1),
    prescribed_form_section=None,  # FL is content-based, not form-prescribed
    apr_calculation_method="not_required",
    apr_required=False,
    threshold_amount_usd=Decimal("500000"),
    threshold_test_summary=(
        "Disclosure required when financing <= $500,000 AND business "
        "located in Florida AND provider consummates more than 5 "
        "transactions in Florida per calendar year."
    ),
    disclosure_required=True,
    disclosure_required_content=[
        "total_amount_of_funds_provided",
        "total_amount_disbursed_to_business",
        "total_amount_business_must_pay",
        "total_dollar_cost",
        "manner_frequency_amount_of_each_payment",
        "prepayment_costs_or_discounts",
    ],
    coj_allowed=False,
    coj_citation="Fla. Stat. § 55.05",
    coj_citation_url="https://www.flsenate.gov/Laws/Statutes/2018/Chapter55/All",
    coj_caselaw_note=(
        "FL Supreme Court has held § 55.05 cannot block enforcement of "
        "out-of-state CoJ under Full Faith and Credit (Trauger v. AJ Spagnol "
        "Lumber, 442 So.2d 182, Fla. 1983). FL-law CoJs themselves are void."
    ),
    broker_compensation_disclosure_required=False,
    broker_advance_fees_prohibited=True,
    broker_advertisement_address_disclosure_required=True,
    private_right_of_action=False,
    enforcement_authority="Florida Attorney General (exclusive)",
    penalty_per_violation_usd=Decimal("500"),
    penalty_aggregate_max_usd=Decimal("20000"),
    penalty_per_violation_after_notice_usd=Decimal("1000"),
    penalty_aggregate_after_notice_max_usd=Decimal("50000"),
    notes=(
        "FCFDL is content-based not form-prescribed - no row/column table required. "
        "Six required content items per § 559.9613. "
        "No APR disclosure required (lighter than CA/NY). "
        "Lease financing NOT covered (narrower than CA/NY). "
        "AEGIS as broker: NO upfront broker fees from FL merchants, "
        "address/phone disclosure required in advertisements."
    ),
    verified_date=None,
)
```

---

## Confidence assessment

| Finding | Confidence |
|---|---|
| HB 1353 enacted, statute citation §§ 559.961-559.9615 | High — bill text on FL Senate official site |
| Effective dates (statute 2023-07-01, mandatory 2024-01-01) | High — bill text + multiple law firm summaries |
| Six required disclosure content items | High — verbatim statutory text in § 559.9613(2) |
| No APR required | High — statute text omits APR, multiple law firm summaries note this |
| Threshold $500K + >5 transactions/year | High — § 559.9612 + § 559.9611 |
| CoJ banned by § 55.05 | High — verbatim historic statute text |
| Trauger out-of-state enforcement caveat | High — verified case citation |
| Broker advance fee prohibition | High — § 559.9614 verbatim |
| No private right of action, AG exclusive | High — § 559.9615 + analyses |
| Penalty amounts | High — § 559.9615 verbatim |
