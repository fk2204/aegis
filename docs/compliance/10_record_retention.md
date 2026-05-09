# Record Retention — Complete AEGIS Compliance Dossier

**Researched: 2026-05-07** by Claude (web-search based) for operator verification.
**Status: master retention schedule. Use to populate `data_retention_policy` table at deploy.**

---

## TL;DR for AEGIS

There is no single retention period. AEGIS must retain different categories of records for different periods because each category is governed by a different rule. **The longest applicable retention period wins.**

For most categories, **5–7 years from the close of the transaction** is the operating norm. Some specific records (like marketing assets, OFAC blocked-property records, and litigation-hold material) may need to be kept longer.

---

## Master retention schedule

| Record category | Retention (years from event) | Triggering event | Source |
|---|---|---|---|
| State disclosure documents (CA, NY) | 4 | Disclosure transmission date | CA 10 CCR § 952; NY 23 NYCRR § 600.21 |
| State disclosure documents (FL, GA) | 4 (recommended) | Disclosure transmission date | No explicit statute; mirror CA/NY |
| Disclosure transmission proof (receipts, timestamps) | 4 | Transmission date | Same as above |
| Funder agreement (ISO/broker contract) | 7 | Contract end date | General contract statute of limitations + buffer |
| Merchant deal file (signed contract, decision rationale) | 7 | Deal funded or declined | Multi-state UDAP statutes of limitations |
| Bank statements collected for underwriting | 5 | Deal funded or declined | KYC/CIP cascade (31 CFR § 1020.220 norm) |
| Merchant identity documents (DL, EIN letter, etc.) | 5 | End of merchant relationship | KYC cascade |
| Loan application (if AEGIS-arranged → ECOA reach) | 25 months for adverse action records | Application date | 12 CFR § 1002.12 (ECOA Reg B) |
| OFAC screening logs (cleared deals) | 5 | Screening date | 31 CFR § 501.601 |
| OFAC blocked property records | 5 | Date property unblocked OR transaction declined | 31 CFR § 501.601(b) |
| OFAC license applications + responses | 5 | License action date | 31 CFR § 501.601 |
| Internal suspicious activity log | 5 | Entry date | Industry norm; no AEGIS direct BSA filing duty |
| Funder match log (which funders matched to which deal) | 7 | Deal date | Anti-discrimination defense |
| Marketing assets (ads, email templates, landing pages) | 7 | Asset retired date | UDAP statute of limitations + buffer |
| Sales scripts and call recordings | 7 (recordings as state law permits) | Recording date | UDAP defense + state recording laws |
| Merchant complaint log | 7 | Resolution date | UDAP / private suit window |
| Cybersecurity incident records | 5 | Incident closure date | 23 NYCRR § 500.17(c); state breach laws |
| Tax records | 7 | Tax return filed | IRC § 6501 |
| Employment records (W-2, payroll) | 4–7 | Termination | DOL / state |
| HR records | 4 | Termination | EEOC / Title VII |
| Litigation hold | Indefinite until released | Litigation initiated | Court order or attorney instruction |

---

## How AEGIS computes retention dates

```python
class DataRetentionPolicy:
    @classmethod
    def compute_retention_date(
        cls,
        record_type: str,
        event_date: date,
        litigation_hold: bool = False,
    ) -> date | None:
        """Return the date after which the record may be deleted, or None if held indefinitely."""

        if litigation_hold:
            return None  # held until released

        years = {
            "disclosure_document": 4,
            "disclosure_transmission_proof": 4,
            "funder_agreement": 7,
            "merchant_deal_file": 7,
            "bank_statement": 5,
            "merchant_identity_document": 5,
            "loan_application_adverse_action": 2,  # 25 months ≈ 2 years; actual: 25 months
            "ofac_screening_cleared": 5,
            "ofac_blocked_property": 5,
            "internal_suspicious_activity": 5,
            "funder_match_log": 7,
            "marketing_asset": 7,
            "sales_script": 7,
            "call_recording": 7,
            "merchant_complaint": 7,
            "cybersecurity_incident": 5,
            "tax_record": 7,
        }.get(record_type)

        if years is None:
            raise ValueError(f"Unknown record type: {record_type}")

        return event_date + timedelta(days=365 * years + 30)  # +30d buffer
```

### Buffer policy
Add 30 days to all computed retention periods. Reason: regulatory ambiguity around "from the date of" — better to over-retain by a month than under-retain by a day.

### Litigation hold override
Any record subject to litigation hold is retained indefinitely. AEGIS marks records with `litigation_hold=true` when (a) attorney issues a hold notice, (b) AEGIS receives a subpoena or preservation letter, (c) any deal is the subject of active dispute. Hold is released only on attorney instruction.

---

## Operational implementation

### Database design
```sql
CREATE TABLE data_retention_policy (
    record_type TEXT PRIMARY KEY,
    retention_years INT NOT NULL,
    trigger_description TEXT NOT NULL,
    statute_citation TEXT NOT NULL,
    statute_url TEXT,
    last_reviewed DATE NOT NULL
);

CREATE TABLE record_retention_metadata (
    record_id UUID PRIMARY KEY,
    record_type TEXT NOT NULL REFERENCES data_retention_policy(record_type),
    event_date DATE NOT NULL,
    retention_until DATE NOT NULL,
    litigation_hold BOOLEAN NOT NULL DEFAULT FALSE,
    litigation_hold_set_at TIMESTAMPTZ,
    litigation_hold_released_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ
);
```

### Daily reaper job
```python
async def reap_expired_records():
    """Daily job: identify records past retention with no litigation hold, queue deletion."""
    today = date.today()
    candidates = await db.fetch("""
        SELECT record_id, record_type
        FROM record_retention_metadata
        WHERE retention_until < $1
          AND litigation_hold = FALSE
          AND deleted_at IS NULL
        LIMIT 10000
    """, today)
    for c in candidates:
        await delete_record(c.record_id, c.record_type)
        await mark_deleted(c.record_id)
```

### Annual review
Once per year, operator reviews `data_retention_policy` table for:
- New regulations affecting retention periods.
- Changes to existing statutes.
- New record categories AEGIS now produces.
- Litigation holds that should be released.

### Personal data minimization
For records that contain merchant PII, retention period applies but ALSO:
- After 90 days post-funding, redact account numbers from bank statements where possible (keep deal totals and patterns; remove full account numbers).
- After 1 year, redact merchant SSN from non-essential records.
- After retention expires, secure deletion (cryptographic erasure for cloud storage; multi-pass overwrite for on-disk).

---

## CCPA / CPRA right-to-delete interaction

If a CA merchant exercises right to delete under CCPA:
- AEGIS may retain records under "legal compliance" exception (Cal. Civ. Code § 1798.105(d)(8)) for as long as required by law.
- AEGIS responds within 45 days with: "Your data is retained for legal compliance under [list of statutes]. After [retention end date] we will delete the records."
- See `11_data_security_privacy.md` for full CCPA workflow.

---

## State breach notification interaction

If AEGIS suffers a data breach involving merchant PII, retention rules conflict with breach notification rules. **Breach notification wins** — AEGIS must notify affected merchants regardless of whether the records would otherwise be deleted. Notification timelines:

| State | Timeline | Citation |
|---|---|---|
| CA | "in the most expedient time possible and without unreasonable delay" | Cal. Civ. Code § 1798.82 |
| NY | "in the most expedient time possible and without unreasonable delay"; consumers + AG + DFS if applicable | NY Gen Bus Law § 899-aa; 23 NYCRR § 500.17 |
| FL | 30 days | Fla. Stat. § 501.171 |
| GA | "in the most expedient time possible and without unreasonable delay" | OCGA § 10-1-912 |
| IL | "in the most expedient time possible and without unreasonable delay" | 815 ILCS 530/10 |

Plus federal: HHS for HIPAA, FTC for some categories. See `11_data_security_privacy.md`.

---

## What this enables

When a regulator (state AG, NY DFS, IRS, OFAC) sends AEGIS a records request:
1. Look up by record category in `record_retention_metadata`.
2. Confirm record is still within retention.
3. Produce on demand.

When AEGIS deletes records:
1. Verify retention period has elapsed.
2. Verify no litigation hold.
3. Cryptographic erasure or secure deletion.
4. Mark `deleted_at` in metadata table — but preserve the metadata record indefinitely (the metadata is small and shows compliance with retention policy).

---

## Source URLs

1. **CA 10 CCR § 952** — https://www.law.cornell.edu/regulations/california/10-CCR-952
2. **NY 23 NYCRR § 600.21** — https://www.law.cornell.edu/regulations/new-york/23-NYCRR-600.21
3. **31 CFR § 501.601 (OFAC retention)** — https://www.ecfr.gov/current/title-31/subtitle-B/chapter-V/part-501/subpart-G/section-501.601
4. **31 CFR § 1020.220 (CIP retention)** — https://www.ecfr.gov/current/title-31/subtitle-B/chapter-X/part-1020/subpart-B/section-1020.220
5. **12 CFR § 1002.12 (ECOA Reg B retention)** — https://www.ecfr.gov/current/title-12/chapter-X/part-1002/section-1002.12
6. **23 NYCRR § 500.17(c) (NY DFS cyber retention)** — https://www.dfs.ny.gov/industry_guidance/cybersecurity
7. **Cal. Civ. Code § 1798.82 (CA breach notification)** — https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=CIV&sectionNum=1798.82
8. **Fla. Stat. § 501.171 (FL breach notification)** — https://www.flsenate.gov/Laws/Statutes/2024/501.171

---

## Confidence

| Finding | Confidence |
|---|---|
| CA/NY 4-year disclosure retention | High — verbatim regulatory text |
| OFAC 5-year retention | High — 31 CFR § 501.601 |
| CIP 5-year retention norm | High — 31 CFR § 1020.220 (cascaded) |
| ECOA Reg B 25-month retention | High — 12 CFR § 1002.12 |
| 7-year UDAP defense norm | Medium — varies by state SOL; 7 covers most |
| State breach notification windows | High — verbatim statutory text |
