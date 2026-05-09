"""Zoho CRM integration.

Three pieces:

  * ``client.ZohoClient`` — OAuth refresh + retrying httpx calls.
  * ``sync.ZohoSync`` — outbound (AEGIS merchant + score → Zoho Deal)
    and inbound (Zoho Deal → AEGIS merchant), idempotent on
    ``zoho_deal_id``.
  * ``aegis.api.routes.webhooks_zoho`` — HMAC + freshness-checked
    webhook receiver that funnels into ``ZohoSync.apply_inbound``.
"""

from __future__ import annotations

from aegis.zoho.client import ZohoAuthError, ZohoClient, ZohoError
from aegis.zoho.sync import ZohoSync

__all__ = ["ZohoAuthError", "ZohoClient", "ZohoError", "ZohoSync"]
