# AEGIS Compliance Posture — Master Document

**Researched: 2026-05-07** by Claude (web-search based) for operator verification.
**Status: master document. Ties together all 14 dossiers in this folder.**

---

## Purpose

This is the single document that summarizes AEGIS's complete compliance obligation surface. Use this as the table of contents into the 14 detailed dossiers. Use it for funder due diligence, attorney consults, and operator self-audit.

---

## Compliance obligation matrix

| Obligation | Source | Direct/Cascade | AEGIS dossier | Status |
|---|---|---|---|---|
| State disclosure (CA) | SB 1235 + SB 362, 10 CCR § 914 | Direct | `01_california.md` | Tier 1 |
| State disclosure (NY) | CFDL, 23 NYCRR § 600.6 | Direct | `02_new_york.md` | Tier 1 |
| State disclosure (FL) | FCFDL, Fla. Stat. § 559.961 et seq. | Direct | `03_florida.md` | Tier 1 |
| State disclosure (GA) | SB 90, OCGA § 10-1-393.18 et seq. | Direct | `04_georgia.md` | Tier 1 |
| State disclosure (IL) | None enacted (HB 3477 pending) | n/a | `05_illinois.md` | Tier 2 |
| State disclosure (other 41 states) | Varies; most unenacted | n/a | (see state research) | Tier 2/3 |
| Federal Section 1071 | CFPB Reg B Subpart B | Exempt (MCAs excluded) | `06_section_1071_federal.md` | Exempt |
| OFAC sanctions | 31 CFR Ch. V | Direct | `07_ofac_sanctions.md` | Required, in Phase 3 |
| AML / BSA / KYC | 31 USC § 5312, 31 CFR Ch. X | Cascade (via funder ISO) | `08_aml_bsa_kyc.md` | Cascade-implemented |
| CFPB UDAAP / state UDAP | 12 USC § 5531, state analogs | Indirect / direct (state) | `09_cfpb_udaap.md` | Required |
| Record retention | Multi-statute | Direct | `10_record_retention.md` | Required |
| Data security (NY DFS Part 500) | 23 NYCRR Part 500 | Cascade (via funder) | `11_data_security_privacy.md` | Cascade-implemented |
| Data security (FTC Safeguards) | 16 CFR Part 314 | Direct | `11_data_security_privacy.md` | Required |
| State breach notification | State statutes | Direct | `11_data_security_privacy.md` | Required |
| MCA reclassification defense | State case law | Direct | `12_mca_vs_loan_reclassification.md` | Architectural |
| Broker advance-fee (FL/GA) | Fla. Stat. § 559.9614, OCGA § 10-1-393.20 | Direct | `13_broker_specific_rules.md` | Required |
| IL Loan Brokers Act | 815 ILCS 175 | Direct (verify) | `13_broker_specific_rules.md` | Verify before first IL deal |
| Renewal re-disclosure | CA SB 362, NY § 600.6 | Direct | `14_renewal_redisclosure.md` | Required |
| FTC Telemarketing Sales Rule | 16 CFR Part 310 | Direct (if telemarketing) | `13_broker_specific_rules.md` | If applicable |

---

## Database schema for compliance

AEGIS needs the following tables to operationalize this compliance posture. Each is described in detail in the cited dossier.

```python
# State regulation framework
class StateRegulation: ...  # populated from state dossiers 01-05

# Disclosure tracking
class DisclosureTransmissionLog: ...  # CA § 952, NY § 600.21
class DisclosureDocument: ...  # generated PDFs/HTML

# OFAC
class OFACScreeningLog: ...  # see 07
class OFACBlockedTransactionReport: ...  # if any

# AML/CIP
class MerchantIdentityVerification: ...  # see 08
class SuspiciousActivityLogInternal: ...  # AEGIS-internal, not SAR-filed

# UDAAP/marketing
class MarketingReviewLog: ...  # see 09
class MerchantComplaint: ...  # see 09

# Retention
class DataRetentionPolicy: ...  # see 10
class RecordRetentionMetadata: ...  # see 10

# Security
class CybersecurityProgram: ...  # see 11
class CybersecurityIncident: ...  # see 11
class CybersecurityAccessLog: ...  # § 500.6 audit trail

# Funder oversight
class FunderMcaTemplateReview: ...  # see 12
class FunderMatchSignals: ...  # see 12
class StackingDetectionResult: ...  # see 12

# Renewal handling
class RenewalContext: ...  # see 14

# Compliance exemptions documented
class ComplianceExemption: ...  # e.g., Section 1071 exemption per 06
```

These overlap with tables already in AEGIS's data model from Phases 0-4. Operator should review Phase 4's existing compliance tables and merge with this list.

---

## Calendar of compliance actions

### Daily
- Refresh OFAC SDN cache (every hour automated; max staleness 7 days enforced).
- Daily reaper job for expired records.

### Weekly
- Review failed OFAC screenings + operator dispositions on near-matches.
- Review marketing/sales-script approvals queue.
- Monitor active deals for stacking signals.

### Monthly
- Review merchant complaints log; track trends.
- Verify all state disclosure templates still match current statutory text.
- Operator reviews funder match log for reclassification-risk patterns.

### Quarterly
- Verify all State Tier 3 status — has any state enacted CFDL since last review?
  - Check IL HB 3477 status.
  - Check New Jersey, Maryland, Mississippi, North Carolina, Texas pending bills.
- Check AEGIS revenue and CA-merchant count against CCPA thresholds.
- Vulnerability scan of AEGIS production environment.
- Review third-party service provider security attestations (Hetzner, Cloudflare, Anthropic, Supabase).
- Review pending state legislation tracker.

### Semi-annually
- Tabletop incident response exercise.
- Review and update Data Retention Policy for new record categories.

### Annually
- Cybersecurity program review and CISO report.
- Risk assessment.
- Annual penetration test (or attestation of equivalent monitoring).
- Annual AEGIS staff cybersecurity awareness training.
- Annual NY DFS attestation by April 15 (if cascaded by funder requiring it).
- File annual OFAC Form TD F 90-22.50 by September 30 (only if AEGIS had blocked transactions).
- Review insurance coverage (E&O, cyber, GL).
- Review all funder ISO agreements for changed obligations.
- Review state legislative tracker for enacted laws affecting AEGIS.

### Event-driven
- New funder onboarding: Funder template review, ISO contract review.
- New state served: full state research and dossier creation.
- State regulation amended: re-validate disclosure templates.
- Cybersecurity incident: 72h notification cascade.
- Merchant complaint or regulator inquiry: litigation hold + counsel.
- AEGIS revenue crosses $25M / 100K CA merchants: full CCPA compliance build-out.

---

## Verification checklist (one-time)

Before AEGIS funds first deal in production, operator must verify:

- [ ] All state-statute URLs in dossiers 01-05 (~45 minutes total).
- [ ] IL Loan Brokers Act applicability (one phone call to IL Sec of State Securities Dept).
- [ ] FTC Safeguards Rule applicability (attorney consult, ~30 minutes).
- [ ] Funder ISO contract obligations (review actual contracts AEGIS will sign).
- [ ] E&O insurance procured.
- [ ] Cybersecurity program documentation written and tested.
- [ ] OFAC screening cache populated and refresh job tested.
- [ ] Disclosure templates (CA, NY, FL, GA) match current regulatory text.
- [ ] Marketing copy reviewed for UDAP risk.
- [ ] AML/CIP procedures documented and tested with synthetic deal.

---

## When to consult an attorney

The dossiers in this folder are paste-ready for AEGIS architecture decisions. **They are not a substitute for legal advice.** Specific scenarios where operator must engage commercial finance counsel:

1. **Before funding first deal in any new state** — verify disclosure template against attorney review.
2. **Before signing a funder ISO agreement** — review for unusual cascade obligations.
3. **MCA reclassification claim or threatened lawsuit** — see `12_mca_vs_loan_reclassification.md`.
4. **Cybersecurity incident** — invoke counsel immediately, ideally before notifying anyone.
5. **Regulator inquiry from state AG, NY DFS, CFPB, or any other authority** — counsel directs response.
6. **Subpoena or preservation letter** — litigation hold + counsel.
7. **CCPA threshold approached** — full privacy program build-out.
8. **Pivot in business model** (direct funding, money transmission, consumer products) — full BSA/regulatory reassessment.

Estimated baseline attorney spend for AEGIS at scale: $5,000-$15,000/year for compliance review and ad-hoc questions, plus event-driven engagements for incidents.

---

## Things this document does NOT cover (because they're out of scope)

- **State licensing for direct lending.** AEGIS is a broker, not a lender. If AEGIS pivots to direct funding, state lender licensing is required in most states.
- **Money transmitter licensing.** AEGIS is not an MTL holder. If AEGIS handles merchant funds in escrow, this changes.
- **Specific funder underwriting requirements.** Each funder has its own internal underwriting that may differ from AEGIS's scoring. AEGIS's job is to score and match, not duplicate funder underwriting.
- **Tax compliance.** Operator's accountant.
- **Employment law.** Not a compliance posture for an AEGIS-arranged transaction.
- **Specific state usury rates.** These vary by state and apply only if reclassification occurs. AEGIS's defense is structural (don't get reclassified), not numerical (target a specific APR).

---

## Files in this compliance package

1. `00_index.md` — top-5 state index/comparison.
2. `01_california.md` — CA full dossier.
3. `02_new_york.md` — NY full dossier.
4. `03_florida.md` — FL full dossier.
5. `04_georgia.md` — GA full dossier.
6. `05_illinois.md` — IL full dossier (Tier 2 with pending legislation).
7. `06_section_1071_federal.md` — federal Section 1071 (MCA exempt).
8. `07_ofac_sanctions.md` — OFAC sanctions screening and reporting.
9. `08_aml_bsa_kyc.md` — AML/BSA/KYC posture.
10. `09_cfpb_udaap.md` — UDAAP/UDAP marketing and sales conduct.
11. `10_record_retention.md` — master retention schedule.
12. `11_data_security_privacy.md` — NY DFS Part 500, CCPA, FTC Safeguards, breach notification.
13. `12_mca_vs_loan_reclassification.md` — Yellowstone-pattern defense.
14. `13_broker_specific_rules.md` — broker-only rule matrix.
15. `14_renewal_redisclosure.md` — renewals, double-dipping, re-disclosure.
16. `15_aegis_compliance_posture.md` — this master document.

---

## Confidence

This master document summarizes the cumulative confidence assessments from all 14 dossiers. Overall:

- **High confidence:** state disclosure laws (CA, NY, FL, GA), OFAC framework, federal § 1071 exemption, record retention norms, UDAAP scope.
- **Medium confidence:** specific implementations of cascading obligations (NY DFS Part 500 third-party cascade, FTC Safeguards Rule applicability to AEGIS), AML/BSA boundaries.
- **Action required:** IL Loan Brokers Act applicability requires phone verification; one attorney consult recommended for MCA reclassification defense architecture.

After verification of source URLs (~45 minutes) and the IL phone call (~15 minutes), this compliance package is ready for AEGIS architectural integration. Estimated total operator action time: ~60 minutes one-time + recurring calendar items.
