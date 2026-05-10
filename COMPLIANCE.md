# COMPLIANCE.md — Statute Citation Index

Quick-reference table for AEGIS's compliance posture. Detailed dossiers
live under `docs/compliance/`. Read
`docs/compliance/15_aegis_compliance_posture.md` for the master
obligation matrix and `docs/compliance/CORRECTIONS_2026-05-08.md` for
the verification audit log.

**Source of truth for runtime behavior:** `src/aegis/compliance/states.py`.
This file is human-readable index; the Python module is what the
disclosure router and matcher actually consult.

---

## Served states (45)

Texas, Virginia, Connecticut, Utah, Missouri, Washington DC, and U.S.
territories are NOT served — the API rejects deals from those states
with `state_not_served`.

---

## State regulation tiers

### Tier 1 — MCA-specific commercial financing disclosure law in effect

| State | Bill | Effective | APR method | CoJ | Dossier | Template |
|---|---|---|---|---|---|---|
| **CA** | SB 1235 + SB 362 (2018, amended 2025) | 2022-12-09 (regs); SB 362 effective 2026-01-01 | Actuarial Reg Z | Banned | `01_california.md` | `ca_sb1235.html.j2` |
| **NY** | CFDL — SB 5470 + S898 (2020, amended 2021) | 2023-08-01 (mandatory compliance) | Actuarial Reg Z | Conditional (residents only) | `02_new_york.md` | `ny_cfdl.html.j2` |
| **FL** | FCFDL — Fla. Stat. § 559.961 et seq. (2023) | 2024-01-01 | Not required | Banned | `03_florida.md` | `fl_fcfdl.html.j2` |
| **GA** | SB 90 — OCGA § 10-1-393.18 et seq. (2023) | 2024-01-01 | Actuarial Reg Z | Allowed (venue restricted, OCGA § 9-12-18) | `04_georgia.md` | `ga_sb90.html.j2` |

### Tier 2 — General state law applies; no MCA-specific statute

| State | Citation | CoJ | Notes | Dossier |
|---|---|---|---|---|
| **IL** | 815 ILCS 505 (UDAP); 815 ILCS 175 (Loan Brokers Act); 735 ILCS 5/2-1301(c) (CoJ) | Allowed (commercial only) | HB 3477 pending; loan-broker registration may apply, verify before first IL deal | `05_illinois.md` |

### Tier 3 — Served, audit pending

The remaining 40 served states default to Tier 3. Disclosure endpoint
raises `StateNotAudited`. Each requires operator-supplied source
material before promotion (statute text, citation URL, regulator's
prescribed form for Tier 1).

States in Tier 3: AL, AK, AZ, AR, CO, DE, HI, ID, IN, IA, KS, KY, LA,
ME, MD, MA, MI, MN, MS, MT, NE, NV, NH, NJ, NM, NC, ND, OH, OK, OR, PA,
RI, SC, SD, TN, VT, WA, WV, WI, WY.

**Quarterly review priorities** (per master dossier): NJ, MD, MS, NC
have pending CFDL bills; re-check IL HB 3477 status.

---

## Federal & cross-cutting obligations

| Obligation | Source | AEGIS posture | Dossier |
|---|---|---|---|
| Section 1071 (CFPB) | Reg B Subpart B, 12 CFR § 1002.101 et seq. | Exempt (MCAs excluded) | `06_section_1071_federal.md` |
| OFAC sanctions | 31 CFR Ch. V | Required (hard decline on SDN match); Phase 3 implementation in `scoring/ofac.py` | `07_ofac_sanctions.md` |
| AML / BSA / KYC | 31 USC § 5312, 31 CFR Ch. X | Cascade via funder ISO; AEGIS performs CIP-equivalent checks | `08_aml_bsa_kyc.md` |
| CFPB UDAAP / state UDAP | 12 USC § 5531, state analogs | Marketing review, complaint log | `09_cfpb_udaap.md` |
| Record retention | Multi-statute | See dossier for per-record-type schedule | `10_record_retention.md` |
| NY DFS Cybersecurity (Part 500) | 23 NYCRR Part 500 | Cascade via funder where applicable | `11_data_security_privacy.md` |
| FTC Safeguards Rule | 16 CFR Part 314 | Direct (verify applicability with counsel) | `11_data_security_privacy.md` |
| State breach notification | 50-state patchwork | Direct | `11_data_security_privacy.md` |
| MCA-vs-loan reclassification defense | State case law (Yellowstone-pattern) | Architectural | `12_mca_vs_loan_reclassification.md` |
| Broker advance-fee prohibition (FL/GA) | Fla. Stat. § 559.9614, OCGA § 10-1-393.20 | Hard-fail match if funder charges merchant advance fees + state in {FL, GA} | `13_broker_specific_rules.md` |
| IL Loan Brokers Act | 815 ILCS 175 | Verify applicability before first IL deal | `13_broker_specific_rules.md` |
| Renewal re-disclosure | CA SB 362, NY § 600.6 | Implemented in `compliance/renewal.py` | `14_renewal_redisclosure.md` |
| FTC Telemarketing Sales Rule | 16 CFR Part 310 | Conditional (only if AEGIS telemarkets) | `13_broker_specific_rules.md` |

---

## How a Tier 3 state gets promoted

The TS predecessor invented Tier 1 entries from prior knowledge,
producing fictional bill numbers. We do not repeat that. Promotion
sequence:

1. Operator supplies source material: bill text, citation URL,
   verbatim excerpt (≤500 words), effective date, prescribed form (for
   Tier 1), CoJ rule with sub-citation.
2. Claude writes a dossier under `docs/compliance/NN_<state>.md` with a
   confidence assessment.
3. Operator reviews the dossier against the source material.
4. Claude updates `src/aegis/compliance/states.py` with the new
   `Tier1Regulation` or `Tier2Regulation`. For Tier 1, also adds the
   Jinja template under `compliance/templates/{state}_{bill}.html.j2`
   matching the regulator's prescribed form line-by-line.
5. Snapshot test added under `tests/snapshots/` locking the rendered
   HTML.
6. Boot validator (`validate_states_table()`) must still pass.
7. Operator reviews template against the regulator's prescribed form.
8. Commit message cites the dossier:
   `feat(compliance): promote XX from Tier 3 to Tier 1 per docs/compliance/NN_<state>.md`.

This sequence is non-negotiable for Tier 1 promotions because the
prescribed form is regulator-mandated and a generic template is not
acceptable.
