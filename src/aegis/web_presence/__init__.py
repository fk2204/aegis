"""Web-presence reputation scan.

Soft signal layered on top of the existing scoring + matching tracks.
Invokes Claude on Bedrock with the ``web_search_20250305`` tool to look
up a merchant's online footprint and surface red flags (BBB complaints,
negative reviews, signs of closure, etc). Never used as a hard gate.
"""

from aegis.web_presence.refresh import (
    ensure_web_presence_scan,
    refresh_web_presence_for_merchant,
)
from aegis.web_presence.scanner import (
    WebPresenceResult,
    scan_web_presence,
)

__all__ = [
    "WebPresenceResult",
    "ensure_web_presence_scan",
    "refresh_web_presence_for_merchant",
    "scan_web_presence",
]
