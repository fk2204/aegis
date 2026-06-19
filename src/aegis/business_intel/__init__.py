"""Business-intel lookups layered on top of the parser + scorer.

UCC filings + previous-default search via Bedrock + web_search tool.
Soft signal only — findings surface as ``FunderMatch.soft_concerns``
but never gate a match.
"""

from aegis.business_intel.refresh import (
    ensure_ucc_check,
    refresh_ucc_for_merchant,
)
from aegis.business_intel.ucc_checker import (
    UCCResult,
    check_ucc_and_defaults,
)

__all__ = [
    "UCCResult",
    "check_ucc_and_defaults",
    "ensure_ucc_check",
    "refresh_ucc_for_merchant",
]
