# OFAC Sanctions — Complete AEGIS Compliance Dossier

**Researched: 2026-05-07** by Claude (web-search based) for operator verification.
**Status: required for every funded deal. Already partially built in Phase 3 — this dossier completes the picture.**

---

## TL;DR for AEGIS

- **OFAC** (Office of Foreign Assets Control, U.S. Treasury) administers sanctions against individuals, entities, governments, and territories. Compliance is **strict liability** — there is no "safe harbor" for not knowing.
- **Key list:** OFAC Specially Designated Nationals (SDN) List + consolidated non-SDN sanctions lists (NS-PLC, FSE-IR, etc.).
- **Operational rule:** AEGIS must screen every merchant principal AND business name against SDN before funding. Match = hard decline + freeze + report.
- **Reporting:** Blocked transactions must be reported to OFAC within **10 business days**. Annual aggregate report (Form TD F 90-22.50) by **September 30** for the prior calendar year.
- **Recordkeeping:** **5 years** for all blocked-property reports, suspect-activity records, and screening logs (31 CFR § 501.601).
- **Penalties:** Up to **$368,136 per violation** civil; criminal penalties up to **$1M and 20 years** for willful violations (IEEPA, 50 USC § 1705 — annually adjusted).
- **AEGIS already implements:** SDN.XML fetcher, fail-closed cache (>7 days = block all funding), hard-decline rule on match. This dossier adds: reporting workflow, near-match handling, false-positive resolution, retention.

---

## What AEGIS must screen

For every deal, before submission to a funder:

1. **Business legal name** (and DBA) against SDN.
2. **Each principal's name + DOB** if available (DOB helps disambiguate common names).
3. **Business address** — flag if in or near OFAC-sanctioned countries (Iran, North Korea, Cuba, Crimea/DNR/LNR, etc.) — these would be screened by ZIP/country code.
4. **Bank account info** — bank routing numbers tied to sanctioned institutions (rare but possible).

For comprehensive screening, AEGIS should also consult:
- **FinCEN 314(a) Information Sharing list** (subpoenaed names from law enforcement) — different list, different mechanics; not strictly required for non-bank brokers but often expected by funder partners.
- **PEP (Politically Exposed Persons)** lists — not OFAC-required for U.S. domestic merchants but valuable for AML risk scoring.

---

## Screening workflow (AEGIS implementation)

```python
class OFACScreening:
    def screen_deal(self, deal: Deal) -> ScreeningResult:
        # 1. Refresh SDN cache if needed (target: hourly; max staleness: 7 days)
        if self.sdn_cache.age_days > 7:
            return ScreeningResult.fail_closed("sdn_cache_stale")

        # 2. Screen business name (exact + fuzzy)
        biz_matches = self.sdn_cache.search(
            deal.merchant.legal_name,
            fuzzy_threshold=0.85,
        )

        # 3. Screen each principal
        principal_matches = []
        for p in deal.principals:
            principal_matches.extend(
                self.sdn_cache.search(
                    f"{p.first_name} {p.last_name}",
                    dob=p.dob,  # if available
                    fuzzy_threshold=0.85,
                )
            )

        # 4. Decision tree
        if any(m.score == 1.0 for m in biz_matches + principal_matches):
            return ScreeningResult.exact_match_block(...)
        if any(m.score >= 0.95 for m in biz_matches + principal_matches):
            return ScreeningResult.high_confidence_match_block(...)  # operator review
        if any(m.score >= 0.85 for m in biz_matches + principal_matches):
            return ScreeningResult.possible_match_review(...)  # operator review
        return ScreeningResult.cleared(...)
```

### Match thresholds (industry convention)
- **1.00 (exact):** automatic hard decline. Log to `ofac_blocked_log`. Trigger 10-day reporting workflow.
- **≥ 0.95 (high confidence):** automatic hard decline. Operator must review and disposition within 24h.
- **≥ 0.85 (possible match):** soft decline pending operator review. Common with names like "Smith" or "Lopez" — operator confirms via DOB, address, country.
- **< 0.85:** cleared, log result for audit.

---

## When AEGIS gets a hit: the reporting workflow

### Step 1: Block immediately
- Funding cannot proceed. Funds (if any are held in escrow) must be frozen — but in practice AEGIS as a broker is screening pre-funding, so "blocking" = decline.
- Notify the merchant: AEGIS may NOT tell the merchant they were OFAC-flagged ("tipping off" is itself prohibited under some circumstances). Use a generic decline reason: "We are unable to process your application at this time."

### Step 2: Report to OFAC (10 business days)
- File **Initial Report of Blocked Property** within 10 business days using OFAC's electronic reporting system (Reporting and License Application Forms).
- Required information: name and address of blocked party, date of attempted transaction, dollar amount, description, identifying information (DOB, government ID if known).

### Step 3: Annual aggregate report
- File **Annual Report of Blocked Property** (Form TD F 90-22.50) by September 30 each year, covering the prior calendar year.
- Must report all property still blocked as of June 30.

### Step 4: Retention
- Maintain all records of blocked transactions for **5 years from the date the property was unblocked** (31 CFR § 501.601(b)).
- For declined applications (where AEGIS never accepted property), retain screening result records for 5 years.

---

## Near-match (false positive) resolution

The hardest operational case is a near-match where the merchant is innocent but their name resembles an SDN entry. Operator workflow:

1. **Compare DOBs** if SDN entry has one and merchant DOB is known. Mismatch = high confidence false positive.
2. **Compare addresses.** If SDN entry shows Tehran and merchant is in Brooklyn, low risk.
3. **Compare country/nationality.** SDN entries typically include nationality.
4. **Look at SDN program codes.** If SDN entry is on `[NPWMD]` (non-proliferation) and merchant is a Florida pizza shop, very different risk profile than `[SDGT]` (specially designated global terrorist).
5. **Document the decision.** Whether cleared or blocked, the operator's reasoning goes in the screening log.

If genuinely unsure: **OFAC has a hotline (1-800-540-6322) and a "Reporting and License Application Forms" submission system for License Applications.** When in doubt, license application or attorney consult is the right move.

---

## Critical rule: NO TIPPING OFF

Under various OFAC programs and parallel BSA SAR rules, AEGIS may not tell a merchant that they were flagged for OFAC reasons. The decline message must be generic ("unable to process at this time") and AEGIS may not share screening details with the merchant or any party other than OFAC, law enforcement, or the AEGIS attorney.

---

## What AEGIS records

```python
# Already exists in Phase 3
class OFACScreeningLog:
    deal_id: UUID
    screened_at: datetime
    sdn_cache_version: str  # e.g. "2026-05-07T03:00:00Z"
    screened_names: list[str]  # business + principals
    screened_dobs: list[date | None]
    match_count: int
    matches: list[OFACMatch]  # name, sdn_uid, score, programs
    disposition: Literal["cleared", "blocked_exact", "blocked_high", "review_pending", "false_positive_after_review"]
    operator_review: OperatorReview | None  # for review_pending → cleared/blocked
    reported_to_ofac_at: datetime | None  # if disposition was blocked
    ofac_initial_report_id: str | None
    annual_report_year: int | None
    retention_until: date  # screened_at + 5 years
```

---

## Source URLs

1. **OFAC SDN List downloads (XML, PDF, TXT)** — https://ofac.treasury.gov/sanctions-list-service
2. **31 CFR Chapter V (sanctions regulations)** — https://www.ecfr.gov/current/title-31/subtitle-B/chapter-V
3. **Reporting requirements (31 CFR § 501.603 / 501.604)** — https://www.ecfr.gov/current/title-31/subtitle-B/chapter-V/part-501
4. **Civil penalty schedule (annually updated)** — https://ofac.treasury.gov/civil-penalties-and-enforcement-information
5. **OFAC FAQ (general)** — https://ofac.treasury.gov/faqs

---

## Confidence

| Finding | Confidence |
|---|---|
| SDN screening required, strict liability | High — well-settled OFAC framework |
| 10-day reporting requirement | High — 31 CFR § 501.603 |
| 5-year retention | High — 31 CFR § 501.601 |
| Annual report Form TD F 90-22.50 due Sep 30 | High — OFAC published guidance |
| Penalty amounts | High — annually adjusted; current numbers from OFAC schedule |
| Near-match thresholds 0.85/0.95 | Medium — industry convention, not regulatory mandate; AEGIS may tune |
| No-tipping-off rule | High — across multiple OFAC programs |
