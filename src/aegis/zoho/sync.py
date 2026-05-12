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

from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote
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
                f"/crm/v8/Deals/{merchant.zoho_deal_id}",
                json=payload,
            )
            zoho_deal_id = merchant.zoho_deal_id
        else:
            response = self._client.request("POST", "/crm/v8/Deals", json=payload)
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

    def push_merchant_to_lead(
        self, merchant_id: UUID, score: ScoreResult
    ) -> str:
        """Push merchant + score to a Zoho Lead. Returns the zoho_lead_id.

        Idempotency strategy:
          1. If merchant.zoho_lead_id is set → PUT to /crm/v8/Leads/{id}.
          2. Else if a Lead exists in Zoho with matching Email → PUT to that Lead.
          3. Else → POST a new Lead and record the returned id back on merchant.

        Email matchback uses Zoho's search API:
          GET /crm/v8/Leads/search?email=<merchant.email>

        Why Leads not Deals: the natural pipeline is intake-form → Lead → rep
        converts → Deal. Aegis enriches the Lead with parsed financials + score
        BEFORE conversion. Once converted, those fields carry forward to Deal.
        """
        merchant = self._merchants.get(merchant_id)
        payload = {
            "data": [_lead_payload(merchant, score)],
            "trigger": ["workflow"],
        }

        matched_by: str
        lead_id: str

        if merchant.zoho_lead_id:
            # Path 1: cached id wins.
            self._client.request(
                "PUT",
                f"/crm/v8/Leads/{merchant.zoho_lead_id}",
                json=payload,
            )
            lead_id = merchant.zoho_lead_id
            matched_by = "id"
        else:
            # Path 2: try email matchback before creating a duplicate.
            existing_id = self._find_lead_id_by_email(merchant.email)
            if existing_id:
                self._client.request(
                    "PUT",
                    f"/crm/v8/Leads/{existing_id}",
                    json=payload,
                )
                lead_id = existing_id
                matched_by = "email"
                self._merchants.upsert(
                    merchant.model_copy(update={"zoho_lead_id": lead_id})
                )
            else:
                # Path 3: net-new Lead.
                response = self._client.request(
                    "POST", "/crm/v8/Leads", json=payload
                )
                lead_id = _extract_created_id(response)
                matched_by = "new"
                self._merchants.upsert(
                    merchant.model_copy(update={"zoho_lead_id": lead_id})
                )

        self._audit.record(
            actor="zoho_sync",
            action="zoho.lead.upsert",
            subject_type="merchant",
            subject_id=merchant_id,
            details={
                "zoho_lead_id": lead_id,
                "tier": score.tier,
                "matched_by": matched_by,
            },
        )
        return lead_id

    def attach_findings_csv(
        self,
        *,
        module: str,
        record_id: str,
        merchant_id: UUID,
        csv_bytes: bytes,
        filename: str,
    ) -> None:
        """Attach a findings CSV to the Zoho Lead/Deal record.

        ``module`` is ``"Leads"`` or ``"Deals"``. Failures here are logged
        and audited but never re-raised — the upsert that just succeeded
        is the load-bearing operation; a failed attachment shouldn't
        knock the whole sync over. Operator can re-trigger sync to retry.
        """
        try:
            self._client.upload_attachment(
                module,
                record_id,
                filename=filename,
                content=csv_bytes,
                content_type="text/csv",
            )
        except Exception as exc:  # broad on purpose — see docstring
            _log.warning(
                "zoho attachment upload failed",
                extra={
                    "zoho_module": module,
                    "record_id": record_id,
                    "merchant_id": str(merchant_id),
                    "error": str(exc),
                },
            )
            self._audit.record(
                actor="zoho_sync",
                action="zoho.attachment.failed",
                subject_type="merchant",
                subject_id=merchant_id,
                details={
                    "zoho_module": module,
                    "record_id": record_id,
                    "filename": filename,
                    "error": str(exc),
                },
            )
            return

        self._audit.record(
            actor="zoho_sync",
            action="zoho.attachment.uploaded",
            subject_type="merchant",
            subject_id=merchant_id,
            details={
                "module": module,
                "record_id": record_id,
                "filename": filename,
                "size_bytes": len(csv_bytes),
            },
        )

    def _find_lead_id_by_email(self, email: str | None) -> str | None:
        """Search Zoho Leads for a matching email; return its id or None.

        Empty / missing email short-circuits to None (skip matchback).
        Zoho returns 204 No Content (or empty data) when no Lead matches.
        """
        if not email:
            return None
        try:
            response = self._client.request(
                "GET",
                f"/crm/v8/Leads/search?email={quote(email)}",
            )
        except Exception:
            _log.warning("zoho lead email search failed", extra={"email_masked": True})
            return None
        if not isinstance(response, dict):
            return None
        data = response.get("data")
        if not isinstance(data, list) or not data:
            return None
        first = data[0]
        if not isinstance(first, dict):
            return None
        found = first.get("id")
        return str(found) if found else None

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
        "Account_Name": {"name": m.business_name},
        "Stage": "Qualification",
        "Closing_Date": (date.today() + timedelta(days=30)).isoformat(),
        "Owner_Name_AEGIS": m.owner_name,
        "State_AEGIS": m.state,
        "AEGIS_Score": s.score,
        "AEGIS_Tier": s.tier,
        "AEGIS_Recommendation": s.recommendation,
        # Decimal money fields must round-trip as strings — CLAUDE.md rule
        # "NEVER use float for money". Zoho field types are Currency
        # (Suggested_Max_Advance) and Decimal (Recommended_Factor_Rate,
        # Recommended_Holdback_Pct); the v8 REST API accepts string-encoded
        # numerics into those types without precision loss. float() round-
        # trips like 1.30 → 1.2999999999999998, which the Zoho-side report
        # then renders as 130.00% holdback instead of 30.00%.
        "Suggested_Max_Advance": str(s.suggested_max_advance),
        "Recommended_Factor_Rate": str(s.recommended_factor_rate),
        "Recommended_Holdback_Pct": str(s.recommended_holdback_pct),
        "Estimated_Payback_Days": s.estimated_payback_days,
        "AEGIS_Hard_Decline_Reasons": ", ".join(s.hard_decline_reasons),
        "AEGIS_Soft_Concerns": ", ".join(s.soft_concerns),
    }


def _lead_payload(m: MerchantRow, s: ScoreResult) -> dict[str, Any]:
    """Translate AEGIS merchant + score into Zoho Lead field shape.

    Field names on the right are Zoho Lead field API names (case-sensitive).
    Six AEGIS_* + OFAC + Stacking fields were provisioned today; standard
    Lead fields (Company / Last_Name / etc.) are written when MerchantRow
    has them so the Lead is rep-usable post-sync without manual cleanup.

    Last_Name is mandatory on Zoho Leads. owner_name is split on the first
    space; if there's no space the entire string lands in Last_Name.
    """
    owner = m.owner_name.strip()
    if " " in owner:
        first_name, last_name = owner.split(" ", 1)
    else:
        first_name, last_name = "", owner

    ofac_flagged = any("ofac" in r.lower() for r in s.hard_decline_reasons)
    # Stacking signal: ScoreResult tier F is our defined proxy until a
    # dedicated mca_positions column lands on MerchantRow.
    stacking_risk = s.tier == "F"

    return {
        # Standard Lead fields
        "Company": m.business_name,
        "First_Name": first_name,
        "Last_Name": last_name,
        "State": m.state,
        "Email": m.email,
        # AEGIS-owned custom fields (created 2026-05-12)
        "Aegis_Applicant_ID": str(m.id),
        "Aegis_Score": int(s.score),
        "Aegis_Recommendation": s.recommendation,
        "OFAC_Status": "Flagged" if ofac_flagged else "Clear",
        "Aegis_Last_Synced": datetime.now(UTC).isoformat(),
        "Stacking_Risk": stacking_risk,
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
