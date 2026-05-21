"""Close CRM outbound sync — write AEGIS decisions back to Lead fields.

Single entry point: ``push_decision_to_close``. The function is the
load-bearing piece for idempotency guarantee #4 from the design doc:
read the Lead first, compare current vs desired values across the 4
Aegis-* business fields, PATCH only if a business field actually
differs. ``Aegis Last Synced`` is informational — it changes on every
PATCH, but a change in it alone is never a reason to PATCH.

Scope (per step 5):

* Outbound write-back of decision data to Close Lead custom fields.
* No business logic beyond the value-aware diff. The caller decides
  WHEN to call (step 6 wires it from the operator-triggered sync
  route).
* No pipeline status transitions. No custom-activity writes.
* No coupling to the snapshot writer or scoring code — accepts
  primitives so any source can drive it.

Audit contract:

* ``close.lead.sync_attempted`` on every call (regardless of whether a
  PATCH fires). ``details.patched`` (bool) + ``details.fields_diffed``
  (list[str]) makes the outcome searchable.
* ``close.lead.sync_failed_not_found`` when the Lead has been deleted
  in Close (404). Returns without raising — operator-visible signal,
  not a crash.
* PATCH failure (4xx other than 404, or 5xx after retries): writes
  ``close.lead.sync_attempted`` with ``patched=False`` and the response
  status, THEN re-raises so the caller can fail the operation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from aegis.close.client import CloseClient, CloseError
from aegis.close.field_map import CLOSE_FIELD_IDS
from aegis.logger import get_logger

if TYPE_CHECKING:
    from aegis.audit import AuditLog

_log = get_logger(__name__)


# Recommendation mapping: AEGIS decision literal → Close choice label.
# DecisionPayload.decision is one of {"approve", "decline", "manual_review",
# "redisclosure"}. The first three map to Close; "redisclosure" is NOT a
# recommendation transition and must not be pushed via this path — the
# function raises if asked.
_RECOMMENDATION_MAP: dict[str, str] = {
    "approve": "Approve",
    "decline": "Decline",
    "manual_review": "Refer",
}


# OFAC status literal exposed by ``derive_ofac_status``.
OfacStatus = Literal["Clear", "Flagged", "Pending"]


class SyncError(RuntimeError):
    """Caller-facing error for outbound sync failures (other than the
    Lead-deleted 404 case which is signalled via SyncResult)."""


@dataclass(frozen=True)
class SyncResult:
    """Returned by ``push_decision_to_close`` to describe the outcome
    without needing to parse the audit row.

    ``reason`` is one of:
      * ``"patched"`` — PATCH fired, fields_diffed populated
      * ``"no_diff"`` — Lead already matched desired state, no PATCH
      * ``"lead_not_found"`` — Lead deleted in Close (404 from GET)
    """

    patched: bool
    fields_diffed: list[str]
    reason: str


# ---------------------------------------------------------------------------
# Public helper — derive OFAC status from a decision
# ---------------------------------------------------------------------------


def derive_ofac_status(
    *,
    decision_reason_codes: list[str],
    ofac_cache_timestamp: datetime | None,
) -> OfacStatus:
    """Pure helper. Compute the Close ``OFAC Status`` choice value from
    a stored decision.

    Rules (per design doc decision-to-OFAC mapping):
      * ``"ofac_sanctions_match"`` in reason codes -> ``"Flagged"``
      * Otherwise, if ``ofac_cache_timestamp`` is set -> ``"Clear"``
        (the check ran, no match)
      * Otherwise (no timestamp; OFAC not yet checked) -> ``"Pending"``
    """
    if "ofac_sanctions_match" in decision_reason_codes:
        return "Flagged"
    if ofac_cache_timestamp is not None:
        return "Clear"
    return "Pending"


# ---------------------------------------------------------------------------
# Public — push decision
# ---------------------------------------------------------------------------


def push_decision_to_close(
    *,
    close_lead_id: str,
    decision_id: UUID,
    score: Decimal | int | None,
    recommendation: str,
    ofac_status: OfacStatus,
    client: CloseClient,
    audit: AuditLog,
    now: datetime | None = None,
) -> SyncResult:
    """PATCH the 4 Aegis-* custom fields onto a Close Lead, idempotently.

    ``recommendation`` accepts the AEGIS DecisionPayload literal —
    ``"approve" | "decline" | "manual_review"``. Other values raise
    ``SyncError``. ``"redisclosure"`` is intentionally rejected; that's
    not a recommendation transition.

    ``score`` may be ``Decimal`` (from a DecisionPayload), ``int``, or
    ``None``. Stored on Close as an integer (Close's ``Aegis Score`` is a
    number field; we normalize to int to keep diff comparison clean).

    ``ofac_status`` is the already-derived Close choice value. Callers
    that have a stored decision should use ``derive_ofac_status()``.

    ``now`` is injectable for deterministic tests; defaults to
    ``datetime.now(tz=UTC)``.

    Returns ``SyncResult``. Raises ``CloseError`` on PATCH failure
    (4xx other than 404, or 5xx after retries). Does NOT raise on 404
    from GET — that's signalled via ``SyncResult(reason="lead_not_found")``.
    """
    now = now or datetime.now(tz=UTC)
    aegis_recommendation = _map_recommendation(recommendation)
    desired_score = _normalize_score(score)
    desired_applicant_id_default = str(decision_id)

    # 1) GET the Lead. 404 -> graceful return; other errors propagate.
    try:
        lead = client.get_lead(close_lead_id)
    except CloseError as exc:
        if exc.status_code == 404:
            audit.record(
                actor="close_sync",
                action="close.lead.sync_failed_not_found",
                details={
                    "close_lead_id": close_lead_id,
                    "decision_id": str(decision_id),
                    "status_code": 404,
                },
            )
            return SyncResult(
                patched=False, fields_diffed=[], reason="lead_not_found"
            )
        raise

    # 2) Read current values for the 4 business fields + Applicant ID.
    cur_applicant_id = _read_field(lead, "aegis_applicant_id")
    cur_score = _read_field(lead, "aegis_score")
    cur_recommendation = _read_field(lead, "aegis_recommendation")
    cur_ofac = _read_field(lead, "ofac_status")

    # 3) Desired values. Applicant ID is set-once: if Close already has
    #    one, NEVER overwrite (the operator's stable reference). If
    #    Close has none, the current decision_id wins.
    if isinstance(cur_applicant_id, str) and cur_applicant_id.strip():
        desired_applicant_id = cur_applicant_id
    else:
        desired_applicant_id = desired_applicant_id_default

    # 4) Diff. Aegis Last Synced is NOT part of the diff — that's the
    #    whole point of value-aware sync.
    diffs: dict[str, Any] = {}
    if cur_applicant_id != desired_applicant_id:
        diffs["aegis_applicant_id"] = desired_applicant_id
    if cur_score != desired_score:
        diffs["aegis_score"] = desired_score
    if cur_recommendation != aegis_recommendation:
        diffs["aegis_recommendation"] = aegis_recommendation
    if cur_ofac != ofac_status:
        diffs["ofac_status"] = ofac_status

    if not diffs:
        # No business-field diff. Audit the attempt; do not PATCH.
        audit.record(
            actor="close_sync",
            action="close.lead.sync_attempted",
            details={
                "close_lead_id": close_lead_id,
                "decision_id": str(decision_id),
                "fields_diffed": [],
                "patched": False,
            },
        )
        return SyncResult(patched=False, fields_diffed=[], reason="no_diff")

    # 5) Build PATCH payload — only diffed business fields + Last Synced.
    patch_body: dict[str, Any] = {}
    for aegis_name, desired_value in diffs.items():
        patch_body[f"custom.{CLOSE_FIELD_IDS[aegis_name]}"] = desired_value
    patch_body[
        f"custom.{CLOSE_FIELD_IDS['aegis_last_synced']}"
    ] = now.isoformat()

    fields_diffed_sorted = sorted(diffs.keys())

    try:
        client.update_lead_custom_fields(close_lead_id, patch_body)
    except CloseError as exc:
        audit.record(
            actor="close_sync",
            action="close.lead.sync_attempted",
            details={
                "close_lead_id": close_lead_id,
                "decision_id": str(decision_id),
                "fields_diffed": fields_diffed_sorted,
                "patched": False,
                "error_status": exc.status_code,
                "error": str(exc)[:200],
            },
        )
        raise

    audit.record(
        actor="close_sync",
        action="close.lead.sync_attempted",
        details={
            "close_lead_id": close_lead_id,
            "decision_id": str(decision_id),
            "fields_diffed": fields_diffed_sorted,
            "patched": True,
            "synced_at": now.isoformat(),
        },
    )
    return SyncResult(
        patched=True, fields_diffed=fields_diffed_sorted, reason="patched"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _map_recommendation(recommendation: str) -> str:
    """AEGIS decision literal -> Close choice. Unknown values raise."""
    if recommendation not in _RECOMMENDATION_MAP:
        raise SyncError(
            f"recommendation {recommendation!r} cannot be pushed to Close; "
            f"expected one of {sorted(_RECOMMENDATION_MAP)}"
        )
    return _RECOMMENDATION_MAP[recommendation]


def _normalize_score(score: Decimal | int | None) -> int | None:
    """Close's Aegis Score is a number field. Normalize Decimal/None to
    int/None so diff comparison is stable across types (Close round-trips
    a number as int when there's no fractional part)."""
    if score is None:
        return None
    if isinstance(score, Decimal):
        return int(score)
    return int(score)


def _read_field(lead: dict[str, Any], aegis_name: str) -> Any:  # noqa: ANN401 — Close values are heterogeneous
    """Read a Close custom-field value from a Lead payload."""
    return lead.get(f"custom.{CLOSE_FIELD_IDS[aegis_name]}")


__all__ = [
    "OfacStatus",
    "SyncError",
    "SyncResult",
    "derive_ofac_status",
    "push_decision_to_close",
]
