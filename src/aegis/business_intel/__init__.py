"""Business-intel lookups layered on top of the parser + scorer.

UCC filings + previous-default search via Bedrock + web_search tool.
Soft signal only — findings surface as ``FunderMatch.soft_concerns``
but never gate a match.
"""

from aegis.business_intel.bankruptcy_checker import (
    BankruptcyResult,
    check_bankruptcy,
)
from aegis.business_intel.bankruptcy_refresh import (
    ensure_bankruptcy_check,
    refresh_bankruptcy_for_merchant,
)
from aegis.business_intel.refresh import (
    ensure_ucc_check,
    refresh_ucc_for_merchant,
)
from aegis.business_intel.ucc_checker import (
    UCCResult,
    check_ucc_and_defaults,
)

__all__ = [
    "BankruptcyResult",
    "UCCResult",
    "check_bankruptcy",
    "check_ucc_and_defaults",
    "ensure_bankruptcy_check",
    "ensure_ucc_check",
    "refresh_bankruptcy_for_merchant",
    "refresh_ucc_for_merchant",
]
