# CFPB UDAAP — Complete AEGIS Compliance Dossier

**Researched: 2026-05-07** by Claude (web-search based) for operator verification.
**Status: applies indirectly. AEGIS marketing, sales, and disclosure conduct must avoid UDAAP-style misrepresentations.**

---

## TL;DR for AEGIS

- **CFPB authority over commercial financing is limited but not zero.** Under the Consumer Financial Protection Act (Title X of Dodd-Frank, 12 USC § 5481 et seq.), CFPB's primary mandate is consumer financial products. **Pure commercial-purpose MCAs are not consumer financial products** and CFPB cannot directly enforce UDAAP against them.
- **However, CFPB has multiple paths to reach commercial activity:**
  1. **Section 1071 fair-lending data** (now MCA-excluded, see `06_section_1071_federal.md`).
  2. **Equal Credit Opportunity Act (ECOA)** prohibits discrimination in any credit transaction including commercial — CFPB enforces ECOA for commercial credit.
  3. **CFPB v. nominally-commercial-but-actually-consumer products** — if CFPB believes a transaction is structured as commercial to evade consumer protection, they may challenge the characterization.
- **Indirect cascade through state law:** state UDAP/UDAAP laws (CA Unfair Competition Law, FL Deceptive and Unfair Trade Practices Act, NY GBL § 349) often apply to commercial transactions and can be enforced by state AGs against MCA brokers.
- **Operational rule:** AEGIS marketing copy, sales scripts, disclosure documents, and merchant communications must avoid statements that could be characterized as unfair, deceptive, or abusive — regardless of CFPB's direct jurisdiction.

---

## What constitutes UDAAP

Under 12 USC § 5531(c)-(d), the CFPB defines:

- **Unfair:** an act causes or is likely to cause substantial injury to consumers, not reasonably avoidable by consumers, and not outweighed by countervailing benefits.
- **Deceptive:** a representation, omission, act, or practice that is likely to mislead a reasonable consumer, where the misleading element is material to the transaction.
- **Abusive:** materially interferes with the consumer's ability to understand a term, takes unreasonable advantage of consumer's lack of understanding, inability to protect their interests, or reasonable reliance on a covered person.

Even though "consumer" is the operative term, state attorneys general apply analogous frameworks ("UDAP") to commercial transactions and use them aggressively in MCA enforcement.

---

## What AEGIS must avoid

### In marketing copy and ads
- ❌ "Guaranteed approval" — deceptive (no funding is guaranteed; any deal can be declined).
- ❌ "Lowest rates in the industry" — deceptive without verification.
- ❌ "No cost to apply" — deceptive if there are credit-pull or third-party fees.
- ❌ "Same-day funding" — deceptive if funding typically takes longer.
- ❌ "Bad credit OK / no credit check" combined with implied advance fee — abusive.
- ❌ Photos or testimonials suggesting individual results are typical.

### In sales scripts and merchant communications
- ❌ Quoting an "interest rate" or "APR" that doesn't reflect the funder's actual disclosure.
- ❌ Telling a merchant "this is just like a loan" or "this is a loan" — misrepresents the legal nature of MCA.
- ❌ Telling a merchant "you can pay it back faster to save money" without disclosing whether the contract permits prepayment savings.
- ❌ Pressuring sign-now language ("expires in 24 hours") that creates artificial urgency.
- ❌ Telling a merchant their credit will improve from MCA repayment — MCAs typically do not report to commercial credit bureaus consistently.

### In disclosure documents
- ❌ Disclosure that misstates funder's actual terms.
- ❌ Disclosure that omits required elements (varies by state — see state dossiers).
- ❌ Disclosure delivered after consummation rather than before.
- ❌ Disclosure that uses different numbers than the actual funder agreement.

### In broker-broker / ISO communications
- ❌ Sub-broker recruitment that promises commission percentages exceeding what AEGIS actually pays.
- ❌ Misrepresenting funder approval criteria to sub-brokers.

---

## State-level UDAP enforcement risk

State analogues are the more immediate concern for AEGIS:

| State | Statute | MCA-relevant enforcement pattern |
|---|---|---|
| California | Cal. Bus. & Prof. Code § 17200 (UCL) | Aggressive; DFPI + AG can both bring claims |
| New York | NY GBL §§ 349, 350 | NY AG actively used in Yellowstone judgment |
| Florida | Fla. Stat. § 501.204 (FDUTPA) | AG-only enforcement in disclosure context (§ 559.9615), but FDUTPA reaches broader |
| Georgia | OCGA § 10-1-393 (FBPA) | AG-only; SB 90 disclosure law lives within FBPA |
| Illinois | 815 ILCS 505 (Consumer Fraud Act) | Both AG and private right of action |

**Critical:** even where MCA-specific disclosure laws restrict enforcement to the AG (like FL § 559.9615), the *general* UDAP statute may have a private right of action. A merchant who claims they were misled by AEGIS could sue under the general state UDAP statute even when MCA disclosure law itself doesn't permit private suits.

---

## What AEGIS implements

### 1. Marketing copy review
Before any new ad, landing page, email template, or sales script goes live: legal/compliance review against the prohibited-claims list above. Maintain a `marketing_review_log` table.

### 2. Sales script standardization
AEGIS provides a small set of approved sales scripts. Sub-brokers and ISOs are contractually prohibited from deviating. Recorded calls (where legally permitted) are spot-audited.

### 3. Disclosure consistency check
Before AEGIS forwards a funder's disclosure to a merchant, AEGIS verifies that:
- The merchant name, EIN, and bank account match the funder agreement.
- The funded amount, finance charge, and APR (where required) match what AEGIS has been told and what AEGIS quoted to the merchant.
- The disclosure is in the correct state-specific format for the merchant's location.

### 4. Complaint log
Maintain a `merchant_complaints` table with: date, merchant ID, complaint description, AEGIS response, resolution. CFPB consumer complaint database equivalents do not exist for commercial — but state AGs do receive merchant complaints, and AEGIS's internal log is critical evidence in any investigation.

### 5. Sub-broker oversight
Per Yellowstone-style enforcement reasoning, AEGIS is responsible for the conduct of brokers it works with. Track sub-broker activity, complaint rate, dispute rate. Terminate relationships with sub-brokers showing UDAP-pattern conduct.

---

## "Disguised loan" reclassification — the UDAP angle

The biggest UDAAP-adjacent risk for MCAs: a court reclassifying the transaction as a usurious loan rather than a receivables purchase. This happens when:
- The MCA has fixed payments with no realistic reconciliation mechanism.
- The merchant has unconditional repayment obligation regardless of revenue.
- Personal guarantees look loan-like rather than fraud guarantees.
- Contract language conflicts with actual practice.

When reclassification happens, the "MCA" becomes a usurious loan and:
- State usury caps apply retroactively.
- The transaction may be void.
- The MCA company may face UDAP claims for misrepresenting the product.
- Brokers who marketed the product as MCA may face derivative claims.

**See `12_mca_vs_loan_reclassification.md` for full operational defense.**

---

## What AEGIS records

```python
class MarketingReviewLog:
    asset_id: UUID  # ad, email, landing page, script
    asset_type: str
    asset_content_hash: str  # immutable record of what was reviewed
    reviewed_by: str
    reviewed_at: datetime
    review_decision: Literal["approved", "approved_with_changes", "rejected"]
    review_notes: str
    deployed_at: datetime | None
    retired_at: datetime | None

class MerchantComplaint:
    id: UUID
    deal_id: UUID | None
    merchant_id: UUID
    received_at: datetime
    received_via: Literal["email", "phone", "letter", "online_form", "regulator_inquiry"]
    complaint_summary: str
    aegis_response: str
    resolved_at: datetime | None
    resolution_summary: str | None
    escalated_to: list[str]  # e.g., ["funder", "attorney"]
    retention_until: date  # received_at + 7 years
```

---

## Source URLs

1. **CFPB UDAAP authority (12 USC § 5531)** — https://www.law.cornell.edu/uscode/text/12/5531
2. **CFPB authority over commercial credit ECOA** — https://www.consumerfinance.gov/compliance/compliance-resources/lending-resources/equal-credit-opportunity-act/
3. **CA UCL § 17200** — https://leginfo.legislature.ca.gov/faces/codes_displayText.xhtml?lawCode=BPC&division=7.&title=&part=2.&chapter=5.&article=
4. **NY GBL § 349** — https://www.nysenate.gov/legislation/laws/GBS/349
5. **FL FDUTPA § 501.204** — https://www.flsenate.gov/Laws/Statutes/2024/501.204
6. **GA FBPA § 10-1-393** — https://law.justia.com/codes/georgia/2010/title-10/chapter-1/article-15/10-1-393/
7. **IL Consumer Fraud Act 815 ILCS 505** — https://www.ilga.gov/legislation/ilcs/ilcs3.asp?ActID=2148

---

## Confidence

| Finding | Confidence |
|---|---|
| CFPB lacks direct UDAAP authority over commercial MCAs | High — CFPA Title X consumer-only scope |
| CFPB ECOA reach over commercial credit | High — ECOA covers business credit |
| State UDAP laws apply to commercial in most states | High — multi-state precedent |
| NY GBL § 349 used in Yellowstone enforcement | High — public NY AG announcement |
| Sub-broker liability cascade | Medium — varies by state; demonstrated in Yellowstone-pattern cases |
| Reclassification UDAP angle | High — recurring theme in MCA litigation |
