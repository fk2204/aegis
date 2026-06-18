"""Pure-orchestration tests for
``aegis.merchants.close_context.refresh_close_context_for_merchant``.

Stubs every collaborator (CloseClient methods, MerchantRepository,
AuditLog, lead_fetcher) so the test exercises only the orchestration
logic: read → join → set_close_context → audit.

Audit-row shape pinned per Feature D spec: ``details`` carries
``notes_pulled`` / ``calls_pulled`` / ``lead_description_present`` and
the close_lead_id — never the body content.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aegis.audit import InMemoryAuditLog
from aegis.close.client import CloseCall, CloseNote
from aegis.merchants.close_context import (
    RECENT_CALLS_LIMIT,
    RECENT_NOTES_LIMIT,
    refresh_close_context_for_merchant,
)
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository


@dataclass
class _StubCloseClient:
    """Captures call args and returns scripted notes/calls."""

    notes: list[CloseNote] = field(default_factory=list)
    calls: list[CloseCall] = field(default_factory=list)
    notes_calls_seen: list[tuple[str, int]] = field(default_factory=list)
    calls_calls_seen: list[tuple[str, int]] = field(default_factory=list)

    def list_recent_notes(self, lead_id: str, limit: int = 5) -> list[CloseNote]:
        self.notes_calls_seen.append((lead_id, limit))
        return self.notes

    def list_recent_calls(self, lead_id: str, limit: int = 3) -> list[CloseCall]:
        self.calls_calls_seen.append((lead_id, limit))
        return self.calls

    def get_lead(self, lead_id: str) -> dict[str, Any]:
        # Not used in these tests — they all inject ``lead_fetcher``
        # directly so the default fetcher closure is never built.
        raise AssertionError("get_lead should be unused in these tests")


def _seed_merchant(
    repo: InMemoryMerchantRepository,
    *,
    close_lead_id: str = "lead_test",
) -> MerchantRow:
    m = MerchantRow(
        business_name="Context Test LLC",
        owner_name="Owner",
        state="CA",
        close_lead_id=close_lead_id,
    )
    return repo.upsert(m)


def test_refresh_populates_all_three_columns() -> None:
    """Lead description + notes + calls land on the merchant row;
    bodies are joined with the documented separator."""
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = _seed_merchant(repo)

    stub = _StubCloseClient(
        notes=[
            CloseNote(id="n1", note="Note 1 body"),
            CloseNote(id="n2", note="Note 2 body"),
        ],
        calls=[
            CloseCall(id="c1", note="Call 1 disposition"),
        ],
    )

    refresh_close_context_for_merchant(
        merchant.id,
        "lead_test",
        close_client=stub,  # type: ignore[arg-type]
        merchants_repo=repo,
        audit=audit,
        lead_fetcher=lambda _lid: {"description": "Lead-desc body"},
    )

    updated = repo.get(merchant.id)
    assert updated.close_lead_description == "Lead-desc body"
    assert updated.close_notes_summary == "Note 1 body\n---\nNote 2 body"
    assert updated.close_call_transcripts == "Call 1 disposition"


def test_refresh_uses_default_limits() -> None:
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = _seed_merchant(repo)
    stub = _StubCloseClient()

    refresh_close_context_for_merchant(
        merchant.id,
        "lead_test",
        close_client=stub,  # type: ignore[arg-type]
        merchants_repo=repo,
        audit=audit,
        lead_fetcher=lambda _lid: {},
    )

    assert stub.notes_calls_seen == [("lead_test", RECENT_NOTES_LIMIT)]
    assert stub.calls_calls_seen == [("lead_test", RECENT_CALLS_LIMIT)]
    assert RECENT_NOTES_LIMIT == 5
    assert RECENT_CALLS_LIMIT == 3


def test_refresh_writes_audit_row_with_counts_only() -> None:
    """Audit details must contain counts + presence flag — never the
    bodies themselves (CLAUDE.md PII rule)."""
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = _seed_merchant(repo)

    stub = _StubCloseClient(
        notes=[CloseNote(id=f"n{i}", note=f"Body {i} with PII-like content") for i in range(3)],
        calls=[CloseCall(id="c1", note="Call body with PII")],
    )

    refresh_close_context_for_merchant(
        merchant.id,
        "lead_test",
        close_client=stub,  # type: ignore[arg-type]
        merchants_repo=repo,
        audit=audit,
        lead_fetcher=lambda _lid: {"description": "Lead body"},
    )

    refreshes = [e for e in audit.entries if e["action"] == "merchant.close_context.refreshed"]
    assert len(refreshes) == 1
    details = refreshes[0]["details"]
    assert details["close_lead_id"] == "lead_test"
    assert details["notes_pulled"] == 3
    assert details["calls_pulled"] == 1
    assert details["lead_description_present"] is True
    # Bodies absent from audit row.
    serialized = repr(details)
    assert "Body 0" not in serialized
    assert "Call body" not in serialized
    assert "Lead body" not in serialized


def test_refresh_lead_description_present_flag_false_when_missing() -> None:
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = _seed_merchant(repo)
    stub = _StubCloseClient()

    refresh_close_context_for_merchant(
        merchant.id,
        "lead_test",
        close_client=stub,  # type: ignore[arg-type]
        merchants_repo=repo,
        audit=audit,
        lead_fetcher=lambda _lid: {},  # no description key
    )

    refreshes = [e for e in audit.entries if e["action"] == "merchant.close_context.refreshed"]
    assert refreshes[0]["details"]["lead_description_present"] is False

    updated = repo.get(merchant.id)
    assert updated.close_lead_description is None


def test_refresh_filters_empty_notes_and_calls_from_join() -> None:
    """Notes/calls with None or empty ``note`` field don't poison the
    joined string with bare separators."""
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = _seed_merchant(repo)

    stub = _StubCloseClient(
        notes=[
            CloseNote(id="n1", note="Real note"),
            CloseNote(id="n2", note=None),
            CloseNote(id="n3", note="   "),
            CloseNote(id="n4", note="Another real note"),
        ],
        calls=[],
    )

    refresh_close_context_for_merchant(
        merchant.id,
        "lead_test",
        close_client=stub,  # type: ignore[arg-type]
        merchants_repo=repo,
        audit=audit,
        lead_fetcher=lambda _lid: {},
    )

    updated = repo.get(merchant.id)
    assert updated.close_notes_summary == "Real note\n---\nAnother real note"
    assert updated.close_call_transcripts is None


def test_refresh_passes_lead_id_through_to_fetcher() -> None:
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = _seed_merchant(repo)
    stub = _StubCloseClient()
    seen_lead_ids: list[str] = []

    def _fetcher(lead_id: str) -> dict[str, Any]:
        seen_lead_ids.append(lead_id)
        return {}

    refresh_close_context_for_merchant(
        merchant.id,
        "lead_payload_id_xyz",
        close_client=stub,  # type: ignore[arg-type]
        merchants_repo=repo,
        audit=audit,
        lead_fetcher=_fetcher,
    )

    assert seen_lead_ids == ["lead_payload_id_xyz"]


def test_refresh_subject_id_is_merchant_id() -> None:
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = _seed_merchant(repo)
    stub = _StubCloseClient()

    refresh_close_context_for_merchant(
        merchant.id,
        "lead_test",
        close_client=stub,  # type: ignore[arg-type]
        merchants_repo=repo,
        audit=audit,
        lead_fetcher=lambda _lid: {},
    )

    refreshes = [e for e in audit.entries if e["action"] == "merchant.close_context.refreshed"]
    assert refreshes[0]["subject_type"] == "merchant"
    assert refreshes[0]["subject_id"] == str(merchant.id)
