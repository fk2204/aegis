# Renewals & Re-Disclosure — Complete AEGIS Compliance Dossier

**Researched: 2026-05-07** by Claude (web-search based) for operator verification.
**Status: AEGIS must distinguish renewal scenarios from new transactions because some states require fresh disclosures and disclose double-dipping. CA's SB 362 adds re-disclosure on every pricing communication.**

---

## TL;DR for AEGIS

Renewal handling differs by state:

| State | New disclosure required for renewal? | Anti-double-dipping disclosure? | APR re-disclosure on every quote? |
|---|---|---|---|
| **CA** | Yes (SB 362, eff 2026-01-01) — labeled "Renewal" with updated projections | Indirectly via APR | Yes (SB 362) |
| **NY** | Yes — new disclosure each specific offer | **Yes — explicit § 600.6(b)(3)(v)** | Yes — built into reg |
| **FL** | No new disclosure for modification/forbearance/change | No | No |
| **GA** | No new disclosure for modification/forbearance/change | No | No |
| **IL** | n/a | n/a | n/a |

The most consequential rule: **NY's anti-double-dipping disclosure**. Every renewal in NY must explicitly state how much of the new financing is being used to pay unpaid finance charges on the prior position.

---

## What is a "renewal" vs a "modification"?

The line matters because state laws treat them differently:

- **Renewal:** new agreement, same merchant, frequently while old position is still being repaid. Old position is paid off (in part or whole) from new advance, and new advance also provides additional funds. **New disclosure required in CA and NY.**
- **Modification:** existing agreement is amended (term extended, payment frequency adjusted, fees waived) without a new financing event. **No new disclosure required in any of CA/NY/FL/GA.**
- **Forbearance:** payments are paused or reduced temporarily. **No new disclosure.**
- **Default workout:** restructuring after default. **State-specific; consult counsel.**

For AEGIS purposes: distinguish in the data model.

```python
class TransactionType(StrEnum):
    NEW = "new"
    RENEWAL = "renewal"  # new agreement; triggers fresh disclosure
    MODIFICATION = "modification"  # amend existing
    FORBEARANCE = "forbearance"  # pause/reduce payments
    DEFAULT_WORKOUT = "default_workout"
```

---

## NY anti-double-dipping rule (§ 600.6(b)(3)(v))

**Verbatim regulatory requirement:**
> If any portion of the amount financed will be used to satisfy obligations under another financing with the provider, in the third column, in a second paragraph: "Does the renewal financing include any amount that is used to pay unpaid finance charges or fees, also known as double dipping? {Yes, enter amount}. If the amount is zero, the answer would be No." If the financing being satisfied featured a fixed finance fee that did not vary based on the repayment period, the provider shall consider the amount that is used to pay unpaid finance charges or fees to be the pro rata portion of such finance fee based upon the fraction of the original total amount financed of the previous financing already repaid by the recipient.

### What this means in plain English

When a NY merchant renews an MCA, the disclosure must answer: "How much of this new advance is paying for unfinished finance charges on the previous one?"

The math:
```
prior position:
  original_funded_amount = $100,000
  original_total_payback = $130,000  # implies $30K fixed finance charge
  amount_repaid_so_far    = $80,000

renewal:
  funded_amount = $50,000  # of which X used to pay off prior position
  prior_position_payoff = $50,000  # = remaining balance on prior

double_dipping_amount:
  fraction_of_prior_already_repaid = 80,000 / 100,000 = 0.80
  pro_rata_already_paid_finance_charge = $30,000 * 0.80 = $24,000
  total_finance_charge_on_prior = $30,000
  unpaid_finance_charge_on_prior = $30,000 - $24,000 = $6,000
  if renewal pays off the prior $50K AND renews from a position where any finance charge is unearned:
     double_dipping = $6,000  (this is the explicit disclosure)
```

### What AEGIS implements

```python
class RenewalDoubleDipping:
    """Compute NY-required double-dipping disclosure for a renewal."""

    @staticmethod
    def compute(
        prior_funded_amount: Decimal,
        prior_total_payback: Decimal,
        prior_amount_repaid: Decimal,
        renewal_amount_used_to_pay_prior: Decimal,
    ) -> Decimal:
        prior_finance_charge = prior_total_payback - prior_funded_amount
        fraction_repaid = prior_amount_repaid / prior_funded_amount
        pro_rata_finance_charge_paid = prior_finance_charge * fraction_repaid
        unpaid_finance_charge = prior_finance_charge - pro_rata_finance_charge_paid

        # the disclosure is the lesser of:
        # (a) what's actually being used from the renewal to pay unpaid finance charges
        # (b) the unpaid finance charge balance
        # if renewal uses more than just unpaid finance charges, only the finance-charge portion counts
        return min(renewal_amount_used_to_pay_prior, unpaid_finance_charge)
```

This computation goes into the NY disclosure document at row 1 second-paragraph.

---

## CA SB 362 re-disclosure rule (effective 2026-01-01)

**Statutory rule:** every time AEGIS or a funder communicates a charge, pricing metric, or financing amount to a recipient, the APR must also be stated using "annual percentage rate" or "APR".

### What this means
- Quote emails: APR alongside factor rate.
- Sales calls: APR if any pricing detail is communicated.
- Term sheet revisions: APR.
- Submission packages forwarded by AEGIS: APR.
- Renewal quotes: APR.

### What AEGIS implements

```python
class CaPricingCommunicationGuard:
    """Validate that any communication to a CA merchant containing pricing also contains APR."""

    PRICING_KEYWORDS = {
        "factor", "factor rate", "buy rate", "payback",
        "daily payment", "weekly payment", "total cost",
        "finance charge", "fee", "amount financed", "funded amount"
    }

    @staticmethod
    def validate(
        merchant_state: str,
        communication_text: str,
        contains_apr: bool,
    ) -> ValidationResult:
        if merchant_state != "CA":
            return ValidationResult.passed()
        contains_pricing = any(
            kw in communication_text.lower() for kw in CaPricingCommunicationGuard.PRICING_KEYWORDS
        )
        if contains_pricing and not contains_apr:
            return ValidationResult.failed(
                reason="ca_sb362_apr_required_with_pricing",
                cite="Cal. Fin. Code § 22806",
            )
        return ValidationResult.passed()
```

This is enforced on outbound merchant communications generated by AEGIS for CA-based merchants.

---

## CA SB 362 "Renewal" labeling

When AEGIS generates a CA disclosure for a renewal:
- The disclosure document must be labeled "Renewal."
- Updated sales projections must be used (not those from the prior funding).
- All APR calculations recomputed based on current projections.

```python
def generate_ca_disclosure_for_renewal(deal: Deal) -> Disclosure:
    # ...standard CA disclosure generation...
    disclosure.label = "Renewal"
    disclosure.sales_projection_basis_date = date.today()  # not original funding date
    return disclosure
```

---

## NY re-disclosure on every specific offer

NY requires APR re-disclosure on every pricing communication just like CA SB 362. This is built into 23 NYCRR Part 600 from inception.

For AEGIS, this means the same `PricingCommunicationGuard` applies for NY merchants from August 1, 2023 onward (already in force).

---

## FL/GA — no renewal-disclosure trigger

Florida § 559.9613(1) explicitly says: "a disclosure is not required as result of a modification, forbearance, or change to a consummated commercial financing transaction." Georgia is similar.

For renewals (which are not modifications but new transactions), the answer is more nuanced. A renewal IS a new commercial financing transaction in FL/GA, so a NEW disclosure IS required for the renewal — but no anti-double-dipping logic is mandated by statute.

**AEGIS architectural rule:** for renewals in FL/GA, generate a fresh disclosure as for a new transaction. Don't include the NY-specific double-dipping language (it's not required and may cause inconsistency with FL/GA forms).

---

## Renewal trigger detection

How AEGIS knows a deal is a renewal:

1. **Operator-flagged:** in the deal intake form, operator marks "this is a renewal of a prior position with funder X."
2. **Auto-detected:** AEGIS bank statement parser detects existing MCA position pattern with the same funder. Surfaces as soft signal.
3. **Funder-disclosed:** funder agreement explicitly says "renewal of agreement #..."

When detected:
- AEGIS retrieves prior position data (if AEGIS has it).
- AEGIS computes double-dipping if NY merchant.
- AEGIS labels disclosure "Renewal" if CA merchant.
- AEGIS generates fresh APR for FL/GA merchants.

---

## What AEGIS records

```python
class RenewalContext:
    deal_id: UUID  # the renewal deal
    prior_deal_id: UUID | None  # if AEGIS has prior deal
    prior_funded_amount: Decimal | None
    prior_total_payback: Decimal | None
    prior_amount_repaid: Decimal | None
    prior_position_payoff_from_renewal: Decimal | None
    double_dipping_amount: Decimal | None  # for NY disclosure
    detection_method: Literal["operator_flagged", "auto_detected", "funder_disclosed"]
    detection_confidence: Decimal | None
```

---

## Source URLs

1. **23 NYCRR § 600.6 (sales-based financing form, includes anti-double-dipping)** — https://www.law.cornell.edu/regulations/new-york/23-NYCRR-600.6
2. **CA SB 362 (re-disclosure rule)** — https://leginfo.legislature.ca.gov/faces/billTextClient.xhtml?bill_id=202520260SB362
3. **CA SB 1235 / 10 CCR § 941 (renewal procedures)** — https://www.law.cornell.edu/regulations/california/10-CCR-941
4. **Fla. Stat. § 559.9613** — https://www.flsenate.gov/Laws/Statutes/2024/559.9613
5. **GA SB 90** — https://www.legis.ga.gov/api/legislation/document/20232024/219440

---

## Confidence

| Finding | Confidence |
|---|---|
| NY anti-double-dipping disclosure (§ 600.6(b)(3)(v)) | High — verbatim regulatory text |
| CA SB 362 re-disclosure rule | High — bill text |
| FL/GA no modification re-disclosure | High — verbatim statutory text |
| Pro-rata finance charge computation method | High — explicit in NY reg text |
| AEGIS detection of renewal vs new | Medium — operator flag + auto-detect; not all funders flag clearly |
| "Renewal" labeling on CA disclosure | High — required by 10 CCR § 941 (verify exact section) |
