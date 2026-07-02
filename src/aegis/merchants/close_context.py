"""Pure-orchestration helper that refreshes a merchant's Close-derived
context columns (Feature D, migration 064).

Three Close inputs are pulled, joined, and persisted as three columns:

* ``merchants.close_lead_description`` — verbatim Close Lead
  ``description`` field, extracted via
  :func:`aegis.close.field_map.extract_lead_description`.
* ``merchants.close_notes_summary``    — bodies of the 5 most-recent
  Close ``activity/note`` entries for the lead, joined by ``\n---\n``.
* ``merchants.close_call_transcripts`` — ``note`` field of the 3
  most-recent Close ``activity/call`` entries, same join separator.

Failure model. The orchestrator runs in two contexts:

  1. Webhook handler. Close-side failures must NOT 5xx the webhook —
     Close will retry every webhook for 72 hours and a single transient
     Close failure on the side path would generate noise without
     improving safety. The webhook wraps this call in a try/except so
     a raise here surfaces as an audit row + 200 webhook response.
  2. Dossier "Refresh Close fields" button. Same try/except in the
     route — the operator sees a flash message but the dossier still
     renders.

Inside this module, we let exceptions surface so the caller can decide.
Audit-write failure DOES propagate (per CLAUDE.md "every audit_log write
must succeed or the calling op fails"): if we can't record the
refresh, the operation itself is incomplete.

PII posture (CLAUDE.md, migration 064 column comments). Note bodies and
call transcripts CAN contain merchant PII / transaction descriptions.
They land on ``merchants.*`` columns (acceptable per the funder-review
posture) but they NEVER touch ``audit_log.details`` — the audit row
carries counts only. Anyone reading the audit log can confirm a
refresh occurred without exposing the body content.

``lead_fetcher`` injection. The orchestrator uses an injected
``lead_fetcher`` callable (rather than ``close_client.get_lead``
directly) so the webhook can reuse a lead payload it has already
fetched without paying for a second round-trip. Tests stub the
callable directly.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from aegis.audit import AuditLog
from aegis.close.client import CloseClient, CloseNote
from aegis.close.field_map import (
    extract_lead_description,
    fetch_call_transcripts_for_lead,
)
from aegis.logger import get_logger
from aegis.merchants.repository import MerchantRepository

_log = get_logger(__name__)

# Operator-reviewable constants. The note / call limits match the
# Feature D spec (5 / 3). Bumping them is a config change, not a code
# rewrite — but every bump pulls more PII into the prompt + into one
# Postgres column, so bump deliberately.
RECENT_NOTES_LIMIT: int = 5
RECENT_CALLS_LIMIT: int = 3

# Separator between joined note / call bodies. Three dashes on a line by
# themselves so an LLM parsing the prompt has a stable boundary and a
# human reading the dossier "read-only" block can spot where one note
# ends and the next begins.
_BODY_SEPARATOR: str = "\n---\n"

# Operators sometimes paste Commera's own marketing / product copy into
# the Close Lead description ("We offer merchant cash advances and
# working capital solutions ..." etc). That copy describes Commera,
# not the merchant — surfacing it on the merchant dossier and in the
# Bedrock funder-narrative prompt pollutes funder context with
# information the funder already has on the broker.
#
# Heuristic: case-insensitive substring match on a small operator-
# curated signal list. False-positive rate is acceptably low — a real
# MCA merchant rarely describes its own business as "merchant cash
# advance" or "working capital" (those phrases describe the funding
# product, not the business), and "Commera" should never appear in a
# legitimate merchant context.
_COMMERA_BOILERPLATE_SIGNALS: tuple[str, ...] = (
    "commera",
    "merchant cash advance",
    "working capital",
)


def _is_commera_boilerplate(description: str) -> bool:
    """Return True when ``description`` looks like Commera's own
    marketing copy rather than the merchant's context.
    """
    needle = description.lower()
    return any(signal in needle for signal in _COMMERA_BOILERPLATE_SIGNALS)


def _filter_commera_boilerplate(description: str | None) -> str | None:
    """Drop Commera-marketing-copy descriptions; passthrough otherwise.

    Returns ``None`` when the description is Commera's own product
    pitch rather than the merchant's context, so the merchant row
    stays NULL on ``close_lead_description`` instead of storing
    boilerplate. Logged at INFO so the operator can audit the filter's
    activity without exposing the body in the audit_log row.
    """
    if description is None:
        return None
    if _is_commera_boilerplate(description):
        _log.info("close_context.lead_description_filtered_as_boilerplate")
        return None
    return description


LeadFetcher = Callable[[str], dict[str, Any]]


def _default_lead_fetcher(close_client: CloseClient) -> LeadFetcher:
    """Build a fetcher that calls ``close_client.get_lead`` lazily.

    The webhook caller passes its own pre-fetched lead via a closure
    around ``lambda lead_id: cached_payload``; the dossier route uses
    this default because it has no cached lead.
    """

    def _fetch(lead_id: str) -> dict[str, Any]:
        return close_client.get_lead(lead_id)

    return _fetch


def _join_bodies(bodies: list[str | None]) -> str | None:
    """Filter ``None`` / empty strings then join with the separator.

    Returns ``None`` when every input is empty so the DB column stays
    NULL rather than holding a separator-only string.
    """
    keep = [b.strip() for b in bodies if b and b.strip()]
    if not keep:
        return None
    return _BODY_SEPARATOR.join(keep)


def _note_bodies(notes: list[CloseNote]) -> list[str | None]:
    return [n.note for n in notes]


def refresh_close_context_for_merchant(
    merchant_id: UUID,
    lead_id: str,
    *,
    close_client: CloseClient,
    merchants_repo: MerchantRepository,
    audit: AuditLog,
    lead_fetcher: LeadFetcher | None = None,
) -> None:
    """Pull lead description + recent notes + recent calls from Close;
    persist the three derived columns on ``merchants``; audit.

    Pure orchestration — no HTTP boundary handling beyond letting
    :class:`aegis.close.client.CloseError` propagate to the caller.
    Caller (webhook handler, dossier route) decides whether to swallow
    a transient Close failure or surface it.

    Audit row shape (``merchant.close_context.refreshed``):

    * ``actor``         — ``close_context``
    * ``subject_type`` — ``"merchant"``
    * ``subject_id``   — ``merchant_id``
    * ``details``      — ``{"close_lead_id": lead_id, "notes_pulled":
      N, "calls_pulled": M, "lead_description_present": bool}``

    The bodies themselves never appear in the audit row. Anyone reading
    the audit log can confirm the refresh happened without exposing
    PII.
    """
    fetcher = lead_fetcher or _default_lead_fetcher(close_client)
    lead_payload = fetcher(lead_id)

    lead_description = _filter_commera_boilerplate(extract_lead_description(lead_payload))
    notes = close_client.list_recent_notes(lead_id, RECENT_NOTES_LIMIT)
    # Kept as a shape probe so the audit-row ``calls_pulled`` count stays
    # honest (the caller sees "we pulled M calls, N of which had bodies
    # long enough to appear in the transcript column"). The transcripts
    # column itself is written by ``fetch_call_transcripts_for_lead``,
    # which formats each call as ``[Call YYYY-MM-DD — Ns]\n{note}``
    # blocks separated by ``\n\n`` — the format the narrator prompt
    # reads and the operator sees on the dossier. One write path, richer
    # format flows all the way through.
    calls = close_client.list_recent_calls(lead_id, RECENT_CALLS_LIMIT)

    notes_summary = _join_bodies(_note_bodies(notes))
    call_transcripts = fetch_call_transcripts_for_lead(lead_id, close_client)

    merchants_repo.set_close_context(
        merchant_id,
        notes_summary=notes_summary,
        lead_description=lead_description,
        call_transcripts=call_transcripts,
    )

    audit.record(
        actor="close_context",
        action="merchant.close_context.refreshed",
        subject_type="merchant",
        subject_id=merchant_id,
        details={
            "close_lead_id": lead_id,
            "notes_pulled": len(notes),
            "calls_pulled": len(calls),
            "lead_description_present": lead_description is not None,
        },
    )


__all__ = [
    "RECENT_CALLS_LIMIT",
    "RECENT_NOTES_LIMIT",
    "LeadFetcher",
    "refresh_close_context_for_merchant",
]
