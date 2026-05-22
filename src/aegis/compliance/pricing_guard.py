"""Per-state APR re-disclosure guard for outbound merchant communications.

Whenever a provider communicates a charge, pricing metric, or financing
amount to a merchant in a Tier 1 state with an APR-re-disclosure rule,
the communication must also state the APR.

States covered
--------------
* **California** (SB 362, effective 2026-01-01).
  Source: ``docs/compliance/01_california.md`` → "SB 362 — what changed
  effective 2026-01-01" → "Architectural impact for AEGIS":

      "match-preview displays, submission-package emails, quote summaries
      — all must include APR alongside any factor rate, daily payment,
      or financing amount. Build a content-generation hook that enforces
      this for CA-merchant communications."

  Cite: Cal. Fin. Code § 22806.

* **New York** (CFDL, baked into 23 NYCRR Part 600 from inception, not a
  later amendment).
  Source: ``docs/compliance/02_new_york.md`` → "What 23 NYCRR § 600.6
  requires for MCA disclosures" → "APR re-disclosure rule (§ 600.1 /
  § 600.3)":

      "During the application process AND after the specific offer is
      quoted, whenever the provider states a rate, finance charge, or
      financing amount, the provider must also state the APR. (This is
      the equivalent of CA's SB 362 rule but baked into NY's regulations
      from the start.)"

  Cite: 23 NYCRR § 600.1 / § 600.3.

Implementation
--------------
A state code resolves to a ``_StateRule`` (citation, error class,
trigger-keyword set, APR-mention pattern). For any communication
addressed to a covered state, if the body mentions a pricing trigger,
APR must also be mentioned. Failure raises the state-specific error
subclass with the state's citation. Non-covered states pass through.

Communications without a pricing trigger pass through unchanged
(status updates, doc requests, non-pricing operator notes).

Hook this into any outbound merchant communication generator (CRM
sync templates, dashboard quote previews, submission-package email
builder) before transmission.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from aegis.logger import get_logger

_log = get_logger(__name__)


class PricingComplianceError(RuntimeError):
    """Base class for per-state pricing-disclosure violations.

    Subclasses set ``citation`` to the controlling statute / regulation.
    The base class exists so callers can ``except PricingComplianceError``
    once across all covered states.
    """

    # Subclasses set this to their controlling statute / regulation.
    # Not ``Final`` on the base — subclass overrides are the whole point.
    citation: str = ""

    def __init__(self, message: str) -> None:
        super().__init__(f"{message} ({self.citation})")


class CaPricingComplianceError(PricingComplianceError):
    """Raised when a CA-merchant communication mentions pricing without APR.

    Cite: Cal. Fin. Code § 22806 (added by SB 362, effective 2026-01-01).
    """

    citation: str = "Cal. Fin. Code § 22806"


class NyPricingComplianceError(PricingComplianceError):
    """Raised when an NY-merchant communication mentions pricing without APR.

    Cite: 23 NYCRR § 600.1 / § 600.3 (CFDL, mandatory compliance
    2023-08-01).
    """

    citation: str = "23 NYCRR § 600.1 / § 600.3"


# Pricing triggers — same set across CA and NY. CA dossier enumerates
# "factor rate, daily payment, financing amount"; NY dossier describes
# "a rate, finance charge, or financing amount". The intersection is
# the same operational set: any quote-style pricing language fires the
# rule. Adding new triggers requires a dossier update first.
_PRICING_KEYWORD_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bfactor\s+rate\b", re.IGNORECASE),
    re.compile(r"\bdaily\s+payment\b", re.IGNORECASE),
    re.compile(r"\bfinancing\s+amount\b", re.IGNORECASE),
    re.compile(r"\bfinance\s+charge\b", re.IGNORECASE),
)

# APR mention — the regulation's permitted forms verbatim. Same for both
# states; both regimes accept either "APR" or "annual percentage rate".
_APR_MENTION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\bAPR\b|\bannual\s+percentage\s+rate\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _StateRule:
    error_cls: type[PricingComplianceError]
    state_label: str  # for log + error message disambiguation


_STATE_RULES: Final[dict[str, _StateRule]] = {
    "CA": _StateRule(error_cls=CaPricingComplianceError, state_label="CA"),
    "NY": _StateRule(error_cls=NyPricingComplianceError, state_label="NY"),
}


def validate_communication(*, merchant_state: str, body: str) -> None:
    """Validate an outbound merchant communication against APR re-disclosure.

    Parameters
    ----------
    merchant_state
        USPS code of the merchant's principal place of business.
        Case-insensitive. Only states in the rule registry trigger the
        guard (currently CA, NY).
    body
        The communication body — email text, dashboard quote summary,
        submission-package excerpt. Markdown / plain text both fine; the
        regex matches on word boundaries.

    Raises
    ------
    CaPricingComplianceError
        For CA merchants when pricing is mentioned without APR.
    NyPricingComplianceError
        For NY merchants when pricing is mentioned without APR.

    Both subclass ``PricingComplianceError``; callers can ``except``
    either the base or the specific subclass.
    """
    rule = _STATE_RULES.get((merchant_state or "").upper())
    if rule is None:
        return  # state has no APR re-disclosure rule.

    triggered: list[str] = [
        pat.pattern for pat in _PRICING_KEYWORD_PATTERNS if pat.search(body)
    ]
    if not triggered:
        return  # no pricing reference => no re-disclosure obligation.

    if _APR_MENTION_PATTERN.search(body):
        return  # APR is mentioned alongside pricing — compliant.

    _log.warning(
        "pricing_guard.violation merchant_state=%s triggered_keywords=%s",
        rule.state_label,
        triggered,
    )
    raise rule.error_cls(
        f"{rule.state_label} merchant communication mentions pricing "
        f"({', '.join(triggered)}) without APR / 'annual percentage rate'"
    )


__all__ = [
    "CaPricingComplianceError",
    "NyPricingComplianceError",
    "PricingComplianceError",
    "validate_communication",
]
