"""3-track scoring redesign — pure additive scaffolding.

The redesign described in ``docs/SCORING_REDESIGN_CONTINUATION.md``
replaces the single ``fraud_score`` with three orthogonal outputs:

* **Track A — Document Integrity.** Near-binary gate. Is the
  statement real? Output: ``integrity_verdict ∈ {clean, review, fail}``.
* **Track B — Business Risk.** Explainable 4-band score. Can the
  business support repayment? Output:
  ``risk_band ∈ {low, moderate, elevated, high}``.
* **Track C — Context / Concentration.** Informational only, never
  auto-penalizes. Counterparty mix, stress reasoning, durability
  reframe. Informs Track B's band at most; never independently fires
  a decline.

This package is the scaffold for the three tracks. The legacy
``aegis.scoring`` module continues to power production scoring; the
new tracks ship purely additively and are NOT wired into the decline
decision in this commit. Step 2 of the build order (per the design
doc) is the eventual flip; that's gated on shadow-mode corpus
validation and is a separate, deliberate change.

Track C ships first (per the build-order doc). It reads the
counterparty classifier foundation (``aegis.counterparty``) — every
inflow dollar is in a named class, so the concentration denominator
is correct.
"""
