"""Track C — Context / Concentration.

Informational only, never auto-penalizes. Surfaces the counterparty
mix as DURABILITY context for the underwriter: which classes dominate,
what the stress case looks like if the top class drops, and whether
the underwriter should be asking specific follow-up questions
(international wires → durability question; processor → low concern
unless a single processor disputes; end-customer → genuine
concentration risk).

The KEY reframe (per the design doc and the VU case study):

* VU's three international wires totalling $324,700 are
  ``international_client`` concentration — a durability question
  (will the international counterparty continue paying?), NOT a fraud
  signal. The previous (pre-redesign) scorer flagged international
  wires as suspicious; this panel reframes them correctly.
* Processor concentration is a LOW signal: payment rails (Shopify,
  Stripe, WooPayments) aggregate many end customers and rarely
  disappear. Concern only triggers on rail-specific events (a
  processor holding funds, a chargeback dispute).
* End-customer concentration is the genuine concern: if 60% of
  revenue is from one named end customer, the deal lives or dies on
  that one relationship.

Track C ships PURELY ADDITIVE. The dossier reads it as a context
panel; it does NOT change any decline boundary. Track A (integrity)
and Track B (business risk) remain the things that move the
decision; Track C informs Track B at most.
"""

from aegis.scoring_v2.track_c.compute import compute_context_panel
from aegis.scoring_v2.track_c.models import (
    ConcentrationContextPanel,
    StressView,
)

__all__ = [
    "ConcentrationContextPanel",
    "StressView",
    "compute_context_panel",
]
