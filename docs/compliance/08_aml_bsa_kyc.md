# AML / BSA / KYC — Complete AEGIS Compliance Dossier

**Researched: 2026-05-07** by Claude (web-search based) for operator verification.
**Status: AEGIS as a brokerage-only entity is NOT directly subject to BSA AML program rule. Funder cascade and best-practice KYC still apply.**

---

## TL;DR for AEGIS

- **The Bank Secrecy Act (BSA)** requires "financial institutions" defined under 31 USC § 5312 to maintain AML programs, file Suspicious Activity Reports (SARs), file Currency Transaction Reports (CTRs), and conduct customer identification (CIP).
- **MCAs are NOT covered.** MCA brokers are NOT explicitly listed among BSA-defined financial institutions. The list (31 USC § 5312(a)(2)) includes banks, broker-dealers in securities, MSBs, casinos, futures merchants, insurance companies, dealers in precious metals, etc. **MCA brokering does not fit any defined category.**
- **AEGIS is NOT an MSB** under FinCEN rules (31 CFR § 1010.100(ff)). MSBs are defined narrowly: money transmitters, currency dealers/exchangers, check cashers, money order issuers, traveler's checks, prepaid access. AEGIS does not perform any of these functions.
- **However, AML obligations cascade to AEGIS through funder contracts.** Funders (especially those owned by or partnered with banks) are required to apply BSA-equivalent diligence to their broker channel. Most funder ISO agreements require AEGIS to: implement KYC procedures, provide merchant identification documents, screen against OFAC, retain records, and cooperate with funder AML investigations.
- **NY DFS additionally requires** any DFS-licensed entity (which includes any funder licensed to operate in NY) to maintain a written AML program (3 NYCRR Part 504). Funders cascade obligations to brokers contractually.
- **Practical posture:** AEGIS implements KYC/CIP, OFAC screening, and SAR-equivalent internal escalation as if BSA applied — even though it doesn't directly. This is industry standard and protects against funder contract violations.

---

## Why AEGIS is not directly a BSA-covered financial institution

### Statutory definition (31 USC § 5312(a)(2))
The BSA defines "financial institution" exhaustively as:
- (A) Insured banks
- (B) Commercial banks or trust companies
- (C) Private bankers
- (D) Foreign banks doing business in the U.S.
- (E) Credit unions
- (F) Thrifts
- (G) Brokers or dealers registered under Securities Exchange Act
- (H) Brokers or dealers in securities or commodities
- (I) Investment bankers / investment companies
- (J) Currency exchanges
- (K) Money transmitters
- (L) Issuers/redeemers/cashiers of money orders/traveler's checks
- (M) Operators of credit card systems
- (N) Insurance companies
- (O) Dealers in precious metals/stones/jewels
- (P) Pawnbrokers
- (Q) Loan or finance companies
- (R) Travel agencies
- (S) Money services businesses (MSBs)
- (T) Telegraph companies
- (U)–(Y) Various enumerated other categories
- (Z) Catch-all: any business designated by FinCEN as having "high degree of usefulness in criminal, tax, or regulatory matters"

**AEGIS is not (Q) "loan or finance company"** because:
- A finance company under FinCEN's interpretive history is one that originates or purchases loans for its own account.
- AEGIS does not extend credit, originate loans, or purchase receivables. AEGIS is a transaction facilitator/broker that earns commission from the funder.

**AEGIS is not (S) MSB** per FinCEN's definition at 31 CFR § 1010.100(ff). MSB activities are: dealing in or exchanging currency, transmitting money, issuing/cashing money orders or traveler's checks, providing prepaid access. None of these describe what AEGIS does.

### FinCEN's posture on commercial finance brokers
FinCEN has not issued specific BSA guidance for MCA or commercial finance brokers. The closest analogues are:
- **2014 Administrative Ruling FIN-2014-R009** on payment processors and ISOs — found that an ISO is not an MSB if it operates within four conditions (formal agreement, BSA-regulated clearing system, etc.). This is about payment processors, not credit arrangers, but suggests FinCEN does not aggressively expand MSB categorization.
- **2012 Guidance FIN-2012-R005** on loan/finance company subsidiaries — addresses *subsidiaries* of regulated entities, not standalone brokers.

**Bottom line:** AEGIS as a standalone brokerage is not directly regulated under BSA. **However, this could change.** FinCEN periodically considers expansions, and a future rulemaking could bring commercial finance brokers into scope.

---

## Why AEGIS still implements AML controls

Three independent reasons:

### 1. Funder contractual cascade
Most ISO agreements with established funders contain language like:
> "ISO/Broker shall implement a written customer identification program (CIP) consistent with 31 CFR Part 1020.220 standards. ISO/Broker shall conduct OFAC screening on all merchants and principals prior to submission. ISO/Broker shall retain all merchant identification records for at least 5 years and produce them upon request."

AEGIS may not be BSA-covered directly, but if its funder is, those obligations cascade contractually.

### 2. NY DFS Part 504 cascade
3 NYCRR Part 504 ("Banking Division Transaction Monitoring and Filtering Program Requirements and Certifications") requires DFS-regulated institutions to maintain transaction monitoring and OFAC filtering systems. DFS-licensed funders flow these requirements to brokers via ISO agreements.

### 3. Reputational and litigation risk
The Yellowstone Capital judgment (NY AG, 2025, $1.065B) was not based on BSA violations — but the NY AG cited Yellowstone's lack of consistent KYC, lack of identity verification, and pattern of funding shell entities as evidence of overall bad-faith conduct. **Even without BSA exposure, weak KYC is evidence of broader compliance failure.**

---

## What AEGIS implements (operational)

### Customer Identification Program (CIP) — modeled on 31 CFR § 1020.220
At deal intake, before any funder submission, AEGIS collects and verifies:
1. **Business legal name** (verified against state business registry — Sunbiz for FL, NY DOS for NY, etc.).
2. **Business EIN** (verified against IRS letter 147C or W-9).
3. **Business physical address** (verified against utility bill, lease, bank statement).
4. **Each principal's name, DOB, SSN/ITIN, address** (verified against driver's license, passport, or government-issued ID + utility bill or bank statement).
5. **Merchant bank statements** (3 most recent, used for underwriting AND identity verification).

Documents stored in `merchant_identity_verification` table with hashes and retention timestamps.

### Transaction Monitoring (light)
AEGIS performs basic monitoring for red flags:
- Multiple merchants sharing same address or principal across short time windows.
- Merchants with name patterns matching known shell-entity templates.
- Inconsistencies between business registration date and revenue (e.g., 2-month-old LLC reporting $500K/month).
- Rapid-fire applications across multiple funders for the same merchant (broker stacking pattern).

These flags do NOT trigger SAR filing (AEGIS is not BSA-required to file). They DO trigger internal escalation and may result in declining the deal or notifying the funder.

### Internal Suspicious Activity Escalation
AEGIS maintains an internal `suspicious_activity_log`:
- Date, deal ID, type of red flag, operator notes, disposition (declined / submitted with notation / cleared).
- This log is NOT filed with FinCEN by AEGIS — but if a funder asks, AEGIS produces it.
- Retention: 5 years.

### OFAC screening
See `07_ofac_sanctions.md` — already required and built.

### Records retention
- Merchant CIP records: 5 years from end of relationship (mirroring 31 CFR § 1020.220).
- Bank statements: 5 years from deal funding date.
- Internal suspicious activity log: 5 years.

---

## What AEGIS does NOT do (and shouldn't claim to do)

- **AEGIS does NOT file SARs with FinCEN.** AEGIS is not authorized as a SAR filer because it's not BSA-covered. Don't claim to file SARs in marketing materials — this is misleading.
- **AEGIS does NOT file CTRs.** AEGIS does not transact in currency above $10K cash thresholds.
- **AEGIS does NOT register with FinCEN as an MSB.** AEGIS is not an MSB. Registering would be incorrect.

If AEGIS detects clear evidence of money laundering, the appropriate response is: (a) decline the deal, (b) notify the funder so the funder can SAR-file if they're BSA-covered, (c) document the decision in `suspicious_activity_log`.

---

## What changes if AEGIS pivots

If AEGIS evolves into:
- **Direct funder** (originating MCAs from balance sheet): AEGIS likely becomes a "loan or finance company" → may need MSB-equivalent compliance plus state lender licensing. **Major BSA reassessment required.**
- **Money transmitter** (handling merchant funds in escrow): AEGIS becomes MSB → state-by-state MTL licensing, FinCEN registration, full AML program required.
- **Cryptocurrency-touching activities**: triggers significant FinCEN obligations.

For now (broker-only, MCA-only): NOT BSA-covered, but ISO contracts apply.

---

## Source URLs

1. **31 USC § 5312(a)(2) (financial institution definition)** — https://www.law.cornell.edu/uscode/text/31/5312
2. **31 CFR § 1010.100 (FinCEN definitions, including MSB)** — https://www.ecfr.gov/current/title-31/subtitle-B/chapter-X/part-1010/subpart-A/section-1010.100
3. **31 CFR Part 1022 (MSB rules)** — https://www.ecfr.gov/current/title-31/subtitle-B/chapter-X/part-1022
4. **FinCEN "Am I an MSB?"** — https://www.fincen.gov/am-i-msb
5. **FinCEN MSB Registration page** — https://www.fincen.gov/money-services-business-msb-registration
6. **2014 ISO/payment processor ruling FIN-2014-R009** — https://www.fincen.gov/resources/statutes-regulations/administrative-rulings/application-money-services-business
7. **3 NYCRR Part 504 (NY DFS transaction monitoring)** — https://www.dfs.ny.gov/legal/regulations/adoptions/dfsp504t.pdf

---

## Confidence

| Finding | Confidence |
|---|---|
| AEGIS not directly BSA-covered as broker-only | High — facially based on statutory text and FinCEN guidance |
| AEGIS not MSB | High — does not perform MSB activities under § 1010.100(ff) |
| ISO contractual AML cascade is industry standard | High — well-documented in funder agreements |
| 5-year retention is the operative norm | High — 31 CFR Part 1010 and Part 1020 |
| If business model changes, BSA reassessment required | High — change in covered activities triggers re-analysis |
| 3 NYCRR Part 504 cascade exists for DFS-licensed funders | Medium — confirmed exists; specific cascade language varies by funder agreement |

**This dossier should be reviewed by an attorney before AEGIS makes any public claim about its AML program scope.** The boundaries of "financial institution" and "loan or finance company" under FinCEN's expansive interpretive authority are not always intuitive, and one phone call to a commercial finance attorney could confirm the analysis.
