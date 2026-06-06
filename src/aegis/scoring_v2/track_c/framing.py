"""Per-class durability framing for the Track C context panel.

Each counterparty class gets a one-line reframe that tells the
underwriter what question the concentration ACTUALLY asks. The
copy lives here rather than in the dossier template so it's
auditable as code (a copy change = a code review, not a hidden
template edit) and so the same copy renders identically across the
dossier, the per-merchant API, and any future PDF export.

The KEY reframes (from the design doc):

* ``international_client`` — durability question, NOT a fraud
  signal. The previous scorer flagged international wires as
  suspicious; the reframe is "X% from international counterparty(s)
  — would the counterparty continue paying?". Operators read this
  and decide; nothing auto-declines.
* ``processor`` — low durability concern (rails rarely vanish).
  Concern only triggers on rail-specific events (chargeback dispute,
  processor holding funds), which Track B catches as cash-flow
  signals, not Track C.
* ``end_customer`` — the GENUINE concentration risk. A merchant
  whose top revenue source is one named end customer lives or dies
  on that relationship.
* ``unknown`` — surfaced as a denominator warning, not a class row
  (the panel suppresses unknown from the by-class table; it appears
  only in the warnings list).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from aegis.counterparty.models import CounterpartyClass
from aegis.scoring_v2.track_c.models import PanelSeverity

# Severity thresholds. ``durability`` is the Track-C-specific
# attention marker for international_client + end_customer shares
# that warrant an underwriter question. ``review`` is a milder
# attention marker for processor concentration above ~80% (rare but
# worth noting). Below the thresholds, ``info`` (neutral panel).
_DURABILITY_SHARE_FLOOR_PCT: Final[Decimal] = Decimal("30.00")
_PROCESSOR_REVIEW_SHARE_FLOOR_PCT: Final[Decimal] = Decimal("80.00")


def _select_severity(
    counterparty: CounterpartyClass, share_pct: Decimal
) -> PanelSeverity:
    """Map class + share to a rendering hint. NOT a decline gate."""
    if counterparty in ("international_client", "end_customer"):
        return "durability" if share_pct >= _DURABILITY_SHARE_FLOOR_PCT else "info"
    if counterparty == "processor":
        return "review" if share_pct >= _PROCESSOR_REVIEW_SHARE_FLOOR_PCT else "info"
    return "info"


def _frame_share(share_pct: Decimal) -> str:
    """Format a share percentage for the framing copy."""
    return f"{share_pct.quantize(Decimal('0.1'))}%"


def frame_for_class(
    counterparty: CounterpartyClass, share_pct: Decimal
) -> tuple[str, PanelSeverity]:
    """Return ``(framing_copy, severity)`` for a class + share.

    The framing is intentionally underwriter-voice — it answers the
    "what question does this concentration ask?" the operator needs
    to bring to the deal.
    """
    severity = _select_severity(counterparty, share_pct)
    share = _frame_share(share_pct)

    if counterparty == "international_client":
        if severity == "durability":
            framing = (
                f"{share} of revenue from international counterparty(s). "
                "Durability question — would the counterparty continue "
                "paying? NOT a fraud signal; international wires here "
                "are real revenue."
            )
        else:
            framing = (
                f"{share} from international counterparty(s); informational."
            )
        return framing, severity

    if counterparty == "end_customer":
        if severity == "durability":
            framing = (
                f"{share} of revenue from named end customer(s). "
                "Genuine concentration risk — the deal lives or dies "
                "on this relationship. Review the named senders below."
            )
        else:
            framing = (
                f"{share} from named end customer(s); low concentration."
            )
        return framing, severity

    if counterparty == "processor":
        if severity == "review":
            framing = (
                f"{share} from payment-rail processor(s). Rails rarely "
                "disappear; concern triggers only on a rail-specific "
                "event (chargeback dispute, processor holding funds)."
            )
        else:
            framing = (
                f"{share} from payment-rail processor(s); low durability "
                "concern."
            )
        return framing, severity

    # Fallback for any future class added without a specific frame —
    # surface plainly rather than hide.
    return (f"{share} from {counterparty}; informational.", "info")


def frame_stress(
    top_class: CounterpartyClass,
    base_revenue: Decimal,
    stress_revenue: Decimal,
    revenue_drop_pct: Decimal,
) -> str:
    """Underwriter-voice copy for the stress view."""
    drop = _frame_share(revenue_drop_pct)
    if top_class == "international_client":
        what = "the international counterparty(s) stop paying"
    elif top_class == "end_customer":
        what = "the top end customer stops purchasing"
    elif top_class == "processor":
        what = "the top processor holds or disputes funds"
    else:
        what = f"the top {top_class} class stops contributing"

    return (
        f"Stress case: if {what}, revenue drops {drop} "
        f"(from ${base_revenue:,.0f} to ${stress_revenue:,.0f}). "
        "Underwriter compares stress revenue to deal remittance — "
        "this panel does not gate on the comparison."
    )


__all__ = ["frame_for_class", "frame_stress"]
