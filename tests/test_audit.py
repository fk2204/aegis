"""Audit log tests — InMemoryAuditLog (Supabase impl needs a live DB)."""

from __future__ import annotations

from uuid import uuid4

from aegis.audit import InMemoryAuditLog


def test_record_appends_entry() -> None:
    log = InMemoryAuditLog()
    sid = uuid4()
    log.record(
        actor="op:abcd",
        action="document.upload",
        subject_type="document",
        subject_id=sid,
        details={"file_hash": "h" * 64, "byte_size": 1000},
    )
    assert len(log.entries) == 1
    e = log.entries[0]
    assert e["actor"] == "op:abcd"
    assert e["subject_id"] == str(sid)
    assert e["details"]["file_hash"] == "h" * 64


def test_pii_in_details_is_masked() -> None:
    log = InMemoryAuditLog()
    log.record(
        actor="op",
        action="merchant.create",
        details={"owner_name": "Alice", "industry": "retail"},
    )
    e = log.entries[0]
    assert e["details"]["owner_name"] == "***"
    assert e["details"]["industry"] == "retail"
