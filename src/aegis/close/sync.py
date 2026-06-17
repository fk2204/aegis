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
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from aegis.close.client import CloseClient, CloseError
from aegis.close.field_map import CLOSE_FIELD_IDS, CLOSE_OPPORTUNITY_FIELD_IDS
from aegis.logger import get_logger

if TYPE_CHECKING:
    from aegis.audit import AuditLog
    from aegis.merchants.models import MerchantRow

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
    merchant: MerchantRow | None = None,
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
            return SyncResult(patched=False, fields_diffed=[], reason="lead_not_found")
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
    patch_body[f"custom.{CLOSE_FIELD_IDS['aegis_last_synced']}"] = now.isoformat()

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

    # Operator-action task: when the merchant has no credit score on
    # file, prompt the operator via a one-shot Close task. Guarded by
    # an audit-log dedupe so a sync that re-runs doesn't pile up
    # duplicate tasks in Close. Best-effort: a Close 4xx/5xx on the
    # task POST is logged via the audit row but does NOT raise — the
    # core sync already succeeded.
    if merchant is not None and merchant.credit_score is None:
        _maybe_create_credit_score_task(
            merchant=merchant,
            close_lead_id=close_lead_id,
            client=client,
            audit=audit,
            now=now,
        )

    return SyncResult(patched=True, fields_diffed=fields_diffed_sorted, reason="patched")


def _maybe_create_credit_score_task(
    *,
    merchant: MerchantRow,
    close_lead_id: str,
    client: CloseClient,
    audit: AuditLog,
    now: datetime,
) -> None:
    """Create a one-shot Close task asking the operator to pull credit.

    Dedupe key is the audit row ``close.task.credit_score_requested``
    on this merchant. The first time a sync runs with credit_score
    missing, we write the task and stamp the audit row; subsequent
    syncs see the audit row and skip.
    """
    prior = audit.list_for_subject(
        subject_type="merchant",
        subject_id=merchant.id,
        action="close.task.credit_score_requested",
    )
    if prior:
        return

    due = (now or datetime.now(tz=UTC)).date() + timedelta(days=1)
    text = f"Pull credit score for {merchant.business_name}"
    try:
        client.create_task(lead_id=close_lead_id, text=text, due_date=due)
    except CloseError as exc:
        audit.record(
            actor="close_sync",
            action="close.task.credit_score_request_failed",
            subject_type="merchant",
            subject_id=merchant.id,
            details={
                "close_lead_id": close_lead_id,
                "status_code": exc.status_code,
                "error": str(exc)[:200],
            },
        )
        return

    audit.record(
        actor="close_sync",
        action="close.task.credit_score_requested",
        subject_type="merchant",
        subject_id=merchant.id,
        details={
            "close_lead_id": close_lead_id,
            "task_text": text,
            "due_date": due.isoformat(),
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Public — push offer to opportunity
# ---------------------------------------------------------------------------


def push_offer_to_opportunity(
    *,
    close_opportunity_id: str,
    decision_id: UUID,
    suggested_max_advance: Decimal | None,
    recommended_factor_rate: Decimal | None,
    recommended_holdback_pct: Decimal | None,
    true_revenue_monthly: Decimal | None,
    holdback_capacity_monthly: Decimal | None,
    existing_mca_count: int | None,
    existing_mca_daily_total: Decimal | None,
    client: CloseClient,
    audit: AuditLog,
    opportunity_payload: dict[str, Any] | None = None,
) -> SyncResult:
    """PATCH the AEGIS offer + supporting cashflow snapshot onto a Close
    Opportunity, idempotently.

    Mirrors :func:`push_decision_to_close`'s read-before-write diff
    contract: GET the opportunity, compare current vs desired across
    the seven Aegis-driven Opportunity custom fields, PATCH only if at
    least one value differs.

    Field mapping (operator-confirmed 2026-06-15 via
    ``find_opportunity_custom_fields`` MCP query):

      ============================  ===========================================
      AEGIS source                  Close Opportunity custom field
      ============================  ===========================================
      ``OfferRecommendation``       ``Suggested Max Advance``
       .recommended_amount          (cf_XMtBI8Of38ic...)
      ``ScoreResult``               ``Recommended Factor Rate``
       .recommended_factor_rate     (cf_flsrZT0fTjNo...)
      ``OfferRecommendation``       ``Recommended Holdback Pct``
       .holdback_pct                (cf_mJxOH8wrNd4K...)
      ``AnalysisRow``               ``True Revenue``
       .monthly_revenue             (cf_DivasTofPYPO...)
      route default                 ``Holdback Capacity``
       (revenue * 0.25 in v1;       (cf_B3gXaET1Hzhf...)
       Close-sourced later)
      ``MCAStackAggregation``       ``Existing MCA Debits Identified``
       .active_mca_count            (cf_exJtKEjItwsJ...)
      ``AnalysisRow``               ``Existing MCA Daily Debits Total``
       .mca_daily_total             (cf_xOiXpJtf1W9D...)
      ============================  ===========================================

    All seven values are nullable inputs. ``None`` means "AEGIS doesn't
    have a value for this merchant yet" — the diff treats ``None`` as
    "no desired value" and SKIPS that field (leaves whatever's on the
    Close side alone). This matches the contract for the offer-sizing
    surface: a non-finalized merchant produces no scores and no offer,
    so partial syncs are normal.

    Returns ``SyncResult``:

      * ``"patched"``           PATCH fired, fields_diffed populated.
      * ``"no_diff"``           every defined field already matched.
      * ``"opportunity_not_found"`` 404 from the initial GET.

    Audit contract mirrors ``push_decision_to_close``:

      * ``close.opportunity.sync_attempted`` on every call with
        ``details.patched`` (bool) + ``details.fields_diffed`` (list[str]).
      * ``close.opportunity.sync_failed_not_found`` on 404.
      * PATCH failure (4xx other than 404, or 5xx after retries): same
        ``sync_attempted`` row with ``patched=False`` + status code,
        then re-raise.

    Decision-boundary posture: SHADOW ONLY. The offer + capacity
    figures feed the underwriter's view; they do NOT participate in
    AEGIS's live decline path. See ``scoring_v2/offer.py`` module
    docstring.

    ``opportunity_payload``: callers that already fetched the
    opportunity (e.g. to read the operator-entered ``Holdback Capacity``
    before computing the offer) pass the dict through to avoid a second
    GET. ``None`` (default) falls back to the in-function GET so
    standalone callers don't need to do their own fetch. The 404
    short-circuit only fires when this function does its own GET — a
    pre-fetched payload necessarily came from a successful GET, so 404
    is impossible at that point.
    """
    # 1) GET the opportunity (or accept the pre-fetched payload).
    if opportunity_payload is not None:
        opportunity = opportunity_payload
    else:
        try:
            opportunity = client.get_opportunity(close_opportunity_id)
        except CloseError as exc:
            if exc.status_code == 404:
                audit.record(
                    actor="close_sync",
                    action="close.opportunity.sync_failed_not_found",
                    details={
                        "close_opportunity_id": close_opportunity_id,
                        "decision_id": str(decision_id),
                        "status_code": 404,
                    },
                )
                return SyncResult(
                    patched=False,
                    fields_diffed=[],
                    reason="opportunity_not_found",
                )
            raise

    # 2) Desired values. ``None`` means "don't touch the Close-side
    #    value" — every desired entry below is conditional.
    desired: dict[str, Any] = {}
    if suggested_max_advance is not None:
        desired["suggested_max_advance"] = _quantize_money(suggested_max_advance)
    if recommended_factor_rate is not None:
        desired["recommended_factor_rate"] = _quantize_factor_rate(recommended_factor_rate)
    if recommended_holdback_pct is not None:
        desired["recommended_holdback_pct"] = _quantize_holdback_pct(recommended_holdback_pct)
    if true_revenue_monthly is not None:
        desired["true_revenue"] = _quantize_money(true_revenue_monthly)
    if holdback_capacity_monthly is not None:
        desired["holdback_capacity"] = _quantize_money(holdback_capacity_monthly)
    if existing_mca_count is not None:
        desired["existing_mca_count"] = existing_mca_count
    if existing_mca_daily_total is not None:
        desired["existing_mca_daily_total"] = _quantize_money(existing_mca_daily_total)

    # 3) Diff against current.
    diffs: dict[str, Any] = {}
    for aegis_name, desired_value in desired.items():
        current = _read_opportunity_field(opportunity, aegis_name)
        if current != desired_value:
            diffs[aegis_name] = desired_value

    if not diffs:
        audit.record(
            actor="close_sync",
            action="close.opportunity.sync_attempted",
            details={
                "close_opportunity_id": close_opportunity_id,
                "decision_id": str(decision_id),
                "fields_diffed": [],
                "patched": False,
            },
        )
        return SyncResult(patched=False, fields_diffed=[], reason="no_diff")

    # 4) Build PATCH payload — only diffed fields.
    patch_body: dict[str, Any] = {}
    for aegis_name, desired_value in diffs.items():
        cf_id = CLOSE_OPPORTUNITY_FIELD_IDS[aegis_name]
        patch_body[f"custom.{cf_id}"] = desired_value

    fields_diffed_sorted = sorted(diffs.keys())

    try:
        client.update_opportunity_custom_fields(close_opportunity_id, patch_body)
    except CloseError as exc:
        audit.record(
            actor="close_sync",
            action="close.opportunity.sync_attempted",
            details={
                "close_opportunity_id": close_opportunity_id,
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
        action="close.opportunity.sync_attempted",
        details={
            "close_opportunity_id": close_opportunity_id,
            "decision_id": str(decision_id),
            "fields_diffed": fields_diffed_sorted,
            "patched": True,
        },
    )
    return SyncResult(patched=True, fields_diffed=fields_diffed_sorted, reason="patched")


def _quantize_money(value: Decimal) -> str:
    """Close's text-typed money custom fields round-trip as strings.
    Quantize to 2dp so the diff is stable across types (a stored
    ``"50000"`` from Close vs a freshly-computed ``Decimal("50000.00")``
    would otherwise read as different)."""
    return str(value.quantize(Decimal("0.01")))


def _quantize_factor_rate(value: Decimal) -> str:
    """Factor rates are 3dp by convention (e.g. ``1.180``)."""
    return str(value.quantize(Decimal("0.001")))


def _quantize_holdback_pct(value: Decimal) -> str:
    """Holdback percentages render as 4dp decimal fractions
    (``0.1500``) so a downstream display can present 15.00% without
    ambiguity over whether the stored value is a fraction or a
    pre-multiplied percentage."""
    return str(value.quantize(Decimal("0.0001")))


def _read_opportunity_field(opportunity: dict[str, Any], aegis_name: str) -> Any:  # noqa: ANN401 — Close custom-field values are heterogeneous
    """Read an Opportunity custom-field value via the AEGIS-side name."""
    return opportunity.get(f"custom.{CLOSE_OPPORTUNITY_FIELD_IDS[aegis_name]}")


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
    "push_offer_to_opportunity",
]
