"""M8 — cross-statement / related-account merchant binding detector.

AEGIS today treats each uploaded statement bundle in isolation. Two real
risks go undetected at single-bundle parse-time:

1. **Duplicate PDF upload.** A merchant uploads the same PDF twice — by
   accident (re-upload after a network hiccup) OR by attempting to bump
   aggregates ("if I upload the same month twice, my deposits look 2x").
   Bundle-level aggregates are computed twice without dedup.

2. **Related accounts.** A merchant has multiple bank accounts — per
   location, per processor, per entity. One submission shows revenue
   from account A; the broker can't tell if the merchant is also pulling
   revenue from an unrelated account B that wasn't disclosed. Same legal
   ``account_holder`` (the BUSINESS name on the statement) across two
   bundles with DIFFERENT ``account_last4`` is the textbook "hidden
   second bank account" signal in MCA fraud playbooks — used to hide
   revenue (inflate Stage-1 aggregates) or hide ACH debits (inflate
   solvency).

Per CLAUDE.md "Decision-boundary changes — deliberate + shadow-first":
this detector emits flags only. It does NOT alter ``mca_positions``,
``fraud_score``, ``parse_status``, or any hard-decline reason. Operator
validates on corpus + live shadow audit rows before flipping to live via
a config / env var change — same discipline as the tampering rule and
the parser-side R1.1 shadow patterns in ``parser/patterns.py``.

Why this lives in ``merchants/`` and not ``parser/patterns.py``
--------------------------------------------------------------
``parser/patterns.py::analyze_patterns`` operates on a flat
``list[ClassifiedTransaction]`` for ONE document — no multi-document
context. Cross-statement binding by definition needs the merchant's
prior uploads. So this module sits next to the merchant repository.

Why this lives in ``merchants/`` and not ``scoring/multi_month.py``
------------------------------------------------------------------
``scoring/multi_month.py`` runs at score-time, not at upload-time. The
duplicate-PDF flag is most useful at upload-time — it lets the operator
spot the dedup BEFORE the worker has re-computed aggregates the second
time. The detector is therefore a pure function the worker can call
right after ``persist_parse_result`` returns. Wiring the call site is
a follow-up; this module just exposes the pure function.

In-memory only
--------------
The detector returns ``list[Pattern]`` (the same shape used by
``parser/patterns.py``). How the flags get persisted into the
documents row, the analyses row, or a new per-merchant
``shadow_signals`` channel is a follow-up — explicitly out of scope per
the U12 spec. No Supabase write, no migration in this commit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from aegis.parser.patterns import Pattern

# ---------------------------------------------------------------------------
# Inputs
#
# Pure data shapes the caller assembles from the merchant's prior parses.
# The caller (the worker, post-persist) reads ``documents`` for the file
# hashes and ``analyses`` + ``StatementSummary`` for the bank identity
# triple. The detector itself has no I/O so tests build these in memory.


@dataclass(frozen=True)
class CurrentUploadContext:
    """The just-parsed upload that the detector evaluates AGAINST priors.

    ``sha256_original`` is the plaintext PDF hash from
    ``documents.sha256_original`` (migration 033). ``account_holder``
    and ``account_last4`` come from ``StatementSummary`` — pass-1
    extraction populates them at parse time. ``bank_name`` is the same
    StatementSummary field (carried onto ``analyses.bank_name`` since
    migration 014); kept here for context-rich flag detail strings even
    though the related-account rule keys on holder+last4 only.

    Holder normalization (case + whitespace + punctuation) happens
    INSIDE the detector — callers pass the raw extracted string so the
    detector controls the rule.
    """

    document_id: UUID
    sha256_original: str | None
    bank_name: str | None
    account_holder: str | None
    account_last4: str | None


@dataclass(frozen=True)
class PriorDocumentRef:
    """Minimal shape per prior ``documents`` row for the same merchant.

    ``sha256_original`` is nullable for legacy pre-033 docs. The detector
    silently skips priors whose hash is missing — there's no fact to
    compare. ``uploaded_at`` lands in the duplicate-flag detail string
    so the operator can drill straight to the prior upload event.
    """

    document_id: UUID
    sha256_original: str | None
    uploaded_at: datetime


@dataclass(frozen=True)
class PriorAnalysisIdentity:
    """Per-prior bank-identity triple plus its source ``document_id``.

    ``document_id`` is the analysis's source document — the audit-trail
    handle the operator clicks to drill back. ``account_holder`` is
    nullable because (a) pass-1 occasionally fails to recover it on
    noisy statements and (b) pre-finalize provisional rows may carry
    None. The detector skips identities whose holder is missing.
    """

    document_id: UUID
    bank_name: str | None
    account_holder: str | None
    account_last4: str | None


# ---------------------------------------------------------------------------
# Flag codes — keep stable; downstream filtering uses these as keys.

_CODE_DUPLICATE_PDF: Final[str] = "duplicate_pdf_upload"
_CODE_RELATED_ACCOUNT: Final[str] = "related_account_suspected"

# Shadow-only severity. Per CLAUDE.md scoring discipline, severity > 0
# means "contributes to fraud_score" — and that's the decline-path
# input. Shadow flags ride severity 0 until the operator flips them
# live via an explicit corpus-validated decision-boundary change.
_SHADOW_SEVERITY: Final[int] = 0


# ---------------------------------------------------------------------------
# account_holder normalization
#
# Real-world account_holder strings drift even when the underlying
# legal entity is identical:
#   "Acme LLC"          vs  "ACME LLC"        — case
#   "Acme, LLC"         vs  "Acme LLC"        — punctuation
#   "Acme  LLC"         vs  "Acme LLC"        — whitespace collapse
#   " Acme LLC "        vs  "Acme LLC"        — leading/trailing
#
# We normalize for the COMPARISON only — the detail string carries the
# raw original holder so the operator sees what's actually on the
# statement. Two rules:
#
# 1. Casefold (Unicode-correct lower) to neutralize case.
# 2. Strip ASCII punctuation (``,.&'"-/\``) — these are presentational
#    on a legal name and routinely vary statement-to-statement.
# 3. Collapse all runs of whitespace to a single space; strip ends.
#
# We intentionally do NOT strip the entity suffix ("LLC", "INC",
# "CORP"). Two merchants "Acme LLC" and "Acme Inc" are legally
# DISTINCT entities — flagging them as related would be a false
# positive. The detector's purpose is "same legal entity, different
# bank account," not "similar-looking name."

_PUNCT_TO_STRIP: Final[re.Pattern[str]] = re.compile(r"[,.&'\"\-/\\]")
_WHITESPACE_RUN: Final[re.Pattern[str]] = re.compile(r"\s+")


def _normalize_holder(raw: str | None) -> str | None:
    """Return the comparison-key form of ``account_holder``.

    ``None`` -> ``None`` (no comparison possible). Empty / whitespace-only
    -> ``None`` (likewise). All other inputs return a non-empty
    normalized string.
    """
    if raw is None:
        return None
    # Casefold first so subsequent regex strips operate on a single case
    # form (cheaper + slightly safer for non-ASCII characters than .lower).
    folded = raw.casefold()
    folded = _PUNCT_TO_STRIP.sub(" ", folded)
    folded = _WHITESPACE_RUN.sub(" ", folded).strip()
    return folded or None


# ---------------------------------------------------------------------------
# Sub-detector A — duplicate PDF upload


def detect_duplicate_pdf_upload(
    current: CurrentUploadContext,
    priors: list[PriorDocumentRef],
) -> list[Pattern]:
    """Flag if any prior document for this merchant shares the SHA-256.

    Identical plaintext PDFs collide on ``sha256_original`` — the
    documents row already records this (migration 033). When the
    operator uploads the same statement file twice for the same
    merchant, the second parse re-computes aggregates against
    byte-identical data; the dashboard then shows "2x deposits" for
    that period.

    Skipped paths (all return ``[]``):
      * Current hash is None — pre-033 row OR storage step hadn't
        landed yet; nothing to compare.
      * No priors with matching hash.
      * Priors whose own hash is None (legacy) — silently skipped.

    The flag detail string carries the colliding prior's document_id
    + uploaded_at so the operator can drill straight to the original
    upload event from the dossier.
    """
    if current.sha256_original is None:
        return []

    hits: list[PriorDocumentRef] = [
        p
        for p in priors
        if p.sha256_original is not None
        and p.sha256_original == current.sha256_original
        and p.document_id != current.document_id
    ]
    if not hits:
        return []

    # Sort by uploaded_at so the detail string is deterministic across
    # tests + production. Oldest first — most-relevant for "this is
    # the duplicate you already uploaded N days ago" framing.
    hits.sort(key=lambda p: p.uploaded_at)

    # The audit-trail handle the operator clicks. All hits become
    # source_ids so a multi-collision (rare but possible — accidental
    # 3x upload) all surface from one flag.
    source_ids = [h.document_id for h in hits]
    detail_parts = [
        f"sha256_match_with_doc={hits[0].document_id}",
        f"uploaded={hits[0].uploaded_at.isoformat()}",
    ]
    if len(hits) > 1:
        # Surface the count so the operator sees "this is the 3rd copy"
        # not just "there's one duplicate."
        detail_parts.append(f"total_prior_copies={len(hits)}")

    detail = ":".join(detail_parts)
    return [
        Pattern(
            code=_CODE_DUPLICATE_PDF,
            severity=_SHADOW_SEVERITY,
            detail=detail,
            source_ids=source_ids,
        )
    ]


# ---------------------------------------------------------------------------
# Sub-detector B — related account (account_holder collision, last4 drift)


def detect_related_account_holder(
    current: CurrentUploadContext,
    priors: list[PriorAnalysisIdentity],
) -> list[Pattern]:
    """Flag when same holder appears with a NEW ``account_last4``.

    The "related accounts" risk in MCA underwriting: same legal entity
    (same normalized account_holder) banking at the same or different
    bank but on a DIFFERENT account number. The merchant may be
    pulling revenue from an undisclosed sibling account ("revenue
    hide") or, conversely, may be hiding ACH debits there ("solvency
    hide"). Either way, the broker needs to chase the second-account
    statement.

    Rule:
      1. Normalize the current upload's ``account_holder``.
      2. Walk priors. Collect the set of distinct ``account_last4``
         values seen historically under the same normalized holder.
      3. If the current upload's ``account_last4`` is NOT in that set
         AND the set is non-empty (i.e. we DO have prior holders to
         compare against), fire.

    Skipped paths (all return ``[]``):
      * Current ``account_holder`` is None / empty post-normalize —
        cannot compare.
      * Current ``account_last4`` is None — cannot drift from prior
        last4 if the current value is unknown.
      * No priors share the normalized holder — first time we've seen
        this holder for the merchant; nothing yet to drift against.
      * Current ``account_last4`` already in the prior set — same
        account, just additional months of data. NOT a related-account
        signal.

    The flag detail string keys the comma-joined existing last4 set
    plus the new last4, plus the raw (unnormalized) holder so the
    operator sees the statement-literal name.

    ``source_ids`` are the ``document_id`` of every prior analysis
    whose holder matches — the audit-trail handles the operator
    clicks to see the OTHER accounts already on file.
    """
    norm_current = _normalize_holder(current.account_holder)
    if norm_current is None:
        return []
    if current.account_last4 is None or current.account_last4.strip() == "":
        return []

    # Build the per-holder last4 set + the list of contributing prior
    # document_ids. We walk priors once.
    matching_last4: set[str] = set()
    matching_doc_ids: list[UUID] = []
    for prior in priors:
        norm_prior = _normalize_holder(prior.account_holder)
        if norm_prior is None or norm_prior != norm_current:
            continue
        if prior.account_last4 is None or prior.account_last4.strip() == "":
            # We've matched the holder but cannot derive a last4 —
            # skip from the SET (so it doesn't get joined into the
            # detail string as ""), but still credit the document
            # as a source — the prior analysis IS for the same
            # holder and the operator may want to see it.
            matching_doc_ids.append(prior.document_id)
            continue
        matching_last4.add(prior.account_last4)
        matching_doc_ids.append(prior.document_id)

    if not matching_doc_ids:
        # No prior holder match → no related-account claim.
        return []

    if current.account_last4 in matching_last4:
        # Same holder + same last4 — additional months on the SAME
        # account. Not a related-account signal.
        return []

    # If we matched ONLY holders that had no last4 (rare) we can't
    # claim "drift to a new last4" honestly — there's no prior last4
    # to drift FROM. Skip rather than emit a misleading flag.
    if not matching_last4:
        return []

    # Deterministic order on the detail string + source_ids.
    existing_last4_joined = ",".join(sorted(matching_last4))
    matching_doc_ids.sort(key=str)

    # Raw (statement-literal) holder in the detail so the operator
    # sees what's actually printed; the normalized form is what we
    # matched on but it's UX noise on the dossier line.
    raw_holder = (current.account_holder or "").strip()

    detail = (
        f"holder={raw_holder}"
        f":existing_last4={existing_last4_joined}"
        f":new_last4={current.account_last4}"
    )
    return [
        Pattern(
            code=_CODE_RELATED_ACCOUNT,
            severity=_SHADOW_SEVERITY,
            detail=detail,
            source_ids=matching_doc_ids,
        )
    ]


# ---------------------------------------------------------------------------
# Top-level entry


def detect_cross_statement_bindings(
    current: CurrentUploadContext,
    *,
    prior_documents: list[PriorDocumentRef],
    prior_analyses: list[PriorAnalysisIdentity],
) -> list[Pattern]:
    """Run both M8 sub-detectors and return the combined shadow flags.

    Order is deterministic: duplicate-PDF first, then related-account.
    Returns an empty list when neither sub-detector fires — the caller
    can use a truthiness check without iterating.

    Per CLAUDE.md "Decision-boundary changes — shadow-first": every
    flag emitted here MUST have severity 0 so it never feeds
    ``patterns.fraud_score`` (which sums severities). The shadow path
    is operator-evidence-only until the corpus + audit-log validation
    has run and the operator flips the rule live via an explicit
    config change.
    """
    flags: list[Pattern] = []
    flags.extend(detect_duplicate_pdf_upload(current, prior_documents))
    flags.extend(detect_related_account_holder(current, prior_analyses))
    return flags


__all__ = [
    "CurrentUploadContext",
    "PriorAnalysisIdentity",
    "PriorDocumentRef",
    "detect_cross_statement_bindings",
    "detect_duplicate_pdf_upload",
    "detect_related_account_holder",
]
