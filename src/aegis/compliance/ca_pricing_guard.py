"""SB 362 APR re-disclosure guard for outbound CA-merchant communications.

Source: ``docs/compliance/01_california.md``, section "SB 362 — what changed
effective 2026-01-01" → "Architectural impact for AEGIS":

    "match-preview displays, submission-package emails, quote summaries —
    all must include APR alongside any factor rate, daily payment, or
    financing amount. Build a content-generation hook that enforces this
    for CA-merchant communications."

Rule
----
After a specific offer is extended, every time AEGIS or a funder
communicates a charge, pricing metric, or financing amount in any form
(email, portal, sales call, term sheet revision), the APR must also be
stated using ``"annual percentage rate"`` or ``"APR"``.

Implementation
--------------
For any communication addressed to a California merchant, if the
communication body mentions any of the dossier's explicit pricing
triggers (``factor rate``, ``daily payment``, ``financing amount``), it
must also mention APR via ``"APR"`` or ``"annual percentage rate"``.
Failure raises ``CaPricingComplianceError`` cited to Cal. Fin. Code
§ 22806.

Non-CA merchants pass through unchanged. Communications without any
pricing keyword pass through unchanged (e.g. status updates, doc
requests, non-pricing operator notes).

Hook this into any outbound merchant communication generator (Zoho
sync templates, dashboard quote previews, submission-package email
builder) before transmission. Validation is intentionally cheap so it
runs on every send.
"""

from __future__ import annotations

import re
from typing import Final

from aegis.logger import get_logger

_log = get_logger(__name__)


class CaPricingComplianceError(RuntimeError):
    """Raised when a CA-merchant communication mentions pricing without APR.

    Cite: Cal. Fin. Code § 22806 (added by SB 362, effective 2026-01-01).
    """

    citation: Final[str] = "Cal. Fin. Code § 22806"

    def __init__(self, message: str) -> None:
        super().__init__(f"{message} ({self.citation})")


# Pricing triggers — verbatim from the dossier "Architectural impact"
# paragraph. Adding new triggers requires a dossier update first.
_PRICING_KEYWORD_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bfactor\s+rate\b", re.IGNORECASE),
    re.compile(r"\bdaily\s+payment\b", re.IGNORECASE),
    re.compile(r"\bfinancing\s+amount\b", re.IGNORECASE),
)

# APR mention — the regulation's permitted forms verbatim.
_APR_MENTION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\bAPR\b|\bannual\s+percentage\s+rate\b",
    re.IGNORECASE,
)


def validate_communication(*, merchant_state: str, body: str) -> None:
    """Validate an outbound merchant communication against SB 362.

    Parameters
    ----------
    merchant_state
        USPS code of the merchant's principal place of business. Only
        ``"CA"`` (case-insensitive) triggers the guard.
    body
        The communication body — email text, dashboard quote summary,
        submission-package excerpt. Markdown / plain text both fine; the
        regex matches on word boundaries.

    Raises
    ------
    CaPricingComplianceError
        When ``merchant_state == "CA"``, the body contains at least one
        pricing keyword, AND the body does not mention APR. Citation:
        Cal. Fin. Code § 22806.
    """
    if (merchant_state or "").upper() != "CA":
        return  # non-CA merchants are out of scope for SB 362.

    triggered: list[str] = [
        pat.pattern for pat in _PRICING_KEYWORD_PATTERNS if pat.search(body)
    ]
    if not triggered:
        return  # no pricing reference => no re-disclosure obligation.

    if _APR_MENTION_PATTERN.search(body):
        return  # APR is mentioned alongside pricing — compliant.

    _log.warning(
        "ca_pricing_guard.violation merchant_state=CA triggered_keywords=%s",
        triggered,
    )
    raise CaPricingComplianceError(
        "CA merchant communication mentions pricing "
        f"({', '.join(triggered)}) without APR / 'annual percentage rate'"
    )


__all__ = ["CaPricingComplianceError", "validate_communication"]
