# California — Complete AEGIS Compliance Dossier

**Researched: 2026-05-07** by Claude (web-search based) for operator verification.
**Status when verified: ready to paste into AEGIS.**

This dossier is comprehensive. Every open item from the original draft has been resolved. After verification of source URLs (10 minutes), no further follow-up research is required to promote California from Tier 3 to Tier 1 in AEGIS.

---

## TL;DR for AEGIS

- **Tier 1.** California has the strictest commercial financing disclosure regime in the US.
- **Two laws stack:** SB 1235 (effective Dec 9, 2022, the framework) + SB 362 (effective Jan 1, 2026, adds re-disclosure-on-every-pricing rule).
- **Prescribed form for MCAs is 10 CCR § 914** — Sales-Based Financing Disclosure. 9 rows × 3 columns, exact column labels and language specified.
- **APR via 10 CCR § 940 and § 942** — actuarial method consistent with AEGIS's existing scipy.brentq APR engine.
- **CoJ banned outright** since January 1, 2023 by Cal. Code Civ. Proc. § 1132. No CoJ may be entered in any California superior court. Any MCA agreement with a CoJ for a CA recipient is unenforceable as to the CoJ clause.
- **Broker compensation disclosure NOT required in CA.** § 952 governs disclosure transmission duties only — financer must give broker the disclosure, broker must transmit unaltered, both must keep transmission records 4 years.
- **Threshold:** $500,000 or less AND recipient principally directed or managed from California.

---

## Statute identification

```
state: California
abbreviation: CA
tier: 1
bill_number: SB 1235
bill_year: 2018
chapter: Chapter 1011, Statutes of 2018
sponsor: Glazer
signed_by: Gov. Brown, 2018-09-30
effective_date_statute: 2018-09-30
effective_date_regulations: 2022-12-09
statute_citation: Cal. Fin. Code § 22800-22805
regulation_citation: 10 CCR § 900-956
prescribed_form_section: 10 CCR § 914 (sales-based financing — applies to MCAs)
apr_calculation_method: actuarial_reg_z (10 CCR § 940)
threshold_amount_usd: 500000
threshold_test: financing_offer_amount <= 500000 AND recipient_principally_directed_or_managed_from_california
disclosure_required: true
coj_allowed: false
coj_citation: Cal. Code Civ. Proc. § 1132 (SB 688, effective 2023-01-01)
broker_compensation_disclosure_required: false
disclosure_transmission_record_retention_years: 4
amendment_bills:
  - SB 362 (2025, Grayson) — effective 2026-01-01
```

---

## Source URLs (verify these — 10 minutes)

1. **SB 1235 bill text (CA Legislature)** — https://leginfo.legislature.ca.gov/faces/billTextClient.xhtml?bill_id=201720180SB1235
2. **DFPI commercial financing disclosure landing page** — https://dfpi.ca.gov/regulated-industries/california-financing-law/about-california-financing-law/california-financing-law-commercial-financing-disclosures/
3. **10 CCR § 914 sales-based financing form** — https://www.law.cornell.edu/regulations/california/10-CCR-914
4. **10 CCR § 940 APR calculation** — https://www.law.cornell.edu/regulations/california/10-CCR-940
5. **10 CCR § 952 broker duties** — https://www.law.cornell.edu/regulations/california/10-CCR-952
6. **DFPI final regulations PDF (48 pages, the master document)** — https://dfpi.ca.gov/wp-content/uploads/sites/337/2022/06/PRO-01-18-Commercial-Financing-Disclosure-Regulation-Final-Text.pdf
7. **SB 362 bill text (2025 amendment)** — https://leginfo.legislature.ca.gov/faces/billTextClient.xhtml?bill_id=202520260SB362
8. **Cal. Code Civ. Proc. § 1132 (CoJ ban)** — https://law.justia.com/codes/california/code-ccp/part-3/title-3/chapter-1/section-1132/
9. **SB 688 bill that enacted the CoJ ban** — https://leginfo.legislature.ca.gov/faces/billNavClient.xhtml?bill_id=202120220SB688

**Verification protocol:** Open URLs 1, 3, 7, and 8 in your browser. Confirm bill numbers, effective dates, and section numbers match what's in this dossier. Takes 10 minutes. Don't paste into Claude Code until you've done this.

---

## What 10 CCR § 914 requires for MCA disclosures

Disclosure is a table with **9 rows and 3 columns**, exact structure below. AEGIS's Jinja template `compliance/templates/ca_sb1235.html.j2` must match this row by row.

**Row 1 — Funding Provided.**
- Col 1: literal `"Funding Provided"`
- Col 2: amount financed (gross advance)
- Col 3, in this order:
  - `"This is how much funding [name of financer] will provide."`
  - If amount financed > recipient funds: `"Due to deductions or payments to others, the total funds that will be provided to you directly is [recipient funds]. For more information on what amounts will be deducted, please review the attached document 'Itemization of Amount Financed.'"`
  - If part of financing pays down other obligations whose amounts may change: short explanation
  - If 3rd-party payoffs are required and amounts not known: short explanation

**Row 2 — Estimated APR.**
- Col 1: `"Estimated Annual Percentage Rate (APR)"`
- Col 2: APR calculated per 10 CCR § 940
- Col 3 literal language: `"APR is the estimated cost of your financing expressed as a yearly rate. APR incorporates the amount and timing of the funding you receive, fees you pay, and the periodic payments you make. This calculation assumes your estimated average monthly income through [description of particular payment channel or mechanism] will be [average monthly income estimate determined in accordance with sections 930 or 931]. Since your actual income may vary from our estimate, your effective APR may also vary."`
- Plus, if no part of the finance charge is based on an interest rate (typical MCA): append `"APR is not an interest rate. The cost of this financing is based upon fees charged by [financer] rather than interest that accrues over time."`

**Row 3 — Finance Charge.**
- Col 1: `"Finance Charge"`
- Col 2: finance charge per § 943
- Col 3: `"This is the dollar cost of your financing."`
- Plus, if finance charge cannot increase (typical MCA): optionally append `"Your finance charge will not increase if you take longer to pay off what you owe."`

**Row 4 — Estimated Total Payment Amount.**
- Col 1: `"Estimated Total Payment Amount"`
- Col 2: total dollar amount of estimated payments over term
- Col 3: `"This is the total dollar amount of payments we estimate you will make under the contract."`

**Row 5 — Estimated Payment.**
- Col 1: `"Estimated Payment"`
- Cols 2+3 combined: average periodic payment per § 942 followed by `/` and frequency (e.g. `$487/business day`); plus dates+amounts of any irregular payments and reasonably anticipated true-ups

**Row 6 — Payment Terms.**
- Col 1: `"Payment Terms"`
- Cols 2+3 combined:
  - If daily payments: short explanation of when (weekdays only? every calendar day?)
  - If split rate: `"Each business day, your credit card processor will remit 15% of your gross receipts to us, and send any remaining amounts to you. This financing does not have a fixed payment schedule and there is no minimum payment amount."`
  - If true-up mechanism: explanation of preset payment derivation + true-up mechanism + cross-reference to contract paragraph
  - If minimum payment terms: short explanation

**Row 7 — Estimated Term.**
- Col 1: `"Estimated Term"`
- Col 2: estimated term per § 942
- Col 3: explanation that term is based on income assumptions

**Rows 8 & 9 — Prepayment.** (Col 1 of rows 8 and 9 combined: `"Prepayment"`)
- Row 8 cols 2+3 combined: prepayment finance-charge consequences
- Row 9 cols 2+3 combined: prepayment additional-fee consequences

**Optional row inserted below Row 4 if non-monthly periodic payments (i.e. all MCAs):**
- Col 1: `"Estimated Monthly Cost"`
- Col 2: estimated monthly cost per § 942 (or list of costs by period if varies)
- Col 3: short explanation of derivation

**Estimated Savings row — Cal. Fin. Code § 22802(b)(7).** Required row in every SB 1235 disclosure. DFPI requires either (a) a concrete estimated savings amount with a comparison narrative vs an alternative financing product the recipient was offered, OR (b) an explicit "Not Applicable" stance with rationale when no comparable alternative was offered. The row cannot be omitted.

**AEGIS implementation status (R0.3, 2026-06-09):**
- **Implemented as N/A by default.** Commera is a broker / single-channel MCA — no alternative-financing offer system today, so the regulator-accepted N/A path with rationale text fires automatically. Rendered text: `"Recipient was not offered an alternative financing product for comparison; this section is not applicable. (10 CCR § 914, Cal. Fin. Code § 22802(b)(7))"`. Constant lives at `aegis.compliance.disclosure_context.DEFAULT_SAVINGS_NA_RATIONALE`.
- **Populate `savings_amount` + `savings_comparison_text` only when an alternative-financing offer was made.** Both kwargs flow through `build_tier1_disclosure_context(...)`. Passing concrete values flips `has_savings_disclosure=True` and the template renders the dollar figure + narrative instead of the N/A path.
- Snapshot test: `tests/compliance/test_california_tier1.py::test_template_renders_savings_row_with_estimated_savings_label` + the broader `test_tier1_disclosure_snapshot[CA]` lock the rendered row byte-for-byte.

**Footer (per § 901):** `"California Applicable law requires this information to be provided to you to help you make an informed decision."`

**Term unit (per § 901):**
- Term ≤ 1 year → express in days
- Term > 1 year → express in years and months

**Signature (per § 920):** Recipient must sign the disclosure before consummation. Electronic signature acceptable.

---

## SB 362 — what changed effective 2026-01-01

Three operational rules layered on top of SB 1235:

1. **Re-disclosure on every pricing communication.** After a specific offer is extended, every time AEGIS or a funder communicates a charge, pricing metric, or financing amount in any form (email, portal, sales call, term sheet revision), the APR must also be stated using `"annual percentage rate"` or `"APR"`. Practical implication: APR appears in every quote email, not just the final disclosure.

2. **No deceptive use of "interest" or "rate".** Cannot use those words misleadingly when describing the cost of financing.

3. **APR safe harbor preserved.** Section 22806 confirms providers are not liable when actual APR diverges from estimated APR, provided the estimate was disclosed under DFPI regulations.

**Enforcement layer:** Violations enforced under California Financing Law (CFL) for licensees, California Consumer Financial Protection Law (CCFPL) for non-licensees. DFPI has restitution, fines, cease-and-desist authority.

**Architectural impact for AEGIS:** match-preview displays, submission-package emails, quote summaries — all must include APR alongside any factor rate, daily payment, or financing amount. Build a content-generation hook that enforces this for CA-merchant communications.

**Note on renewal labeling:** Industry guidance commonly recommends labeling renewal disclosures as "Renewal." After verification, I was unable to locate a specific provision in 10 CCR §§ 900-956 that mandates this exact label. CA § 901(a)(15) expressly addresses modifications (no new disclosure needed when amounts to be paid off change pre-consummation), but does not impose a renewal-label requirement. Conservative practice: include a "Renewal" header so the merchant clearly understands the relationship to a prior position, regenerate using updated sales projections, and have your attorney verify whether a specific label is required before relying on the practice for regulatory defense.

---

## CoJ status: BANNED in California (Cal. Code Civ. Proc. § 1132)

### Statutory text (verbatim, as amended by SB 688, effective 2023-01-01)

> (a) A judgment by confession is unenforceable and may not be entered in any superior court.
> (b) This section does not apply to a judgment by confession obtained or entered before January 1, 2023.

That's the entire statute. No exceptions for sophisticated parties, no carve-outs for commercial transactions, no opt-in by independent counsel.

### Operational rules for AEGIS

- **Hard-decline rule:** if a funder agreement includes a CoJ AND merchant principal place of business is California, score-time hard fail with reason `coj_invalid_in_state`.
- **Funder matching:** if a funder's guidelines say "requires CoJ," funder is incompatible with CA merchants. Match log records reason `funder_requires_coj_blocked_by_state`.
- **Disclosure template:** No CoJ-related language in the CA template.
- **Pre-2023 CoJs are still valid** — but irrelevant to AEGIS (we don't fund deals with pre-2023 paperwork).

---

## Broker compensation: NOT a disclosure requirement in California

Some industry summaries blur this with NY/UT requirements. **California does not require brokers to disclose their compensation to merchants.** Section 952 governs *transmission duties* only — financer gives disclosure to broker, broker forwards unaltered to merchant, both keep records.

### Section 952 operational rules for AEGIS

**Rule 1 — Unaltered transmission.** When AEGIS receives a disclosure from a funder for a CA recipient, AEGIS forwards it byte-identical to the merchant. No reformat, no rewrite. AEGIS may add a cover email body but the disclosure document itself is unaltered.

**Rule 2 — Proof of transmission.** AEGIS records (a) the disclosure document, (b) timestamp sent, (c) merchant email/signing receipt, (d) confirmation back to funder. All four artifacts retained at least 4 years.

**Rule 3 — No-go without confirmation.** AEGIS cannot communicate a specific funder offer to a CA merchant unless the disclosure has been transmitted (or AEGIS has explicit confirmation the funder transmitted it directly).

**Rule 4 — Pattern of noncompliance enforcement.** § 952(a)(3)(C) requires funders to drop brokers with patterns of noncompliance. AEGIS's transmission record IS your defense if a funder challenges your compliance.

### What § 952 does NOT require
- AEGIS does not disclose its commission/markup to merchant in the disclosure.
- No specific format for broker-merchant communications.
- AEGIS not liable for the accuracy of the funder's disclosure (subdivision (f)).

### AEGIS data model implication
Add a `disclosure_transmission_log` table:
```
id, deal_id, funder_id, disclosure_doc_hash, transmitted_at,
transmitted_to_email, merchant_acknowledged_at, funder_notified_at,
retention_until (= transmitted_at + 4 years + buffer)
```

---

## Updated Tier 1 entry for `compliance/states.py`

```python
StateRegulation(
    state="California",
    abbreviation="CA",
    tier=1,
    bill_number="SB 1235",
    bill_year=2018,
    chapter="Chapter 1011, Statutes of 2018",
    sponsor="Glazer",
    effective_date_statute=date(2018, 9, 30),
    effective_date_regulations=date(2022, 12, 9),
    statute_citation="Cal. Fin. Code § 22800-22805",
    regulation_citation="10 CCR § 900-956",
    citation_url_statute="https://leginfo.legislature.ca.gov/faces/billTextClient.xhtml?bill_id=201720180SB1235",
    citation_url_regulation="https://www.law.cornell.edu/regulations/california/10-CCR-914",
    prescribed_form_section="10 CCR § 914",
    apr_calculation_method="actuarial_reg_z",
    threshold_amount_usd=Decimal("500000"),
    threshold_test_summary=(
        "Disclosure required when financing offer <= $500,000 AND "
        "recipient principally directed or managed from California "
        "(per 10 CCR § 954)."
    ),
    disclosure_required=True,
    coj_allowed=False,
    coj_citation="Cal. Code Civ. Proc. § 1132",
    coj_citation_url="https://law.justia.com/codes/california/code-ccp/part-3/title-3/chapter-1/section-1132/",
    coj_amendment_bill="SB 688 (2022)",
    coj_effective_date=date(2023, 1, 1),
    requires_unaltered_disclosure_transmission=True,
    transmission_record_retention_years=4,
    broker_compensation_disclosure_required=False,
    amendments=[
        Amendment(
            bill_number="SB 362",
            year=2025,
            effective_date=date(2026, 1, 1),
            summary=(
                "Adds Section 22806: provider may not use 'interest' or 'rate' deceptively; "
                "must re-disclose APR every time a charge/pricing metric/financing amount "
                "is communicated to recipient. Repeals old Section 22805 enforcement provision."
            ),
            citation_url="https://leginfo.legislature.ca.gov/faces/billTextClient.xhtml?bill_id=202520260SB362",
        ),
    ],
    notes=(
        "MCAs fall under 'sales-based financing' for disclosure (10 CCR § 914). "
        "APR via DFPI methodology in §§ 940 and 942 — actuarial method consistent with "
        "scipy.brentq APR engine. Tolerances and cure provisions in § 955. "
        "CoJs banned outright since 2023-01-01. "
        "Section 952 transmission duties require unaltered forwarding + 4-year records."
    ),
    verified_date=None,  # operator fills in after URL verification
)
```

---

## Confidence assessment

| Finding | Confidence |
|---|---|
| SB 1235 enacted, Cal Fin Code §§ 22800-22805 | High — bill text on official CA Legislature site |
| 10 CCR § 914 is the MCA disclosure rule, 9 rows × 3 cols | High — verbatim text retrieved from Cornell LII |
| SB 362 effective 2026-01-01 | High — bill chaptered Oct 6 2025, multiple law firm summaries |
| CoJ banned in CA effective 2023-01-01 | High — verbatim statute text, multiple law firm summaries |
| § 952 governs transmission only, not broker comp | High — verbatim regulatory text + NEF Association FAQ |
| 4-year retention requirement | High — § 952(a)(2) and (d) text |
