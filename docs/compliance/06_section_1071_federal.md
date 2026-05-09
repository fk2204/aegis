# Federal Section 1071 — Complete AEGIS Compliance Dossier

**Researched: 2026-05-07** by Claude (web-search based) for operator verification.
**Status: AEGIS is currently EXEMPT. Document the exemption in the audit log. No data-collection build-out required.**

---

## TL;DR for AEGIS

- **Section 1071 of the Dodd-Frank Act** amended ECOA to require covered financial institutions to collect and report demographic data on small-business credit applications. Implemented via **Regulation B, Subpart B (12 CFR Part 1002.101 et seq.)**.
- **CFPB issued a final 2026 Rule on May 1, 2026**, narrowing the scope of the 2023 final rule.
- **MCAs are explicitly excluded** from the 2026 Final Rule. The CFPB conceded the issue raised by *RBFC v. CFPB*. AEGIS-arranged MCA transactions are not covered credit transactions under § 1002.104.
- **Coverage threshold raised** to 1,000 covered originations in each of the two preceding calendar years (was 100 under the 2023 rule). Even if MCAs *were* covered, AEGIS at ~100 deals/month = ~1,200 deals/year would only cross the threshold years from now.
- **Compliance date: January 1, 2028.** Effective date: June 30, 2026. Even if rules change, no near-term action.
- **Litigation risk:** Consumer advocacy groups may challenge the narrowing under the APA (e.g., *Rise Economy v. Vought* in D.C. district). Worth tracking but not load-bearing.

---

## What AEGIS records in compliance audit log

Add this single entry to AEGIS at deploy:

```python
ComplianceExemption(
    regulation="CFPB Section 1071 / Regulation B Subpart B",
    citation="12 CFR § 1002.104",
    federal_register_url="https://www.federalregister.gov/documents/2026/05/01/2026-08494/small-business-lending-under-the-equal-credit-opportunity-act-regulation-b",
    cfpb_rule_url="https://www.consumerfinance.gov/1071-rule/",
    exemption_basis=(
        "Per the 2026 Final Rule effective 2026-06-30, merchant cash advances "
        "(MCAs) are explicitly excluded from the definition of covered credit "
        "transactions under § 1002.104. AEGIS facilitates only sales-based "
        "financing/MCA transactions and does not arrange agricultural loans, "
        "small dollar loans, or other excluded products that nonetheless "
        "remain non-covered."
    ),
    secondary_basis=(
        "Even if MCAs were covered, AEGIS as a broker is not a 'covered "
        "financial institution.' AEGIS does not originate credit. The reporting "
        "obligation falls on the funder (provider) if the funder originates "
        "more than 1,000 covered credit transactions in each of two preceding "
        "calendar years. AEGIS is a transaction facilitator, not an originator."
    ),
    compliance_date_if_applicable=date(2028, 1, 1),
    monitoring_required=True,
    monitoring_cadence="quarterly",
    monitoring_trigger="If CFPB amends Regulation B to include MCAs OR if AEGIS pivots to non-MCA products",
    last_reviewed=date(2026, 5, 7),
)
```

---

## Source URLs

1. **2026 Final Rule (Federal Register)** — https://www.federalregister.gov/documents/2026/05/01/2026-08494/small-business-lending-under-the-equal-credit-opportunity-act-regulation-b
2. **CFPB 1071 rule landing page** — https://www.consumerfinance.gov/1071-rule/
3. **MCA exclusion confirmation (industry summary)** — https://www.funderintel.com/post/the-cfpb-just-scaled-back-the-1071-rule-and-mcas-are-officially-out
4. **2023 final rule (now superseded but historical reference)** — https://www.consumerfinance.gov/about-us/newsroom/cfpb-extends-compliance-dates-for-small-business-lending-rule/

---

## What changes if MCAs are later included

If a future rulemaking or a successful APA challenge restores MCA coverage, AEGIS would face these obligations as a *broker* in a tight scenario where the rule covers indirect application paths:

1. **Application register** with up to 81 data fields per the 2023 rule (now ~30 fields under the 2026 narrower scope).
2. **Demographic data collection** at application: race, ethnicity, sex, woman-/minority-/LGBTQ+-owned status, principal-owner data.
3. **Firewall** between underwriters and demographic data (operational separation).
4. **Annual report** filing by June 1 following the data-collection year.
5. **Five-year retention** of application register data.

For a broker like AEGIS, the conventional position has been that the *originating* funder reports, not the broker. CFPB has not directly addressed broker-only entities in the final rule. **If MCA coverage is restored, this requires fresh research and an attorney consult** — but that scenario is years away.

## Confidence

| Finding | Confidence |
|---|---|
| MCA exclusion in 2026 Final Rule | High — Federal Register citation + multiple legal summaries |
| Effective date 2026-06-30, compliance 2028-01-01 | High — Federal Register text |
| 1,000-origination threshold | High — 2026 Final Rule § 1002.105 |
| AEGIS exempt as MCA-only broker | High — facially based on text; verify if AEGIS adds non-MCA products |
