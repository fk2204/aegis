"""Outbound + inbound Zoho sync.

Outbound (AEGIS → Zoho)
-----------------------
``push_merchant_with_score`` builds a Deal payload from an AEGIS merchant
plus the latest ``ScoreResult``, then upserts the deal in Zoho's
``Deals`` module. The merchant's ``zoho_deal_id`` is the dedupe key —
absent ⇒ create + record id; present ⇒ update.

Inbound (Zoho → AEGIS)
----------------------
``apply_inbound`` accepts a Zoho webhook payload (one Deal record),
extracts merchant identity, and upserts a ``MerchantRow`` in the
repository. Idempotent on ``zoho_deal_id``: a re-fired webhook updates
in place rather than creating a duplicate.

PII
---
Merchant identity (business_name, owner_name) flows through the Zoho
API in plaintext — that's the integration's purpose. Local logging of
the payload is masked through the project logger.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from aegis.audit import AuditLog
from aegis.logger import get_logger
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import MerchantRepository
from aegis.scoring.models import ScoreResult
from aegis.zoho.client import ZohoClient

_log = get_logger(__name__)


class ZohoSyncError(RuntimeError):
    """Raised when sync cannot complete after retries."""


class ZohoSync:
    """Bridges AEGIS merchants ↔ Zoho Deals."""

    def __init__(
        self,
        *,
        client: ZohoClient,
        merchants: MerchantRepository,
        audit: AuditLog,
    ) -> None:
        self._client = client
        self._merchants = merchants
        self._audit = audit

    # Outbound ----------------------------------------------------------------

    def push_merchant_with_score(
        self, merchant_id: UUID, score: ScoreResult
    ) -> str:
        """Push merchant + score to Zoho. Returns the resolved zoho_deal_id."""
        merchant = self._merchants.get(merchant_id)
        payload = {
            "data": [_deal_payload(merchant, score)],
            "trigger": ["workflow"],
        }

        if merchant.zoho_deal_id:
            response = self._client.request(
                "PUT",
                f"/crm/v6/Deals/{merchant.zoho_deal_id}",
                json=payload,
            )
            zoho_deal_id = merchant.zoho_deal_id
        else:
            response = self._client.request("POST", "/crm/v6/Deals", json=payload)
            zoho_deal_id = _extract_created_id(response)
            updated = merchant.model_copy(update={"zoho_deal_id": zoho_deal_id})
            self._merchants.upsert(updated)

        self._audit.record(
            actor="zoho_sync",
            action="zoho.deal.upsert",
            subject_type="merchant",
            subject_id=merchant_id,
            details={"zoho_deal_id": zoho_deal_id, "tier": score.tier},
        )
        return zoho_deal_id

    # Inbound -----------------------------------------------------------------

    def apply_inbound(self, deal_record: dict[str, Any]) -> MerchantRow:
        """Idempotent upsert from a Zoho webhook deal payload."""
        zoho_deal_id = str(deal_record.get("id") or "").strip()
        if not zoho_deal_id:
            raise ZohoSyncError("inbound deal record missing 'id'")

        existing = self._merchants.find_by_zoho_deal_id(zoho_deal_id)
        merchant = _deal_record_to_merchant(deal_record, existing=existing)
        saved = self._merchants.upsert(merchant)

        self._audit.record(
            actor="zoho_sync",
            action="zoho.deal.inbound",
            subject_type="merchant",
            subject_id=saved.id,
            details={"zoho_deal_id": zoho_deal_id, "is_update": existing is not None},
        )
        return saved


# --- helpers -----------------------------------------------------------------


def _deal_payload(m: MerchantRow, s: ScoreResult) -> dict[str, Any]:
    """Translate AEGIS merchant + score into Zoho Deal field shape.

    Field names on the right are Zoho field API names (case-sensitive).
    """
    return {
        "Deal_Name": f"{m.business_name} — AEGIS {s.tier}",
        "Account_Name": m.business_name,
        "Owner_Name_AEGIS": m.owner_name,
        "State_AEGIS": m.state,
        "AEGIS_Score": s.score,
        "AEGIS_Tier": s.tier,
        "AEGIS_Recommendation": s.recommendation,
        "Suggested_Max_Advance": str(s.suggested_max_advance),
        "Recommended_Factor_Rate": str(s.recommended_factor_rate),
        "Recommended_Holdback_Pct": str(s.recommended_holdback_pct),
        "Estimated_Payback_Days": s.estimated_payback_days,
        "AEGIS_Hard_Decline_Reasons": ", ".join(s.hard_decline_reasons),
        "AEGIS_Soft_Concerns": ", ".join(s.soft_concerns),
    }


def _extract_created_id(response: dict[str, Any]) -> str:
    """Pull the new Deal id from a Zoho create response.

    Shape: ``{"data": [{"code": "SUCCESS", "details": {"id": "...", ...}}]}``.
    """
    data = response.get("data")
    if not isinstance(data, list) or not data:
        raise ZohoSyncError(f"unexpected zoho create response: {response!r}")
    first = data[0]
    if not isinstance(first, dict):
        raise ZohoSyncError(f"unexpected zoho data row: {first!r}")
    if first.get("code") != "SUCCESS":
        raise ZohoSyncError(f"zoho create failed: {first!r}")
    details = first.get("details") or {}
    new_id = details.get("id") if isinstance(details, dict) else None
    if not new_id:
        raise ZohoSyncError(f"zoho create response missing id: {response!r}")
    return str(new_id)


def _deal_record_to_merchant(
    record: dict[str, Any],
    *,
    existing: MerchantRow | None,
) -> MerchantRow:
    """Map a Zoho Deal record to a (possibly updated) MerchantRow.

    Preserves the AEGIS-side merchant id when updating an existing row so
    foreign-key references (analyses, transactions) stay intact.
    """
    business = str(record.get("Account_Name") or "").strip() or "Unknown"
    owner = str(record.get("Owner_Name_AEGIS") or "").strip() or "Unknown"
    state = str(record.get("State_AEGIS") or "").strip().upper()
    if len(state) != 2:
        # Default to a placeholder; the caller's audit will surface the issue.
        state = existing.state if existing else "CA"
    zoho_deal_id = str(record["id"])

    base = existing or MerchantRow(
        business_name=business,
        owner_name=owner,
        state=state,
        zoho_deal_id=zoho_deal_id,
    )
    return base.model_copy(
        update={
            "business_name": business,
            "owner_name": owner,
            "state": state,
            "zoho_deal_id": zoho_deal_id,
        }
    )


__all__ = ["ZohoSync", "ZohoSyncError"]
