# Broker-Specific Rules — Complete AEGIS Compliance Dossier

**Researched: 2026-05-07** by Claude (web-search based) for operator verification.
**Status: AEGIS-as-broker rule consolidation across all top-5 states + federal/contractual.**

---

## TL;DR for AEGIS

The state CFDLs treat brokers differently from funders. Here is the consolidated set of broker-specific obligations across the top-5 states and beyond:

| Rule | CA | NY | FL | GA | IL |
|---|---|---|---|---|---|
| Disclosure transmission duty (unaltered) | ✓ § 952 | ✓ § 600.21 | — | — | — |
| Broker compensation written disclosure | — | ✓ § 600.21(f) | — | — | — |
| Broker advance fees prohibited | — | — | ✓ § 559.9614 | ✓ § 10-1-393.20 | (Loan Brokers Act) |
| Broker advertisement disclosure | — | — | ✓ § 559.9614(3) | — | (Loan Brokers Act) |
| Broker registration with state | — (SB 869 pending) | — | — | — | maybe (verify) |
| Broker bond requirement | — | — | — | — | $25K (Loan Brokers Act) |
| Pattern-of-noncompliance enforcement | ✓ § 952(a)(3)(C) | ✓ § 600.21 | — | — | — |
| 4-year disclosure transmission record retention | ✓ | ✓ | — (recommended) | — (recommended) | — |

Plus: the **federal FTC** broker rules (where AEGIS is a "finance company"), funder ISO contract obligations, and broker E&O insurance norms.

---

## State-by-state broker rules

### California (10 CCR § 952)
**Obligation type:** transmission duty (not advance-fee prohibition).

- **§ 952(a)(1):** Funder must give the disclosure to the broker (or directly to merchant).
- **§ 952(a)(2):** Broker must transmit to merchant unaltered, within timeframe specified by funder.
- **§ 952(a)(3):** Broker must produce evidence of transmission and time of transmission to funder.
- **§ 952(a)(3)(C):** Funder must drop brokers with patterns of noncompliance (cascade).
- **§ 952(d):** 4-year retention of transmission records.

**AEGIS already implements:** disclosure_transmission_log table per `01_california.md`.

### New York (23 NYCRR § 600.21)
**Obligation types:** transmission duty + compensation disclosure + 4-year retention.

- **§ 600.21(b):** Broker may transmit on behalf of funder.
- **§ 600.21(c):** Broker must obtain merchant signature on disclosure before allowing access to financing.
- **§ 600.21(d):** 4-year retention of all related records.
- **§ 600.21(f):** When broker is involved, funder must inform recipient in writing how, and by whom, broker is compensated.

**AEGIS implements:** transmission log + standardized broker-compensation text block delivered to funder for inclusion in disclosure package.

### Florida (Fla. Stat. § 559.9614)
**Obligation types:** advance-fee prohibition + advertising disclosure + no-misrepresentation.

- **§ 559.9614(1)(a):** No advance fees from business for broker services. Narrow exception: actual third-party services (credit checks, appraisals) paid by check or money order to independent third party.
- **§ 559.9614(1)(b):** No false or misleading representations.
- **§ 559.9614(3):** In any advertisement, broker must disclose actual address and phone number of broker's business AND any forwarding service used.

**AEGIS implements:** sales script enforcement (no upfront fees from FL merchants); marketing review (FL ads include address/phone).

### Georgia (O.C.G.A. § 10-1-393.20)
**Obligation types:** advance-fee prohibition + no-misrepresentation.

- Cannot assess or solicit advance fee from a business for broker services. Narrow exception: actual third-party services paid to independent third party.
- Cannot make false or misleading representations.

**AEGIS implements:** same as Florida re: no upfront fees.

### Illinois (Loan Brokers Act of 1995, 815 ILCS 175/)
**Obligation types:** registration + bond + disclosure (if AEGIS qualifies as "loan broker" under Act).

- **815 ILCS 175/15-15(b):** $25,000 surety bond required.
- **815 ILCS 175/15-30:** Disclosure document required to each non-exempt borrower.
- Annual registration with IL Secretary of State.

**Note:** whether AEGIS is a "loan broker" under the Act depends on AEGIS's specific activity scope — see `05_illinois.md`. **Operator must verify before first IL deal.**

---

## Federal FTC Telemarketing Sales Rule (TSR) — 16 CFR Part 310

If AEGIS or its sub-brokers conduct outbound telemarketing to merchants:
- Must comply with TSR's prohibitions on false/misleading representations.
- Must honor Do Not Call requests (within 5 business days).
- Cannot use abusive tactics.
- Recordkeeping for 24 months minimum.

This applies even though merchants are commercial — the TSR's commercial exemption is narrower than commonly believed, and FTC has brought cases against B2B telemarketers.

---

## ISO / Funder contract obligations

Most funder ISO agreements impose broker obligations beyond state law. Common requirements:

1. **Background check requirements.** AEGIS conducts background checks on principals.
2. **AML cascade.** AEGIS implements KYC/CIP per funder standards (see `08_aml_bsa_kyc.md`).
3. **OFAC screening.** AEGIS screens before submission (see `07_ofac_sanctions.md`).
4. **Cybersecurity controls.** AEGIS maintains security per funder standards (see `11_data_security_privacy.md`).
5. **Disclosure transmission.** AEGIS transmits per state laws (see CA/NY).
6. **Anti-stacking.** AEGIS detects and discloses stacking (see `12_mca_vs_loan_reclassification.md`).
7. **No misrepresentation.** AEGIS cannot misrepresent funder terms to merchants.
8. **Fee disclosure.** AEGIS discloses to funder how AEGIS is compensated.
9. **Right of audit.** Funder can audit AEGIS records on reasonable notice.
10. **Indemnification.** AEGIS indemnifies funder for AEGIS-caused losses.
11. **Errors and Omissions (E&O) insurance.** Many funders require AEGIS to carry $1M+ E&O coverage.
12. **Termination.** Funder can terminate AEGIS for any compliance failure.

These are the contractual cascade. Operator should review every funder ISO agreement and ensure AEGIS's controls meet the highest cascade standard.

---

## Broker E&O insurance norms

While not a state law requirement (except IL Loan Brokers Act bond), funders frequently require:
- **$1M minimum E&O coverage.** Some require $2M-$5M.
- **Cyber liability rider.** $1M minimum, covering breach response, regulatory defense, third-party liability.
- **Commercial general liability.** $1M minimum.

Operator should obtain E&O insurance before serving deals from major funders. Brokers without E&O are typically declined for funder onboarding.

---

## Anti-stacking enforcement

Funders increasingly require brokers to certify that no other position is being placed simultaneously. Patterns AEGIS detects:

1. **Bank statement analysis** — multiple MCA payment patterns indicate active positions.
2. **UCC search** — UCC-1 financing statements reveal active positions.
3. **Application date proximity** — two applications to two funders within 48h is a classic stacking signal.

**AEGIS rule:** if active positions detected, AEGIS:
- Discloses to all funders in the application.
- Refuses to broker a position that would put merchant past 2 active positions.
- Requires merchant acknowledgment that they understand stacking risk.

---

## Operator playbook: pre-funding compliance check

Before submitting any deal to any funder, AEGIS validates:

```python
def pre_submission_compliance_check(deal: Deal) -> list[str]:
    issues = []

    # Disclosure
    if deal.merchant.state in TIER_1_STATES:
        if not deal.disclosure_doc_present:
            issues.append("missing_state_disclosure")
        if not deal.disclosure_doc_signed_by_merchant:
            issues.append("disclosure_not_signed")
        if not deal.disclosure_consistent_with_funder_agreement:
            issues.append("disclosure_funder_agreement_mismatch")

    # State-specific broker rules
    if deal.merchant.state == "FL":
        if deal.aegis_charged_advance_fee:
            issues.append("fl_advance_fee_violation")
    if deal.merchant.state == "GA":
        if deal.aegis_charged_advance_fee:
            issues.append("ga_advance_fee_violation")

    # CoJ
    if deal.coj_in_funder_agreement and deal.merchant.state in COJ_BANNED_STATES:
        issues.append(f"coj_banned_in_{deal.merchant.state}")

    # AML
    if not deal.cip_complete:
        issues.append("cip_incomplete")
    if not deal.ofac_cleared:
        issues.append("ofac_not_cleared")

    # Stacking
    if deal.detected_active_positions >= 2:
        issues.append("stacking_warning")

    # Sub-broker
    if deal.sub_broker_id and not deal.sub_broker_compliance_certified_current_year:
        issues.append("sub_broker_compliance_stale")

    return issues
```

If `issues` is non-empty, deal cannot be submitted without operator review.

---

## Source URLs

1. **CA 10 CCR § 952** — https://www.law.cornell.edu/regulations/california/10-CCR-952
2. **NY 23 NYCRR § 600.21** — https://www.law.cornell.edu/regulations/new-york/23-NYCRR-600.21
3. **Fla. Stat. § 559.9614** — https://www.flsenate.gov/Laws/Statutes/2024/559.9614
4. **GA SB 90 (broker rules)** — https://www.legis.ga.gov/api/legislation/document/20232024/219440
5. **IL Loan Brokers Act of 1995** — https://www.ilsos.gov/departments/securities/loanbrokers.html
6. **FTC TSR (16 CFR Part 310)** — https://www.ecfr.gov/current/title-16/chapter-I/subchapter-C/part-310

---

## Confidence

| Finding | Confidence |
|---|---|
| State-by-state broker rule matrix | High — verbatim regulatory text |
| FL/GA advance fee bans verbatim | High |
| NY broker compensation written disclosure | High — § 600.21(f) |
| CA transmission duty (no comp disclosure) | High |
| IL Loan Brokers Act applicability uncertain | Medium — depends on AEGIS activity scope |
| ISO contractual cascade norms | High — industry-standard |
| E&O insurance $1M norm | Medium-High — common funder requirement, varies |
| FTC TSR applicability to B2B telemarketing | Medium-High — narrower commercial exemption than commonly believed |
