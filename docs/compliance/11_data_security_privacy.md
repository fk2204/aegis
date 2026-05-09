# Data Security & Privacy — Complete AEGIS Compliance Dossier

**Researched: 2026-05-07** by Claude (web-search based) for operator verification.
**Status: AEGIS not directly NY DFS Part 500 covered. Third-party service provider obligations apply via funder cascade. CCPA / state breach notification laws apply directly.**

---

## TL;DR for AEGIS

- **NY DFS 23 NYCRR Part 500 ("Cybersecurity Regulation"):** AEGIS is NOT directly a covered entity (covered entities are NY Banking/Insurance/Financial Services Law licensees). However, AEGIS is a **third-party service provider** to funders that ARE covered entities. Funders contractually cascade the standards.
- **California Consumer Privacy Act (CCPA) / CPRA:** Threshold is **$26.625 million annual revenue** (2025-2026, adjusted annually for inflation). The B2B exemption **sunset January 1, 2023** — business contact information IS now in CCPA scope. **However, GLBA-covered data is exempt at the data level** (Cal. Civ. Code § 1798.145(e)). Since AEGIS qualifies as a financial institution under GLBA (see FTC Safeguards below), the personal information AEGIS collects in connection with the financial transaction is GLBA-exempt from CCPA, even at scale. **Currently exempt** at AEGIS's scale; track quarterly as data-handling activities outside the financial-services scope grow.
- **State breach notification laws:** apply regardless of size. AEGIS must notify affected merchants and authorities within prescribed timelines if breach occurs.
- **FTC Safeguards Rule (16 CFR Part 314): APPLIES to AEGIS — high confidence.** The 2021 amendment explicitly added "finder" activities to the financial institution definition (12 CFR § 225.86(d)(1)). AEGIS as a broker bringing together merchants and funders is a "finder" by this definition. AEGIS qualifies for the partial exemption (under 5,000 consumers) which simplifies but does not eliminate compliance.

---

## NY DFS Part 500 (23 NYCRR Part 500) — third-party cascade

### Why AEGIS is not directly covered
Part 500 applies to "covered entities" defined as any person operating under or required to operate under a license, registration, charter, certificate, permit, or similar authorization under NY Banking Law, Insurance Law, or Financial Services Law. AEGIS as a commercial finance broker does not hold any such license.

### Why AEGIS still implements the controls
Funders that ARE covered entities (most NY-licensed MCA funders, banks, etc.) cascade Part 500 third-party service provider obligations to AEGIS via the Section 500.11 ("Third Party Service Provider Security Policy") cascade:

> "Each Covered Entity shall implement written policies and procedures designed to ensure the security of Information Systems and Nonpublic Information that are accessible to, or held by, Third Party Service Providers."

In practice this means funder contracts with AEGIS will include clauses like:
- AEGIS must maintain a written information security program.
- AEGIS must use multi-factor authentication for access to Nonpublic Information.
- AEGIS must encrypt Nonpublic Information in transit and at rest.
- AEGIS must notify funder of cybersecurity incidents within 24-48 hours.
- AEGIS must provide annual cybersecurity attestation.
- AEGIS must permit funder audits or accept SOC 2 / equivalent reports.

### What AEGIS implements regardless
Even without funder cascade, AEGIS treats the Part 500 framework as the operating standard because:
1. NY DFS is the most aggressive state regulator on cybersecurity in financial services.
2. The framework aligns with NIST CSF, which is broadly applicable.
3. Future expansions of regulatory scope are likely to use Part 500 as a model.

### AEGIS Part 500-aligned controls

**§ 500.2 — Cybersecurity Program**
- Written cybersecurity program documented in `/docs/security/cybersecurity-program.md`.
- Reviewed annually.

**§ 500.3 — Cybersecurity Policy**
- Acceptable use policy.
- Access control policy.
- Data classification policy (PII vs PHI vs payment data vs business confidential vs public).
- Incident response policy.
- Vendor management policy.
- Encryption policy.

**§ 500.4 — CISO**
- Operator designated as Chief Information Security Officer.
- Annual report on cybersecurity posture.

**§ 500.5 — Vulnerability Assessments**
- Quarterly vulnerability scans on AEGIS production environment.
- Annual penetration test (or attestation if no external pentest).

**§ 500.6 — Audit Trail**
- All authentication attempts logged.
- All access to Nonpublic Information logged with user ID, timestamp, action.
- Logs retained 5 years (overlaps with retention from `10_record_retention.md`).

**§ 500.7 — Access Privileges**
- Principle of least privilege.
- Privileged accounts (root, admin, database master) used only when necessary, with logging.
- Service accounts have prohibited interactive login.

**§ 500.8 — Application Security**
- Secure SDLC: code review, dependency vulnerability scanning, no secrets in code.
- mypy strict, ruff strict — already in AEGIS plan.

**§ 500.9 — Risk Assessment**
- Annual risk assessment.

**§ 500.10 — Cybersecurity Personnel**
- Operator + (eventually) team trained in cybersecurity. Cite training records.

**§ 500.11 — Third Party Service Providers**
- Cloud providers (Hetzner, Cloudflare, Anthropic Bedrock) assessed for security posture.
- Annual review of third-party security attestations.

**§ 500.12 — Multi-Factor Authentication**
- MFA required for all administrative access to AEGIS.
- MFA required for remote access from outside Cloudflare Tunnel.
- MFA required for all privileged accounts.

**§ 500.13 — Asset Inventory**
- Written asset inventory of all systems, applications, dependencies.
- Updated quarterly.

**§ 500.14 — Cybersecurity Awareness Training**
- Operator + team training annually.

**§ 500.15 — Encryption**
- TLS 1.2+ for data in transit (HTTPS required, enforced by Cloudflare Tunnel).
- AES-256 (or equivalent) for data at rest in Supabase.
- Key management via Supabase + secrets in `/etc/aegis/aegis.env` (not in code).

**§ 500.16 — Incident Response Plan**
- Written IR plan covering detection, containment, eradication, recovery, notification.
- Tested annually (tabletop or simulation).

**§ 500.17 — Notice of Cybersecurity Event**
- Notify funders within 72 hours of confirmed cybersecurity incident affecting funder data.
- Notify affected merchants per state breach notification law (see below).
- Notify NY DFS within 72 hours **if AEGIS is contractually required by funder cascade** (operator should clarify in funder agreements).

**§ 500.18 — Confidentiality**
- All Nonpublic Information treated as confidential.
- No unnecessary disclosure of merchant data.

---

## CCPA / CPRA (California Consumer Privacy Act / California Privacy Rights Act)

### When CCPA applies to AEGIS
CCPA defines a "business" as one that:
- (i) Has annual gross revenue > **$26.625 million** (2025-2026, adjusted annually); OR
- (ii) Buys/sells/shares personal info of ≥ 100,000 California consumers/households; OR
- (iii) Derives ≥ 50% of revenue from selling/sharing personal info.

**At ~100 deals/month and ~$1M/year revenue, AEGIS does NOT meet any threshold.** CCPA does not directly apply.

### Critical update: B2B exemption sunset January 1, 2023
The B2B partial exemption (which previously excluded business-contact information) **expired January 1, 2023**. Personal information of business owners, principals, and other natural-person business contacts IS now in CCPA scope. The CCPA "consumer" definition is "any natural person who is a California resident" — including business owners and principals.

### GLBA data-level exemption (the key carve-out for AEGIS)
**Cal. Civ. Code § 1798.145(e)** exempts from CCPA scope: personal information collected, processed, sold, or disclosed pursuant to GLBA. Since AEGIS qualifies as a financial institution under GLBA (see FTC Safeguards Rule section below), the personal information AEGIS handles for KYC/CIP/underwriting/principal-ID-verification is exempt at the data level.

**Practical implication:** even if AEGIS exceeds a CCPA threshold (revenue or volume), the financial-services data is GLBA-exempt. CCPA only reaches data AEGIS collects outside the financial-services scope (e.g., website analytics from non-applicant visitors, marketing tracking, employee data if AEGIS hires CA employees).

### When CCPA does apply
- AEGIS principals' (employees', consultants') personal information IS in scope (CPRA expanded employee data to be covered).
- If AEGIS hires CA employees: CCPA employee provisions apply.
- If AEGIS expands into consumer-facing products (e.g., consumer credit, BNPL): full CCPA applies. The GLBA exemption only covers the data-types covered by GLBA.
- Marketing data (CA visitor analytics, ad-tracking pixels) is NOT GLBA-covered and counts toward CCPA scope.

### Threshold tracking
Add quarterly review: monitor AEGIS revenue and CA-non-financial-services-touchpoint count. When approaching $26.625M revenue or 100,000 CA non-GLBA touchpoints, full CCPA program required for the non-GLBA scope (privacy notice, right-to-delete, right-to-correct, right-to-know, opt-out of sale workflows).

### Right-to-delete from a merchant principal
A CA-based principal exercising right-to-delete:
- For data covered by GLBA (KYC docs, underwriting bank statements, etc.): GLBA-exempt — AEGIS responds explaining the exemption and the legal-compliance retention basis.
- For data outside GLBA scope (marketing emails from before they became an applicant, website tracking): CCPA applies — AEGIS responds within 45 days.

---

## State breach notification laws

If AEGIS suffers a data breach exposing merchant or principal PII:

### Notification timing by state
| State | Statute | Notification timing | Authorities to notify |
|---|---|---|---|
| CA | Cal. Civ. Code § 1798.82 | "Most expedient time possible and without unreasonable delay" | Affected residents; AG if > 500 residents |
| NY | NY Gen Bus Law § 899-aa | "Most expedient time possible" | Affected residents; NY AG; State Police; Dept of State |
| FL | Fla. Stat. § 501.171 | 30 days | Affected residents; FL AG if > 500 affected |
| GA | OCGA § 10-1-912 | "Most expedient time possible" | Affected residents |
| IL | 815 ILCS 530/10 | "Most expedient time possible" | Affected residents; AG if > 500 affected |

### What constitutes "breach"
Generally: unauthorized access to or acquisition of unencrypted PII. Encrypted data breaches are typically NOT reportable if encryption keys remain protected.

### What PII triggers notification
Most state laws require notification when "personal information" is breached. Definitions vary but typically include:
- Name + SSN
- Name + driver's license number
- Name + financial account number (with security code)
- Name + medical information
- Name + biometric identifier

### AEGIS data breach response plan (essential elements)
1. **Contain.** Stop the breach. Disable affected accounts/systems.
2. **Investigate.** Determine scope: what data, which merchants, when.
3. **Document.** Timeline, evidence, decisions.
4. **Engage counsel.** Immediately. Privilege depends on counsel-led investigation.
5. **Notify funders** per cascade contracts (often 24-48h).
6. **Notify affected merchants** per state laws.
7. **Notify regulators** per state laws.
8. **Credit monitoring.** Many states require offered to affected residents.
9. **Public statement** if widespread.
10. **Post-incident review** and remediation.

---

## FTC Safeguards Rule (16 CFR Part 314) — APPLIES to AEGIS

### Applicability — verified high confidence
The FTC Safeguards Rule applies to "financial institutions" not regulated by federal banking agencies. The 2021 amendment to the Rule **explicitly added "finder" activities** to the financial institution definition. Per 12 CFR § 225.86(d)(1) (incorporated into the Safeguards Rule):

> Finder means bringing together one or more buyers and sellers of any product or service for transactions that the parties themselves negotiate and consummate.

**This is exactly what AEGIS does as a broker.** AEGIS brings together merchants and funders for transactions that the parties themselves negotiate and consummate. AEGIS is a finder under the FTC's definition. Real estate brokers were specifically carved out; commercial finance brokers were not.

The 16 CFR § 314.2(h) list of explicitly-named financial institutions includes:
- Mortgage lenders, mortgage brokers
- Payday lenders
- **Finance companies** (commercial finance broker likely qualifies)
- Account servicers, check cashers, wire transferors
- Collection agencies
- Credit counselors / financial advisors
- Tax preparation firms
- Non-federally insured credit unions
- Investment advisors not required to register with SEC
- **Finders (added 2021)** — explicit AEGIS fit

### What "customer information" means for AEGIS
GLBA "customer information" = nonpublic personal information about a customer. "Customer" = a consumer (natural person who obtained or applied for a financial product or service primarily for personal/family/household purposes).

For AEGIS:
- The merchant entity itself (LLC, corp) is NOT a customer. Business EIN, business bank account numbers, etc. are NOT customer information.
- The merchant principal's personal information (SSN, DOB, driver's license, personal address, personal financial information used for personal guarantees) IS customer information when AEGIS collects it as part of arranging the financial transaction.

So the rule's reach is partial: it covers the principal's personal data, not the entity-level data.

### Partial exemption: under 5,000 consumers (§ 314.6)
Financial institutions that "maintain customer information concerning fewer than five thousand consumers" qualify for a **partial exemption** from certain provisions:
- Risk assessment requirements simplified (no mandatory written assessment).
- Continuous monitoring or annual penetration testing simplified.
- Written incident response plan simplified.
- Annual report from qualified individual simplified.

AEGIS at ~100 deals/month with maybe 200 principals/year is well under 5,000. **AEGIS qualifies for the partial exemption** — but still must comply with the core safeguards.

### Requirements (effective June 9, 2023, expanded version)
Even with partial exemption, AEGIS must:
- Designate a **qualified individual** (operator initially serves as CISO).
- Implement an **information security program** (written; sized to AEGIS's complexity).
- **Access controls** including MFA for systems containing customer information.
- **Inventory** of customer information and disposal procedures.
- **Encryption** of customer information in transit and at rest (or compensating controls if encryption infeasible, with CISO sign-off).
- **Service provider oversight** (Hetzner, Cloudflare, Anthropic Bedrock, Supabase).
- **Incident response plan** (can be simplified under partial exemption).
- **Annual training** for personnel handling customer information.
- **Reporting** of cybersecurity events to FTC within **30 days** if affecting 500+ consumers (16 CFR § 314.4(j), effective May 13, 2024). **Below 500: no FTC notification required, but state breach notification laws still apply.**

These requirements substantially overlap with NY DFS Part 500. **AEGIS implementation strategy:** build to Part 500 + FTC Safeguards combined; the union covers both.

---

## What AEGIS records

```python
class CybersecurityProgram:
    cybersecurity_program_doc_path: str = "/docs/security/cybersecurity-program.md"
    last_reviewed: date
    ciso_designated: str  # operator name initially
    risk_assessment_last_completed: date
    pentest_last_completed: date | None
    vuln_scan_schedule: str = "quarterly"
    annual_training_completed_date: date | None

class CybersecurityIncident:
    id: UUID
    detected_at: datetime
    contained_at: datetime | None
    eradicated_at: datetime | None
    recovered_at: datetime | None
    affected_data_types: list[str]
    affected_merchant_ids: list[UUID]
    investigation_summary: str
    notifications_required: list[str]  # ["NY_AG", "CA_AG", "merchants", "funder_X"]
    notifications_sent: dict[str, datetime]
    counsel_engaged_at: datetime | None
    closed_at: datetime | None
    retention_until: date  # closed_at + 5 years
```

---

## Source URLs

1. **23 NYCRR Part 500 (NY DFS Cybersecurity)** — https://www.dfs.ny.gov/industry_guidance/cybersecurity
2. **NY DFS Part 500 FAQ** — https://www.dfs.ny.gov/industry_guidance/cybersecurity_faqs
3. **Cal. Civ. Code § 1798.100 et seq. (CCPA/CPRA)** — https://leginfo.legislature.ca.gov/faces/codes_displayText.xhtml?division=3.&part=4.&lawCode=CIV&title=1.81.5
4. **CCPA Threshold ($25M etc.)** — Cal. Civ. Code § 1798.140(d)
5. **Cal. Civ. Code § 1798.82 (CA breach)** — https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=CIV&sectionNum=1798.82
6. **NY Gen Bus Law § 899-aa (NY breach)** — https://www.nysenate.gov/legislation/laws/GBS/899-AA
7. **Fla. Stat. § 501.171 (FL breach)** — https://www.flsenate.gov/Laws/Statutes/2024/501.171
8. **16 CFR Part 314 (FTC Safeguards Rule)** — https://www.ecfr.gov/current/title-16/chapter-I/subchapter-C/part-314

---

## Confidence

| Finding | Confidence |
|---|---|
| AEGIS not direct DFS Part 500 covered entity | High — facially based on covered-entity definition |
| Funder cascade via § 500.11 | High — well-documented industry practice |
| AEGIS below CCPA thresholds at current scale | High — clear from statutory thresholds |
| CCPA threshold $26.625M (2025-2026 adjusted) | High — verified during verification pass |
| B2B exemption sunset 2023-01-01 | High — verified during verification pass |
| GLBA data-level exemption from CCPA | High — Cal. Civ. Code § 1798.145(e) |
| State breach notification laws apply directly | High — state-by-state statutes |
| FTC Safeguards Rule applies (finder activity) | High — verified during verification pass; explicit 2021 addition of finder activities |
| AEGIS qualifies for under-5000-consumer partial exemption | High — § 314.6 |
| 72-hour DFS incident notification (via cascade) | High — § 500.17 |
| 30-day FTC notification (500+ consumers) | High — 16 CFR § 314.4(j) effective 2024-05-13 |
