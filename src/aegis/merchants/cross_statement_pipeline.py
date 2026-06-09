"""U15 — thin orchestrator wrapping the U12 cross-statement detector.

The U12 detector (``aegis.merchants.cross_statement_detector``) is a
pure function over ``CurrentUploadContext`` + ``list[PriorDocumentRef]``
+ ``list[PriorAnalysisIdentity]``. The detector does no I/O by design
so unit tests stay deterministic.

This module is the I/O wrapper the upload worker calls. It:

  1. Pulls the merchant's prior documents via
     ``DocumentRepository.list_documents(merchant_id=...)``, then
     filters out the just-parsed document itself.
  2. Pulls the matching prior analyses via
     ``DocumentRepository.get_analyses_by_document_ids(...)``.
  3. Builds the detector's ``PriorDocumentRef`` + ``PriorAnalysisIdentity``
     lists from the rows.
  4. Calls ``detect_cross_statement_bindings`` and returns the
     resulting Pattern list (severity 0 — U12 shadow-only invariant).

Per CLAUDE.md "Decision-boundary changes — shadow-first": the worker
appends these Patterns to a NEW field on ``PipelineResult``
(``cross_statement_patterns``), NOT to ``pattern_analysis.patterns``.
Conflating document-scope patterns with cross-document-scope patterns
in a single list would let a future scoring change accidentally pull
cross-statement severities into the per-document fraud_score sum;
keeping the channel separate forces the operator to make the merge
deliberate when the corpus + audit-log validation has run.

Persistence of the flags is out of scope for this commit — they live
in-memory on the PipelineResult and surface via the worker's one
INFO log line (``cross_statement_signals_detected``). Whether they
should land in ``pattern_analysis.shadow_patterns`` or a new
``merchants.shadow_signals`` channel is a follow-up; this orchestrator
just emits the list and lets the worker decide.

PII note (CLAUDE.md): ``account_holder`` is PII. The orchestrator
passes the raw extracted holder string TO the detector (which needs
it for the normalized comparison) but the worker's log line carries
flag CODES only, never holder strings or document ids.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from aegis.merchants.cross_statement_detector import (
    CurrentUploadContext,
    PriorAnalysisIdentity,
    PriorDocumentRef,
    detect_cross_statement_bindings,
)
from aegis.parser.patterns import Pattern
from aegis.storage import DocumentRepository


def run_cross_statement_detection(
    *,
    merchant_id: UUID,
    current_document_id: UUID,
    current_sha256: str | None,
    current_uploaded_at: datetime,
    current_bank_name: str | None,
    current_account_holder: str | None,
    current_account_last4: str | None,
    repo: DocumentRepository,
) -> list[Pattern]:
    """Run the U12 detector against the merchant's prior parses.

    Returns an empty list when nothing fires OR when the merchant has
    no prior documents on file. Never raises — the caller can log the
    returned codes without try/except.

    ``current_uploaded_at`` is currently unused (reserved): when the
    detector grows a "only consider priors uploaded BEFORE this one"
    rule (e.g. to avoid a race where two concurrent uploads of the
    same SHA flag each other), the worker already has the timestamp
    on hand. Including it in the signature now keeps the call site
    stable.

    The detector's documented contract is "skip the current document
    in the priors list" — we enforce that here by filtering on
    ``document_id != current_document_id`` so the worker doesn't have
    to.
    """
    # Pull every document on file for this merchant (limit=1000 is
    # 10x the operator's stated 100 deals/month volume — a renewal
    # merchant with 30 statements stays well under cap). The worker
    # path runs ONCE per upload so the read cost is bounded by the
    # operator's throughput, not by traffic.
    all_docs = repo.list_documents(merchant_id=merchant_id, limit=1000)
    prior_docs = [d for d in all_docs if d.id != current_document_id]

    if not prior_docs:
        # First upload for this merchant — nothing to compare against.
        # Skip the analyses fetch entirely so cold-start uploads pay
        # only one Supabase round-trip.
        return []

    prior_doc_refs = [
        PriorDocumentRef(
            document_id=d.id,
            sha256_original=d.sha256_original,
            uploaded_at=d.uploaded_at,
        )
        for d in prior_docs
    ]

    # Batch the analyses fetch — one Supabase ``in.(...)`` query
    # instead of N per-document calls. Missing analyses (parse failed,
    # processor branch, manual_review) are silently absent from the
    # returned dict, which is correct: no analysis means no
    # bank-identity triple to feed the related-account sub-detector.
    prior_doc_ids = [d.id for d in prior_docs]
    analyses_by_doc = repo.get_analyses_by_document_ids(prior_doc_ids)

    prior_analysis_identities = [
        PriorAnalysisIdentity(
            document_id=analysis.document_id,
            bank_name=analysis.bank_name,
            account_holder=analysis.account_holder,
            account_last4=analysis.account_last4,
        )
        for analysis in analyses_by_doc.values()
    ]

    current = CurrentUploadContext(
        document_id=current_document_id,
        sha256_original=current_sha256,
        bank_name=current_bank_name,
        account_holder=current_account_holder,
        account_last4=current_account_last4,
    )

    return detect_cross_statement_bindings(
        current,
        prior_documents=prior_doc_refs,
        prior_analyses=prior_analysis_identities,
    )


__all__ = ["run_cross_statement_detection"]
