# MCA vs Loan Reclassification — Complete AEGIS Compliance Dossier

**Researched: 2026-05-07** by Claude (web-search based) for operator verification.
**Status: critical for AEGIS architecture. The features that make a transaction defensible as a "purchase" rather than a "loan" must be encoded in AEGIS's deal structure validation.**

---

## TL;DR for AEGIS

The defining structural risk in MCA work is **reclassification** — courts treating an MCA as a disguised usurious loan rather than a true sale of receivables. Reclassification voids the transaction, exposes the funder to usury claims, and exposes the broker to UDAP and aiding-and-abetting claims.

The legal standard varies by state but converges on three core factors:
1. **Reconciliation:** Does the contract include a real, available, used reconciliation mechanism that adjusts payments to actual revenue?
2. **Risk:** Does the funder bear genuine risk of non-recovery if the merchant's revenue genuinely declines?
3. **Term:** Is the term unconditional and date-certain (loan-like) or contingent on revenue (purchase-like)?

The Yellowstone Capital judgment (NY AG, January 2025, $1.065B) was the largest enforcement action in MCA history. The NY AG's pattern of allegations is now the reference template for what NOT to do. Every operational signal Yellowstone violated is a signal AEGIS must monitor.

---

## The legal framework

### True purchase vs disguised loan — the multi-factor test

Most state courts apply some version of these factors (drawn from *Pearl Capital v. Banafsheh*, *LG Funding LLC v. United Senior Properties*, and *Funding Metrics LLC v. NDG Logistics LLC*):

1. **Reconciliation provision present and meaningful?**
   - Does the contract say payments adjust to actual revenue?
   - Is the reconciliation actually available (not gated by impossible conditions)?
   - Has reconciliation been used in practice with this merchant?

2. **Term unconditional or contingent?**
   - Does the agreement set a fixed maturity date independent of revenue?
   - Or does it say "until the purchased amount is collected"?

3. **Risk transfer real?**
   - If merchant's business genuinely fails, does funder lose money?
   - Or does funder's recourse (personal guarantees, COJs, etc.) make recovery near-certain?

4. **Personal guarantees framed as loan-guarantees or fraud-guarantees?**
   - "Performance guarantee" against fraud/misrepresentation is OK.
   - "Payment guarantee" of full purchase amount looks loan-like.

5. **Contractual language characterization?**
   - Does the contract say "purchase of receivables" consistently?
   - Or does it slip into "loan," "interest," "principal" language?

6. **Origination fee structure?**
   - Fixed upfront fees independent of receivables → loan-like.
   - Discount on receivables → purchase-like.

When courts find for reclassification: contract void, usury caps apply retroactively, restitution.

---

## The Yellowstone enforcement pattern

In January 2025, NY AG Letitia James obtained a **$1.065 billion judgment** against Yellowstone Capital and affiliates. The largest consumer protection settlement in NY history outside multistate. The NY AG's allegations — now the template for enforcement — covered:

### Pattern 1: Disguised loans
- Contracts described as MCAs but operating as loans.
- Fixed payments without functional reconciliation.
- Personal guarantees that made repayment effectively absolute.

### Pattern 2: Aggressive collection
- Use of CoJs against merchants in NY courts even when merchants weren't NY residents (this is what motivated CPLR § 3218 reform in 2019).
- Bank-account freezing without due process.
- Default declarations on technical breaches with no cure period.

### Pattern 3: Misrepresentation
- "Cost" disclosed in factor rate to obscure annualized rate.
- Aggressive sales tactics promising quick approval and ignoring reconciliation rights.

### Pattern 4: Lack of records
- Inconsistent or missing reconciliation requests.
- Lack of merchant identity verification.
- Lack of contemporary records about decision-making.

### Pattern 5: Stacking and pyramiding
- Funding merchants who already had multiple positions.
- Renewals with double-dipping (charging finance charges on already-paid amounts).

**For AEGIS:** every architectural decision should be tested against "would this look like a Yellowstone pattern?"

---

## What AEGIS architecturally encodes

### 1. Funder reconciliation policy validation
Before approving a funder for AEGIS's catalog, verify their template MCA agreement contains:
- ✓ Explicit reconciliation provision (revenue percentage, true-up).
- ✓ Reconciliation is automatic OR easy to invoke (no impossible conditions).
- ✓ Records of reconciliation use exist.

If a funder's template lacks meaningful reconciliation: **DO NOT add to AEGIS catalog.** This is operator policy, not just regulatory.

### 2. Match log signals
When AEGIS matches a deal to a funder, log structural signals:
```python
class FunderMatchSignals:
    deal_id: UUID
    funder_id: UUID
    has_reconciliation_provision: bool
    reconciliation_type: Literal["automatic_revenue_split", "true_up_request", "fixed_with_remediation"]
    fixed_payment_term_days: int | None  # if fixed-term, flag
    coj_required: bool
    personal_guarantee_type: Literal["fraud_only", "performance", "payment_full"]
    factor_rate: Decimal
    apr_computed: Decimal
    deal_term_days: int
    structural_risk_flags: list[str]  # see below
```

Structural risk flags (any of these triggers an internal review):
- `fixed_term_no_revenue_contingency`
- `personal_guarantee_payment_not_fraud`
- `coj_required_in_state_that_bans` (already enforced via state dossiers)
- `factor_rate_implies_apr_over_300pct`
- `reconciliation_gated_by_impossible_conditions`
- `merchant_has_active_other_positions` (stacking)

### 3. Anti-stacking detection (already in Phase 2)
AEGIS's bank statement parser already detects MCA-pattern outflows. Operationalize:
- If merchant has 2+ active MCA positions → flag "stacking_detected".
- If 3+ active positions → hard block until operator review.
- This protects AEGIS from being part of a stacking pyramid that triggers reclassification claims.

### 4. Disclosure consistency
The state disclosure documents (CA SDF, NY CFDL) MUST match the funder agreement exactly. Discrepancy = misrepresentation = UDAP exposure. AEGIS's disclosure validation logic compares funder agreement key fields to disclosure key fields:
- Funded amount.
- Total payback.
- Payment frequency and amount.
- Reconciliation provision.
- Prepayment terms.
If they don't match, deal does not proceed.

### 5. Source attribution audit trail (already in Phase 2)
Every aggregate AEGIS computes is traceable back to PDF page+line. This audit trail IS the defense in a reclassification case: "AEGIS's underwriting analysis was based on these specific transactions, the conclusions follow these specific computations, here are the records."

### 6. Renewal anti-double-dipping check
When a renewal occurs, NY explicitly requires double-dipping disclosure (see `02_new_york.md`). For all states, AEGIS should:
- Compute pro-rata unearned finance charge from prior position.
- Flag if renewal includes amounts that pay unpaid finance charges from the prior position.
- Surface this to the operator + include in disclosure where required.

### 7. Operator review of factor-rate outliers
Any deal with factor rate above industry norms (typically > 1.5 for short-term MCAs, > 1.4 for longer-term) gets operator review. Outlier rates correlate with reclassification risk.

---

## Database tables

```python
class FunderMcaTemplateReview:
    """Stored when operator approves a funder for AEGIS catalog."""
    funder_id: UUID
    template_doc_hash: str
    reconciliation_present: bool
    reconciliation_type: str
    reconciliation_invocation_difficulty: Literal["automatic", "easy", "moderate", "hard"]
    fixed_term_present: bool
    coj_clause_present: bool
    personal_guarantee_type: str
    structural_risk_signals: list[str]
    operator_decision: Literal["approved", "approved_with_conditions", "rejected"]
    operator_notes: str
    reviewed_at: datetime
    template_active: bool

class StackingDetectionResult:
    deal_id: UUID
    detected_active_positions: int
    detected_position_funders: list[str]  # inferred from bank statement patterns
    detection_confidence: Decimal
    decision: Literal["proceed", "review_required", "blocked"]
    operator_override: bool
```

---

## What AEGIS does NOT do

- AEGIS does NOT advise on contract drafting. Funder agreements are between funder and merchant.
- AEGIS does NOT represent that a transaction "is not a loan" — that characterization is a legal conclusion, not AEGIS's to make.
- AEGIS does NOT participate in collection activities.
- AEGIS does NOT structure the merchant-side terms (factor rate, payback amount) — funder does.

But AEGIS's job is to detect when a funder's terms or a merchant's circumstance create reclassification risk and to refuse the deal.

---

## Operator playbook for reclassification claims

If a merchant or merchant's attorney sends a letter alleging reclassification:
1. **Engage counsel immediately.** Do not respond on your own.
2. **Litigation hold all related records.** Funder match log, deal file, disclosure documents, communications.
3. **Provide records to counsel.** AEGIS's audit trail makes this fast.
4. **Notify the funder.** Most ISO agreements require notification of merchant disputes within 24-48 hours.
5. **Do not communicate further with the merchant** until counsel directs.
6. **Document the inquiry** in `merchant_complaints` log.

If a regulator (state AG, CFPB) sends an inquiry alleging reclassification or UDAP:
- Same as above plus engage specialized commercial finance counsel.
- AEGIS's records are the defense. Produce them per counsel's direction.

---

## Source URLs and key cases

1. **NY AG Yellowstone Capital settlement announcement** — https://ag.ny.gov/press-release/2025/attorney-general-james-secures-1065-billion-judgment-against-merchant-cash
2. **Pearl Capital Rivis Capital v. Banafsheh (Cal. 2018)** — typical reconciliation-required reasoning
3. **LG Funding LLC v. United Senior Properties (NY 2020)** — multi-factor test articulation
4. **Funding Metrics LLC v. NDG Logistics LLC (NY 2017)** — early formulation of the test
5. **Champion Auto Sales v. Pearl Capital (NY 2017)** — reconciliation enforcement
6. **Industry analysis** — https://www.lendsaas.com/2026/01/01/the-official-2026-mca-compliance-checklist/

---

## Confidence

| Finding | Confidence |
|---|---|
| Yellowstone $1.065B judgment | High — public NY AG announcement |
| Multi-factor test (reconciliation, risk, term) | High — repeated across state cases |
| Reconciliation requirement is the most-cited factor | High — *LG Funding* and successors |
| Factor rate > 1.5 correlates with reclassification risk | Medium — industry observation, not legal threshold |
| AEGIS's audit trail is the operative defense | High — pattern of MCA litigation outcomes |
| Anti-stacking detection is Yellowstone-aligned | High — NY AG's specific allegation set |

Verification action for operator: **before AEGIS funds first deal, have a commercial finance attorney review AEGIS's funder approval criteria and disclosure consistency logic.** This is the one place where attorney consult is genuinely load-bearing — the reclassification framework is too case-law-driven to fully reduce to web research.
