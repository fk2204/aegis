# Illinois — Complete AEGIS Compliance Dossier

**Researched: 2026-05-07** by Claude (web-search based) for operator verification.
**Status when verified: ready to paste into AEGIS — but as TIER 2, not Tier 1. See below.**

---

## CRITICAL FINDING — read before treating IL as Tier 1

**Illinois has NOT enacted a commercial financing disclosure law.**

The "Illinois Small Business Truth in Lending Act" (SB 2234) was introduced in the 103rd General Assembly (2023-2024) and **died at the end of session ("session sine die")** without becoming law. It was reintroduced as HB 3477 in the 2025-2026 session but had not been enacted as of the research date.

This contradicts the framing in earlier conversation that Illinois belongs alongside CA, NY, FL, and GA as a "high-stakes state with active disclosure laws." It does not, currently. Illinois is **Tier 2** in the AEGIS framework: served, governed by general state law (consumer fraud act, broker registration, usury), no MCA-specific disclosure obligation.

The AEGIS posture for IL therefore differs:
- No prescribed form, no APR disclosure obligation, no broker advance fee prohibition under a CFDL.
- General Illinois law still applies — interest rate caps, fair lending principles, broker registration if any.
- AEGIS should track the active legislative slot (HB 3477 / successors) for future Tier 1 promotion.
- **CoJs are permitted** for commercial transactions in Illinois under 735 ILCS 5/2-1301(c), with conspicuous-clause and venue requirements.

---

## TL;DR for AEGIS

- **Tier 2** as of research date. No MCA-specific disclosure statute in force.
- **Pending bill to monitor:** HB 3477 (2025-2026 session). If enacted, AEGIS auto-promotes IL to Tier 1.
- **General law applies:** Illinois Consumer Fraud and Deceptive Business Practices Act (815 ILCS 505/), Loan Brokers Act of 1995 (815 ILCS 175/), commercial usury rules.
- **CoJ permitted** for commercial transactions (735 ILCS 5/2-1301(c)). Banned in **consumer** transactions only since 1979.
- **Loan broker registration with the IL Secretary of State** may apply to AEGIS depending on activity scope. **Operator should verify whether AEGIS qualifies as a "loan broker" requiring registration before serving IL deals.**

---

## Statute identification

```
state: Illinois
abbreviation: IL
tier: 2
mca_specific_disclosure_law: false
applicable_general_laws:
  - Illinois Consumer Fraud and Deceptive Business Practices Act (815 ILCS 505/)
  - Loan Brokers Act of 1995 (815 ILCS 175/)
  - 735 ILCS 5/2-1301 (CoJ rules - commercial transactions)
disclosure_required: false  # under any current state-specific MCA statute
coj_allowed: true  # for commercial transactions
coj_citation: 735 ILCS 5/2-1301(c)
loan_broker_registration_required: maybe  # depends on AEGIS activity scope - verify
pending_legislation:
  - HB 3477 (2025-2026, "Small Business Financing Transparency Act")
  - Predecessor SB 2234 (103rd GA, died sine die 2025-01-07)
```

---

## Source URLs (verify these — 6 minutes)

1. **735 ILCS 5/2-1301 (CoJ statute, current)** — https://codes.findlaw.com/il/chapter-735-civil-procedure/il-st-sect-735-5-2-1301/
2. **Illinois Loan Brokers Act of 1995 — Secretary of State landing page** — https://www.ilsos.gov/departments/securities/loanbrokers.html
3. **HB 3477 (2025-2026 pending bill, replaces SB 2234)** — https://legiscan.com/IL/text/HB3477/id/3109034/Illinois-2025-HB3477-Introduced.html
4. **SB 2234 (103rd GA, died) for reference** — https://www.ilga.gov/legislation/billstatus.asp?DocNum=2234&GAID=17&GA=103&DocTypeID=SB&LegID=147119&SessionID=112
5. **McGlinchey legal summary noting SB 2234 status** — https://www.mcglinchey.com/insights/the-small-business-truth-in-lending-act/
6. **Woodstock Institute statement on IL Senate's 2025 passage** — https://woodstockinst.org/press-release/statement-illinois-senate-passes-bill-to-protect-small-businesses/

---

## Why Illinois is Tier 2

The Illinois legislature has been trying to enact a small-business disclosure law since 2023:

- **HB 3064 (2023):** introduced, did not pass.
- **SB 2234 (2023-2024 session):** "Small Business Truth in Lending Act." Passed Illinois Senate. Did not pass House. Died at end of 103rd General Assembly (session sine die 2025-01-07).
- **HB 3477 (2025-2026 session):** "Small Business Financing Transparency Act." Reintroduced version with broker registration requirements. Status pending as of research date.

If/when an Illinois CFDL is enacted, expected features (based on bill drafts):
- Threshold likely $2.5M (matching NY).
- Required APR disclosure (matching CA/NY model).
- Civil penalty up to $10,000 per violation, $20,000 per willful violation.
- Provider registration with Illinois Department of Financial and Professional Regulation.
- Disclosure forms approved for use in other states (CA, NY) likely accepted as compliant.

**Action item for AEGIS:** add a calendar reminder to recheck IL legislative status quarterly. When HB 3477 (or successor) is signed, promote IL from Tier 2 to Tier 1 and build the disclosure template based on the enacted text.

---

## Loan Brokers Act — possible AEGIS obligation

The Illinois Loan Brokers Act of 1995 (815 ILCS 175/) requires "loan brokers" to register with the Illinois Secretary of State, post a $25,000 surety bond, file annual renewals, and provide written disclosures to borrowers under specified conditions.

A "loan broker" under the Act is broadly: any person who, for or in expectation of consideration, arranges or attempts to arrange a loan of money or other thing of value to another person.

**Whether AEGIS is a "loan broker" under the Act depends on:**
- Whether MCAs are "loans" under Illinois law (a contested question — many MCAs are structured as accounts receivable purchases, not loans).
- AEGIS's actual activity (matching/introducing vs. negotiating terms vs. earning commissions).
- Whether the merchant is an Illinois resident borrower.

**Operational rule for AEGIS:** Before serving Illinois deals, operator should:
1. Verify with Illinois Secretary of State whether AEGIS qualifies as a "loan broker" requiring registration. Phone: (217) 782-2756 (Securities Department).
2. If yes: register, post the $25,000 bond, and maintain the disclosure document under 815 ILCS 175/15-30.
3. If no: document the basis for the determination in the AEGIS audit log.

This is a verification, not a research task — the answer depends on facts about how AEGIS operates, not just statute interpretation. **One short call to the Secretary of State's office or to a commercial finance attorney resolves it.**

---

## CoJ status: PERMITTED for commercial transactions (735 ILCS 5/2-1301(c))

Illinois explicitly permits CoJs in commercial contexts but bans them in consumer transactions.

### Statutory text (current, 735 ILCS 5/2-1301(c))

> Any person for a debt bona fide due may confess judgment by himself or herself or attorney duly authorized, without process. The application to confess judgment shall be made in the county in which the note or obligation was executed or in the county in which one or more of the defendants reside or in any county in which is located any property, real or personal, owned by any one or more of the defendants. A judgment entered by any court in any county other than those herein specified has no force or validity, anything in the power to confess to the contrary notwithstanding.
>
> No power to confess judgment shall be required or given after September 24, 1979 in any instrument used in a consumer transaction; any power to confess given in violation hereof is null and void and any judgment entered by a court based on such power shall be unenforceable.

### Operational implications
- **CoJs are valid in IL commercial MCA transactions** if the agreement was executed by a non-consumer business borrower.
- Venue is restricted to: county of execution, county where defendant resides, OR county where defendant has property.
- Conspicuousness requirement: under Illinois case law, the CoJ clause must be in bold/capitals or otherwise conspicuous in the contract.
- Strict construction: courts scrutinize CoJs and may invalidate for procedural defects.

### Operational rules for AEGIS
- **No state-level CoJ block for IL commercial merchants.** A funder requiring a CoJ may proceed.
- Verify the merchant is a non-consumer business borrower (LLC, corporation, partnership) — the consumer-transaction CoJ ban is absolute.
- Track that the funder's CoJ clause is conspicuously presented in the agreement.

---

## What AEGIS produces for IL deals as Tier 2

When AEGIS receives a deal for an IL merchant under Tier 2 posture:

1. **Parse and score normally.** No state-specific exclusion at parse stage.
2. **No prescribed disclosure document required.** AEGIS does NOT need to produce an IL-specific Jinja template.
3. **Generic acknowledgment receipt** is appropriate. Cite: "This commercial financing transaction is governed by Illinois law including the Illinois Consumer Fraud and Deceptive Business Practices Act and applicable usury rules. AEGIS has not yet completed compliance research for an IL-specific disclosure framework, as Illinois has not enacted MCA-specific disclosure legislation as of the date of this transaction."
4. **Loan broker registration check** required before first IL deal (see above).
5. **Soft warning logged** for IL deal flow so operator can re-evaluate when HB 3477 or successor passes.

---

## Updated Tier 2 entry for `compliance/states.py`

```python
StateRegulation(
    state="Illinois",
    abbreviation="IL",
    tier=2,  # NOT Tier 1 - no enacted MCA-specific disclosure law
    general_law_citation=(
        "Illinois Consumer Fraud and Deceptive Business Practices Act (815 ILCS 505/); "
        "Illinois Loan Brokers Act of 1995 (815 ILCS 175/); "
        "Illinois Code of Civil Procedure on confessions of judgment (735 ILCS 5/2-1301)"
    ),
    citation_url=(
        "https://codes.findlaw.com/il/chapter-735-civil-procedure/il-st-sect-735-5-2-1301/"
    ),
    disclosure_required=False,
    coj_allowed=True,  # for commercial transactions only
    coj_citation="735 ILCS 5/2-1301(c)",
    coj_consumer_ban=True,  # IL bans CoJs in consumer transactions since 1979
    loan_broker_registration_authority="Illinois Secretary of State",
    loan_broker_registration_required="verify_before_first_deal",
    loan_broker_bond_required_usd=Decimal("25000"),
    pending_legislation=[
        PendingLegislation(
            bill_number="HB 3477",
            year=2025,
            session="2025-2026",
            common_name="Small Business Financing Transparency Act",
            status="introduced",
            would_promote_to_tier=1,
            citation_url="https://legiscan.com/IL/text/HB3477/id/3109034/Illinois-2025-HB3477-Introduced.html",
        ),
        PendingLegislation(
            bill_number="SB 2234",
            year=2023,
            session="103rd GA",
            common_name="Small Business Truth in Lending Act",
            status="died_sine_die_2025_01_07",
            citation_url="https://www.ilga.gov/legislation/billstatus.asp?DocNum=2234&GAID=17&GA=103&DocTypeID=SB&LegID=147119&SessionID=112",
        ),
    ],
    notes=(
        "Illinois has NOT enacted an MCA-specific disclosure law. Multiple "
        "attempts (HB 3064 2023; SB 2234 2023-2024 died sine die; HB 3477 2025-2026 pending). "
        "AEGIS treats IL as Tier 2 until enactment. CoJs permitted in commercial MCA "
        "transactions. Loan broker registration may apply - verify with IL Secretary of State "
        "Securities Department before first IL deal. Generic acknowledgment receipt only."
    ),
    quarterly_review_required=True,
    verified_date=None,
)
```

---

## Confidence assessment

| Finding | Confidence |
|---|---|
| **Illinois has NOT enacted an MCA-specific disclosure law as of 2026-05-07** | High — confirmed via Illinois General Assembly bill status pages and ABA legal survey (June 2025) which lists 9 enacted state CFDLs and Illinois is NOT among them |
| SB 2234 died sine die end of 103rd GA | High — LegiScan + IL General Assembly status |
| HB 3477 introduced, pending | High — LegiScan |
| 735 ILCS 5/2-1301 governs CoJs, allows commercial CoJs | High — verbatim statute |
| Loan Brokers Act of 1995 may require registration | Medium — Act exists and applies to "loan brokers" but applicability to MCA brokerage specifically depends on facts; verify before relying |
| Bond amount $25,000 | High — IL SOS website |

---

## Recommended action sequence for IL

1. **Treat IL as Tier 2 in AEGIS.** Generic acknowledgment receipt; no prescribed-form template.
2. **Verify Loan Brokers Act applicability** with IL Secretary of State Securities Department before first IL deal. ~15 minute phone call.
3. **Track HB 3477** quarterly. If enacted, this dossier needs major rewrite and IL gets promoted to Tier 1 with a prescribed-form template.
4. **For now, use the IL Tier 2 entry above.** No CoJ block at state level for commercial deals.
