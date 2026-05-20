"""Worker-level tests for ``process_funder_reply`` (mp Phase 10 / 2D-main).

End-to-end exercises of the worker entrypoint: takes a JSON payload,
runs the canned-LLM two-pass extractor, and routes through
``ingest_reply``. Covers the three load-bearing properties from
refinement (5) in the master plan:

  * **Forward arrival order:** open override exists FIRST, then a
    funder reply arrives via the worker → override is stamped exactly
    once. (test_forward_arrival_order_stamps_override)
  * **Reverse arrival order:** funder reply arrives via the worker
    BEFORE any override exists → reply persists; later when the
    override is created and back-stamping runs, the override picks up
    the reply's outcome. (test_reverse_arrival_order_back_stamps)
  * **Idempotency:** running the worker twice with the same payload
    (e.g. arq retried after a crash) leaves the final state unchanged
    — second reply persists but does NOT overwrite the first stamp.
    (test_idempotent_double_invocation)

Additional coverage:
  * Two-pass LLM behavior end-to-end (pass-1 invalid -> pass-2 corrects)
  * Deterministic reconcile failure flags for review without stamping
  * Unknown extraction status audits and skips persistence
  * Malformed JSON payload raises ValueError before any LLM call

All LLM responses are canned via the same ``_StubReplyLLM`` shape as
``tests/funders/test_reply_extract.py``. No real Bedrock calls.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest

from aegis.audit import InMemoryAuditLog
from aegis.funders.replies import InMemoryFunderReplyRepository
from aegis.workers import process_funder_reply

# ---------------------------------------------------------------------------
# Test helpers — canned LLM, payload builders
# ---------------------------------------------------------------------------


class _StubReplyLLM:
    """Sequence-of-responses stub. See test_reply_extract for shape."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.call_count = 0

    def classify_batch_json(self, prompt: str) -> dict[str, Any]:
        if not self._responses:
            raise AssertionError("stub LLM ran out of responses")
        self.call_count += 1
        nxt = self._responses.pop(0)
        if callable(nxt):
            return nxt(prompt)  # type: ignore[no-any-return]
        return dict(nxt)

    def extract_raw_json(
        self, pdf_bytes: bytes, prompt: str
    ) -> tuple[dict[str, Any], bool]:
        raise NotImplementedError

    def extract_raw_json_from_images(
        self, page_images_png: list[bytes], prompt: str
    ) -> tuple[dict[str, Any], bool]:
        raise NotImplementedError


def _approved_extraction(
    *,
    amount: str = "20000.00",
    factor: str = "1.32",
    payback: str = "26400.00",
) -> dict[str, Any]:
    return {
        "status": "approved",
        "decline_reason": None,
        "funder_name_text": "Acme Capital Funding",
        "terms": {
            "amount": amount,
            "factor": factor,
            "payback": payback,
            "term_days": 120,
            "daily_payment": "220.00",
            "holdback_pct": "0.12",
        },
        "parsed_confidence": 85,
        "notes": None,
    }


def _declined_extraction() -> dict[str, Any]:
    return {
        "status": "declined",
        "decline_reason": "NSF count exceeds threshold",
        "funder_name_text": "Acme",
        "terms": {},
        "parsed_confidence": 90,
        "notes": None,
    }


def _worker_payload(
    *,
    deal_id: UUID,
    funder_id: UUID,
    raw_text: str = "Funder approves at 1.32 factor, $20,000 advance.",
    ingested_via: str = "webhook",
) -> str:
    return json.dumps(
        {
            "deal_id": str(deal_id),
            "funder_id": str(funder_id),
            "raw_text": raw_text,
            "ingested_via": ingested_via,
        }
    )


def _seed_open_override(
    repo: InMemoryFunderReplyRepository,
    deal_id: UUID,
    *,
    override_id: UUID | None = None,
    created_at: str = "2026-05-18T10:00:00+00:00",
) -> UUID:
    override_id = override_id or uuid4()
    repo.add_override(
        {
            "id": str(override_id),
            "deal_id": str(deal_id),
            "outcome": None,
            "created_at": created_at,
        }
    )
    return override_id


def _ctx(
    *,
    llm: object,
    audit: InMemoryAuditLog,
    repo: InMemoryFunderReplyRepository,
) -> dict[str, Any]:
    """Build the arq-shaped context the worker accepts."""
    return {
        "audit": audit,
        "llm": llm,
        "funder_reply_repository": repo,
    }


# ---------------------------------------------------------------------------
# Forward arrival order — open override exists, then reply arrives
# ---------------------------------------------------------------------------


async def test_forward_arrival_order_stamps_override() -> None:
    """Override is seeded first (operator pushed deal despite AEGIS
    scoring). Then the funder reply arrives via the worker — the
    extractor parses it, ``ingest_reply`` stamps the open override
    exactly once with outcome='funded'."""
    repo = InMemoryFunderReplyRepository()
    audit = InMemoryAuditLog()
    deal_id = uuid4()
    funder_id = uuid4()
    override_id = _seed_open_override(repo, deal_id)

    llm = _StubReplyLLM([_approved_extraction()])
    result = await process_funder_reply(
        _ctx(llm=llm, audit=audit, repo=repo),
        _worker_payload(deal_id=deal_id, funder_id=funder_id),
    )

    assert result["persisted"] is True
    assert result["status"] == "approved"
    assert result["validation_passed"] is True
    assert result["stamped_override_id"] == str(override_id)

    # Reply landed; override stamped funded; no other replies/overrides.
    assert len(repo.replies()) == 1
    stamped = next(r for r in repo.overrides() if r["id"] == str(override_id))
    assert stamped["outcome"] == "funded"
    assert stamped["outcome_recorded_at"] is not None

    # Audit chain: start + ingested + complete.
    actions = [e["action"] for e in audit.entries]
    assert "funder_reply.process.start" in actions
    assert "funder_reply.ingested" in actions
    assert "funder_reply.process.complete" in actions


async def test_forward_arrival_order_with_declined_reply() -> None:
    """Same forward order, but the reply is a decline → override
    is stamped 'declined_by_funder' rather than 'funded'."""
    repo = InMemoryFunderReplyRepository()
    audit = InMemoryAuditLog()
    deal_id = uuid4()
    funder_id = uuid4()
    override_id = _seed_open_override(repo, deal_id)

    llm = _StubReplyLLM([_declined_extraction()])
    result = await process_funder_reply(
        _ctx(llm=llm, audit=audit, repo=repo),
        _worker_payload(deal_id=deal_id, funder_id=funder_id),
    )
    assert result["stamped_override_id"] == str(override_id)
    stamped = next(r for r in repo.overrides() if r["id"] == str(override_id))
    assert stamped["outcome"] == "declined_by_funder"


# ---------------------------------------------------------------------------
# Reverse arrival order — reply arrives first, override appears later
# ---------------------------------------------------------------------------


async def test_reverse_arrival_order_back_stamps_on_override_creation() -> None:
    """The worker processes a reply for a deal that has no override
    yet. The reply persists. Later, when an override lands and the
    back-stamping path runs (``stamp_override_from_replies``), the
    override picks up the funded outcome.

    Both halves of refinement (5) — in-line stamping (forward order)
    and back-stamping (reverse order) — converge on the same final
    state: override.outcome = 'funded'."""
    from datetime import UTC, datetime

    from aegis.funders.replies import stamp_override_from_replies

    repo = InMemoryFunderReplyRepository()
    audit = InMemoryAuditLog()
    deal_id = uuid4()
    funder_id = uuid4()

    # No override exists yet.
    assert repo.overrides() == []

    # Reply lands via the worker.
    llm = _StubReplyLLM([_approved_extraction()])
    result = await process_funder_reply(
        _ctx(llm=llm, audit=audit, repo=repo),
        _worker_payload(deal_id=deal_id, funder_id=funder_id),
    )
    assert result["persisted"] is True
    assert result["stamped_override_id"] is None  # no override to stamp yet
    assert len(repo.replies()) == 1

    # Override created later — back-stamping runs.
    override_id = _seed_open_override(repo, deal_id)
    back_stamped = stamp_override_from_replies(
        override_id=override_id,
        deal_id=deal_id,
        repo=repo,
        audit=audit,
        now=datetime(2026, 5, 18, 14, 0, tzinfo=UTC),
    )
    assert back_stamped == override_id

    # Final state is identical to the forward-order test.
    stamped = next(r for r in repo.overrides() if r["id"] == str(override_id))
    assert stamped["outcome"] == "funded"


# ---------------------------------------------------------------------------
# Idempotency — running the worker twice on the same payload
# ---------------------------------------------------------------------------


async def test_idempotent_double_invocation() -> None:
    """The worker is enqueued twice with the same payload (arq retry
    after a transient crash). The reply persists twice (one
    funder_replies row per inbound message — by design, audit trail
    of every attempt), but the override is stamped exactly once.
    Final state after second pass is identical to state after first
    pass on the override side."""
    repo = InMemoryFunderReplyRepository()
    audit = InMemoryAuditLog()
    deal_id = uuid4()
    funder_id = uuid4()
    override_id = _seed_open_override(repo, deal_id)

    # Two canned responses for two invocations.
    llm = _StubReplyLLM([_approved_extraction(), _approved_extraction()])
    payload = _worker_payload(deal_id=deal_id, funder_id=funder_id)

    first = await process_funder_reply(
        _ctx(llm=llm, audit=audit, repo=repo), payload
    )
    second = await process_funder_reply(
        _ctx(llm=llm, audit=audit, repo=repo), payload
    )

    # First stamp landed; second did NOT overwrite.
    assert first["stamped_override_id"] == str(override_id)
    assert second["stamped_override_id"] is None
    assert second["persisted"] is True  # row still persisted

    # Final override state is the first stamp's outcome.
    stamped = next(r for r in repo.overrides() if r["id"] == str(override_id))
    assert stamped["outcome"] == "funded"

    # One override; two replies (each inbound attempt audited).
    open_overrides = [r for r in repo.overrides() if r.get("outcome") is None]
    assert len(open_overrides) == 0
    assert len(repo.replies()) == 2


# ---------------------------------------------------------------------------
# Two-pass LLM behavior end-to-end
# ---------------------------------------------------------------------------


async def test_worker_runs_two_pass_when_pass1_invalid() -> None:
    """Pass 1 emits a float amount; pass 2 corrects → worker still
    persists + stamps. Verifies the re-prompt path is plumbed through
    the worker, not just the bare extractor."""
    repo = InMemoryFunderReplyRepository()
    audit = InMemoryAuditLog()
    deal_id = uuid4()
    funder_id = uuid4()
    override_id = _seed_open_override(repo, deal_id)

    invalid = _approved_extraction()
    invalid["terms"]["amount"] = 20000.0  # float — Pydantic rejects
    llm = _StubReplyLLM([invalid, _approved_extraction()])

    result = await process_funder_reply(
        _ctx(llm=llm, audit=audit, repo=repo),
        _worker_payload(deal_id=deal_id, funder_id=funder_id),
    )
    assert llm.call_count == 2
    assert result["stamped_override_id"] == str(override_id)

    # The complete audit row records reprompted=True.
    complete = next(
        e for e in audit.entries if e["action"] == "funder_reply.process.complete"
    )
    assert complete["details"]["reprompted"] is True


# ---------------------------------------------------------------------------
# Deterministic reconcile failure → flagged, not stamped
# ---------------------------------------------------------------------------


async def test_worker_flags_for_review_when_math_reconcile_fails() -> None:
    """LLM extraction passes Pydantic schema; the deterministic math
    gate then catches amount * factor != payback. The reply persists
    with parsed_confidence=0 (operator hand-corrects on the dashboard)
    and the override is NOT stamped. This is the load-bearing
    "don't blindly trust the LLM" property: schema-valid + math-
    invalid is treated as not-validated."""
    repo = InMemoryFunderReplyRepository()
    audit = InMemoryAuditLog()
    deal_id = uuid4()
    funder_id = uuid4()
    override_id = _seed_open_override(repo, deal_id)

    # 20000 * 1.32 = 26400; LLM reports 99999 (math mismatch).
    bad = _approved_extraction(payback="99999.00")
    llm = _StubReplyLLM([bad])

    result = await process_funder_reply(
        _ctx(llm=llm, audit=audit, repo=repo),
        _worker_payload(deal_id=deal_id, funder_id=funder_id),
    )

    assert result["persisted"] is True
    assert result["validation_passed"] is False
    assert result["stamped_override_id"] is None

    # Row persisted with parsed_confidence=0 per the validate-then-
    # persist contract in ingest_reply.
    persisted = repo.replies()[0]
    assert persisted["parsed_confidence"] == 0

    # Override remains OPEN — operator can correct and re-submit.
    open_overrides = [r for r in repo.overrides() if r.get("outcome") is None]
    assert len(open_overrides) == 1
    assert open_overrides[0]["id"] == str(override_id)


# ---------------------------------------------------------------------------
# Unknown extraction status → audit + drop, no persistence
# ---------------------------------------------------------------------------


async def test_worker_drops_unknown_status_extraction() -> None:
    """The LLM couldn't classify the email — status='unknown'. We
    audit the inbound for operator review but do NOT persist a
    funder_replies row (would corrupt the status='approved' /
    'declined' / 'countered' CHECK constraint and confuse the
    confusion matrix downstream)."""
    repo = InMemoryFunderReplyRepository()
    audit = InMemoryAuditLog()
    deal_id = uuid4()
    funder_id = uuid4()
    _seed_open_override(repo, deal_id)

    payload = _approved_extraction()
    payload["status"] = "unknown"
    payload["terms"] = {}
    llm = _StubReplyLLM([payload])

    result = await process_funder_reply(
        _ctx(llm=llm, audit=audit, repo=repo),
        _worker_payload(deal_id=deal_id, funder_id=funder_id),
    )

    assert result["status"] == "unknown"
    assert result["persisted"] is False
    assert result["stamped_override_id"] is None
    assert len(repo.replies()) == 0
    open_overrides = [r for r in repo.overrides() if r.get("outcome") is None]
    assert len(open_overrides) == 1  # untouched

    actions = [e["action"] for e in audit.entries]
    assert "funder_reply.process.unknown" in actions


# ---------------------------------------------------------------------------
# Payload validation — fail fast on malformed input
# ---------------------------------------------------------------------------


async def test_worker_rejects_malformed_payload_json() -> None:
    """Not JSON at all → ValueError, no LLM call, no DB write."""
    repo = InMemoryFunderReplyRepository()
    audit = InMemoryAuditLog()
    llm = _StubReplyLLM([])  # would assert if invoked

    with pytest.raises(ValueError, match="not valid JSON"):
        await process_funder_reply(
            _ctx(llm=llm, audit=audit, repo=repo),
            "not json at all",
        )
    assert llm.call_count == 0
    assert repo.replies() == []


async def test_worker_rejects_payload_missing_required_field() -> None:
    """JSON object missing ingested_via → ValueError up front."""
    repo = InMemoryFunderReplyRepository()
    audit = InMemoryAuditLog()
    llm = _StubReplyLLM([])

    bad = json.dumps(
        {
            "deal_id": str(uuid4()),
            "funder_id": str(uuid4()),
            "raw_text": "hi",
            # ingested_via missing
        }
    )
    with pytest.raises(ValueError, match="ingested_via"):
        await process_funder_reply(_ctx(llm=llm, audit=audit, repo=repo), bad)


async def test_worker_rejects_payload_bad_ingested_via_value() -> None:
    """Worker is strict about the IngestSource literal at the
    boundary — typo'd ingested_via never reaches ingest_reply."""
    repo = InMemoryFunderReplyRepository()
    audit = InMemoryAuditLog()
    llm = _StubReplyLLM([])

    bad = json.dumps(
        {
            "deal_id": str(uuid4()),
            "funder_id": str(uuid4()),
            "raw_text": "hi",
            "ingested_via": "imap",  # not a real source
        }
    )
    with pytest.raises(ValueError, match="ingested_via"):
        await process_funder_reply(_ctx(llm=llm, audit=audit, repo=repo), bad)
