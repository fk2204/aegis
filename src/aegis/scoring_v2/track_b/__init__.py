"""Track B — Business Risk Band.

Explainable 4-band score (``low / moderate / elevated / high``)
answering "can the business support repayment?". Reads the same
counterparty-aware aggregation Track C uses, so the revenue basis
is correct (true_revenue excludes own_account, own_account_unconfirmed,
book_wire_unresolved, card_paydown).

Per the design doc (``docs/SCORING_REDESIGN_CONTINUATION.md``):

* The band is **explainable** — each landing is tied to the SPECIFIC
  factors that put it there. Reasons are surfaced on the output so
  the underwriter sees why, not just what.
* The band → action mapping is **decided** (Q1):
    low      → auto_forward
    moderate → review_neutral
    elevated → review_neutral
    high     → review_decline_default
  Both moderate and elevated map to review_neutral; the band is a
  finer signal than the action so the underwriter can prioritize.
  Nothing auto-declines on business-risk alone; document-integrity
  ``fail`` from Track A is the only auto-block.
* Track C concentration **informs** Track B's band at most; never
  independently fires a decline. Track B reads the same aggregation
  Track C consumes, computes a concentration signal directly (without
  importing Track C's panel — Track B and Track C are orthogonal
  consumers of the shared aggregation).

THIS COMMIT SHIPS PURELY ADDITIVE. The band is computed and tested
but not wired into the live decline path. The blended ``fraud_score``
remains in control of production until Step 2 of the redesign
deliberately replaces it.
"""

from aegis.scoring_v2.track_b.compute import compute_risk_band
from aegis.scoring_v2.track_b.models import (
    BAND_TO_ACTION,
    SEVERITY_TO_BAND,
    BandAction,
    BandLevel,
    BusinessRiskBand,
    CashflowSignals,
    FactorReason,
    SignalSeverity,
)

__all__ = [
    "BAND_TO_ACTION",
    "SEVERITY_TO_BAND",
    "BandAction",
    "BandLevel",
    "BusinessRiskBand",
    "CashflowSignals",
    "FactorReason",
    "SignalSeverity",
    "compute_risk_band",
]
