# Phase 4 — Counsel Questions

**Created:** 2026-05-19
**Phase:** Master plan §14 — State misclassification fixes
**Branch:** `feature/mp-phase-4-state-fixes`
**Status:** Awaiting counsel review

This file collects the questions Phase 4 surfaces for counsel before any
counsel-gated code change ships. Each question cites the controlling
authority from the master plan + dossiers, states a yes/no or
interpretive question, names the specific code change AEGIS would make
if counsel approves, and lists the operator-readable consequence of
getting it wrong.

The non-counsel-gated half of Phase 4 (VA / CT / UT / MO moved out of
`state_not_served` to Tier 3 served, no template yet) ships on this
branch without waiting for counsel — those four states have no statute
that hard-bans serving them, only disclosure-template obligations that
the Tier 3 fall-through (`StateNotAudited`) keeps closed until Phase 5
builds the templates.

---

## TX — Auto-debit prohibition vs. Commera's MCA structure

**Cited authority:** TX HB 700 (Ch. 398, TX Finance Code), effective
2025-09-01. Per master plan §8.5 ("Overlay rules detail" /
auto-debit): "Prohibited unless provider holds first-priority perfected
UCC security interest in the deposit account. Effective deal-killer for
standard MCA in TX. AEGIS hard-declines TX deals using standard
ACH-debit structure."

Per master plan §14 task 2: TX may be promoted from `state_not_served`
to Tier 1 served only if the operator is willing to enforce the
hard-decline rule on standard ACH-debit MCAs.

**Question for counsel:**

1. Confirm Commera funder partners cannot satisfy the HB 700
   first-priority perfected UCC security interest requirement on the
   deposit account under their standard ACH-debit MCA structure. If
   ANY funder *can* satisfy it, name them so we can configure their
   per-funder exception.
2. Confirm the operator-facing decline message is acceptable as
   currently drafted in `docs/compliance/states.yaml` (TX
   `hard_decline_rules[0].message`).

**Proposed code change (if counsel says yes — i.e., no funder satisfies
the lien requirement):**

- Add `TX` to `_SERVED_STATES` in
  `src/aegis/compliance/states.py` as a Tier 3 entry (template comes
  in Phase 5).
- Wire `tx_autodebit_without_first_priority_lien` into the deal-scoring
  flow so it fires at intake regardless of funder selection. Today the
  rule lives in `docs/compliance/states.yaml` and `router()` returns it,
  but `score_deal()` does not consume router output yet. The wire-in
  goes in `src/aegis/scoring/score.py::_check_hard_declines` driven by a
  new `deal.state` check that calls `router()` and propagates
  `hard_decline_rules`.
- Add tests in `tests/test_router.py` and a new
  `tests/compliance/test_texas_tier1.py` covering the matcher path.

**Proposed code change (if counsel identifies one or more compliant
funders):**

- Same as above, plus a per-funder `holds_first_priority_lien_tx: bool`
  flag on `FunderRow`. The matcher then surfaces TX deals only to
  funders with that flag set; all other funders hard-fail the pairing
  with `tx_autodebit_without_first_priority_lien`.

**Blast radius if wrong:**

- *If we serve TX without the hard-decline:* every TX deal that closes
  is a $10k-per-violation OCCC enforcement target (no PRA, but the OCCC
  fines compound). Worst case: a few dozen TX deals × $10k = six-figure
  exposure plus injunctive remediation.
- *If we hard-decline TX deals that a funder COULD legitimately fund:*
  operator loses a small number of deals to a competitor (zero
  regulatory exposure; pure deal-flow cost).

---

## TX — Serving TX before the 2026-12-31 OCCC registration deadline

**Cited authority:** TX HB 700 §III. Per master plan §8.2: "registration
applies regardless of size by 2026-12-31. Renew by Jan 31 annually."

**Question for counsel:**

Can Commera or its registered funder partners serve TX merchants
*today* (before the 2026-12-31 OCCC registration deadline), or does
HB 700 require registration to be effective before any deal is
disclosed? Disclosure obligations are effective 2025-09-01; the
registration deadline is later. Distinguish these and confirm AEGIS's
posture.

**Proposed code change (if counsel says serve now):** add TX to
served list, fire the auto-debit hard-decline.

**Proposed code change (if counsel says wait until registered):** keep
TX in `state_not_served` until the operator has the OCCC registration
in hand; we revisit this question on 2026-12-31 (or earlier if the
operator registers ahead of the deadline). Add a TODO to the master
plan section.

**Blast radius if wrong:** transmitting a disclosure as an unregistered
provider is itself a HB 700 violation. Operator + funder exposure
mirrors the auto-debit question above.

---

## FL — AEGIS broker advance fees to merchants

**Cited authority:** Fla. Stat. § 559.9614(1)(a) per
`docs/compliance/03_florida.md`. The statute prohibits brokers from
assessing, collecting, or soliciting an advance fee from a merchant
for brokering services. Narrow carve-out for "actual" third-party
costs (credit check, appraisal) paid by check or money order to an
independent third party. Per master plan §14 task 5: "Per-broker
config — Commera almost certainly doesn't charge advance fees, but
verify."

**Question for counsel + operator:**

1. Confirm Commera (as broker) does NOT assess, collect, or solicit an
   advance fee from FL merchants under any circumstance.
2. If Commera ever uses a credit-check or appraisal pass-through, does
   that vendor invoice the merchant directly (check or money order to
   independent third party), or does Commera collect and forward? Only
   the first form is within the § 559.9614(1)(a) carve-out.

**Proposed code change (if counsel says no advance fees, and Commera
confirms operator-side):**

- No matcher change needed — current
  `match_funders.py::_broker_advance_fee_rule` already hard-fails any
  funder × FL pairing when `funder.charges_merchant_advance_fees=True`.
- Add an operator-config-level assertion: a boot-time check that
  AEGIS-as-broker does not have an `aegis_charges_merchant_advance_fees=True`
  global flag set. Wire the flag into `src/aegis/config.py` or
  equivalent; default False; fail-closed if True and any FL/GA deal is
  routed.
- Add `tests/compliance/test_florida_tier1.py` assertion that the
  AEGIS-side flag defaults False and that flipping it True causes the
  pre-submission gate to reject FL merchants.

**Proposed code change (if Commera DOES collect pass-throughs):**

- Surface as soft-warning at submission time; require operator to
  manually confirm the pass-through is invoiced by the independent
  third party (not by Commera). Audit-log every confirmation.

**Blast radius if wrong:** $500-$20k/violation (initial),
$50k continued. AG-only enforcement, no PRA, but Florida AG is
active in commercial finance — operator gets an action that's
expensive to defend even when meritless.

---

## GA — AEGIS broker advance fees to merchants (mirrors FL)

**Cited authority:** O.C.G.A. § 10-1-393.18 per
`docs/compliance/04_georgia.md`. Same broker advance-fee prohibition as
FL. Same carve-out structure.

**Question for counsel + operator:** identical to FL question above.

**Proposed code change:** identical to FL — the matcher rule is already
state-driven via `_broker_advance_fee_rule`; only the AEGIS-side flag
needs adding.

**Blast radius if wrong:** $500-$20k/violation (initial), $50k
continued. AG-only enforcement under Fair Business Practices Act.

---

## Open follow-up — confirmation of "first-priority lien" funder structure on file

**Not blocking Phase 4 ship.** Phase 5 needs to know what structure
funders use that ACTUALLY satisfy first-priority lien (UCC-1 perfected
filing on the deposit account, lockbox arrangements, etc.). When
counsel answers the TX questions above, capture the structure name and
add it to `FunderRow.notes` per-funder so AEGIS-future can match
deal-level structure against this requirement.

---

## How to record counsel response

When counsel responds, append a `## Counsel Response — <date>` section
to this file with:
- Date of response
- Counsel's name + firm
- Verbatim language on each question (yes / no / "with the following
  conditions")
- Pointer to the commit that lands the code change

The commit landing each gated change must include:
```
compliance-review: approved by <counsel name>
```
in the commit body (pre-commit hook at `.githooks/pre-commit` enforces
this when any file under `docs/compliance/states/**` is staged).
