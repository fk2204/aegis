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

    def record_funder_submission(
        self,
        *,
        merchant_id: UUID,
        funder_names: list[str],
        zip_bytes: bytes,
        zip_filename: str,
        dossier_pdf: bytes | None = None,
        dossier_filename: str | None = None,
    ) -> None:
        """Reflect an AEGIS funder submission on the Zoho Deal record.

        Best-effort. The audit row written by the caller in AEGIS is the
        authoritative record; any Zoho failure is logged + audited here
        but never raised, so a Zoho outage cannot block the operator's
        submission package download.

        Operations:
          1. Update the Deal: ``Date_Submitted_to_Lenders`` + ``Lenders_Submitted_To``.
          2. For each lender by name: bump ``Total_Submissions`` and set
             ``Last_Submission_Date`` on the Lender record.
          3. Attach the submission ZIP to the Deal as a file attachment.

        Skips entirely (with a single audit row) if the merchant has no
        ``zoho_deal_id`` — operator must push to Zoho first to wire the
        CRM record. No Deal is created from the submission path.
        """
        merchant = self._merchants.get(merchant_id)
        if not merchant.zoho_deal_id:
            self._audit.record(
                actor="zoho_sync",
                action="zoho.submission.skipped_no_deal",
                subject_type="merchant",
                subject_id=merchant_id,
                details={
                    "funder_names": funder_names,
                    "reason": "merchant has no zoho_deal_id; push to Zoho first",
                },
            )
            return

        deal_id = merchant.zoho_deal_id
        today = date.today().isoformat()

        # 1. Deal-level update.
        #
        # ``Lenders_Submitted_To`` stays as the integer count (its Zoho
        # field type is numeric in our layout). The lender names go into
        # ``Description`` between idempotent ``[AEGIS-SUBMISSIONS]``
        # markers so the operator sees actual lender names on the Deal
        # page without us clobbering operator-typed notes around them.
        description = self._compose_submission_description(
            deal_id=deal_id, names=funder_names, today=today
        )
        try:
            self._client.request(
                "PUT",
                f"/crm/v8/Deals/{deal_id}",
                json={
                    "data": [
                        {
                            "Date_Submitted_to_Lenders": today,
                            "Lenders_Submitted_To": len(funder_names),
                            "Description": description,
                        }
                    ]
                },
            )
            self._audit.record(
                actor="zoho_sync",
                action="zoho.deal.submission_marked",
                subject_type="merchant",
                subject_id=merchant_id,
                details={
                    "zoho_deal_id": deal_id,
                    "lender_count": len(funder_names),
                    "date": today,
                },
            )
        except Exception as exc:  # broad on purpose — best-effort CRM update
            _log.warning(
                "zoho deal submission update failed",
                extra={"deal_id": deal_id, "error": str(exc)},
            )
            self._audit.record(
                actor="zoho_sync",
                action="zoho.deal.submission_failed",
                subject_type="merchant",
                subject_id=merchant_id,
                details={"zoho_deal_id": deal_id, "error": str(exc)},
            )

        # 2. Per-lender counters.
        for name in funder_names:
            lender_id, prior_total = self._lookup_lender_counters(name)
            if lender_id is None:
                self._audit.record(
                    actor="zoho_sync",
                    action="zoho.lender.lookup_failed",
                    subject_type="merchant",
                    subject_id=merchant_id,
                    details={"lender_name": name},
                )
                continue
            try:
                self._client.request(
                    "PUT",
                    f"/crm/v8/Lenders/{lender_id}",
                    json={
                        "data": [
                            {
                                "Total_Submissions": prior_total + 1,
                                "Last_Submission_Date": today,
                            }
                        ]
                    },
                )
                self._audit.record(
                    actor="zoho_sync",
                    action="zoho.lender.submission_counted",
                    subject_type="merchant",
                    subject_id=merchant_id,
                    details={
                        "lender_id": lender_id,
                        "lender_name": name,
                        "total_submissions_after": prior_total + 1,
                    },
                )
            except Exception as exc:
                _log.warning(
                    "zoho lender update failed",
                    extra={"lender_id": lender_id, "error": str(exc)},
                )
                self._audit.record(
                    actor="zoho_sync",
                    action="zoho.lender.update_failed",
                    subject_type="merchant",
                    subject_id=merchant_id,
                    details={
                        "lender_id": lender_id,
                        "lender_name": name,
                        "error": str(exc),
                    },
                )

        # 3. ZIP attachment on the Deal.
        self.attach_findings_csv(
            module="Deals",
            record_id=deal_id,
            merchant_id=merchant_id,
            csv_bytes=zip_bytes,
            filename=zip_filename,
        )

        # 4. Optional PDF dossier attachment alongside the submission ZIP.
        # Same best-effort posture — attachment failures are audited but
        # never raised; AEGIS audit log is the authoritative record.
        if dossier_pdf is not None and dossier_filename is not None:
            self.attach_findings_csv(
                module="Deals",
                record_id=deal_id,
                merchant_id=merchant_id,
                csv_bytes=dossier_pdf,
                filename=dossier_filename,
            )

    _AEGIS_DESC_BEGIN = "[AEGIS-SUBMISSIONS]"
    _AEGIS_DESC_END = "[/AEGIS-SUBMISSIONS]"

    def _compose_submission_description(
        self, *, deal_id: str, names: list[str], today: str
    ) -> str:
        """Read the Deal's current Description, swap our marker block in
        place, return the new value.

        Idempotent — re-running the submission strips the prior
        ``[AEGIS-SUBMISSIONS]`` block and writes a fresh one. Operator
        free-text outside the markers is preserved.
        """
        try:
            resp = self._client.request("GET", f"/crm/v8/Deals/{deal_id}")
        except Exception:
            existing = ""
        else:
            rows = resp.get("data") or []
            existing = (rows[0].get("Description") or "") if rows else ""

        prefix = existing
        begin = self._AEGIS_DESC_BEGIN
        end = self._AEGIS_DESC_END
        if begin in existing and end in existing:
            head, _, tail = existing.partition(begin)
            _, _, after = tail.partition(end)
            prefix = (head.rstrip() + "\n" + after.lstrip("\n")).strip()

        names_line = ", ".join(names) if names else "(no lenders)"
        block = (
            f"{begin}\n"
            f"Submitted {today} via AEGIS to: {names_line}\n"
            f"{end}"
        )
        return f"{prefix}\n\n{block}".lstrip() if prefix else block

    def _lookup_lender_counters(self, name: str) -> tuple[str | None, int]:
        """Find a Zoho Lender by Name and return (id, current Total_Submissions).

        Returns ``(None, 0)`` if the Lender is not found or the search
        fails. ``Total_Submissions`` defaults to 0 when the field is null
        on the Zoho side — typical for a freshly-imported Lender.
        """
        try:
            resp = self._client.request(
                "GET",
                f"/crm/v8/Lenders/search?"
                f"criteria=(Name:equals:{quote(name)})"
                f"&fields=Name,id,Total_Submissions",
            )
        except Exception:
            return (None, 0)
        if not isinstance(resp, dict):
            return (None, 0)
        data = resp.get("data") or []
        if not data or not isinstance(data[0], dict):
            return (None, 0)
        row = data[0]
        lender_id = row.get("id")
        prior = row.get("Total_Submissions")
        prior_int = 0
        if isinstance(prior, int):
            prior_int = prior
        elif isinstance(prior, str) and prior.isdigit():
            prior_int = int(prior)
        return (str(lender_id) if lender_id else None, prior_int)

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
