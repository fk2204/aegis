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


def test_record_actor_email_lands_on_entry() -> None:
    """Per-row actor_email (mp Phase 11 #8) lands on the entry alongside
    the system-actor string. None when not supplied — the column is
    nullable on the audit_log table."""
    log = InMemoryAuditLog()
    log.record(
        actor="api",
        action="deal.score",
        actor_email="fkozina92@gmail.com",
    )
    assert log.entries[0]["actor_email"] == "fkozina92@gmail.com"

    log.record(actor="worker", action="document.parse.complete")
    assert log.entries[-1]["actor_email"] is None


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


def test_list_for_subject_filters_by_id_and_action() -> None:
    """list_for_subject returns newest-first rows for one subject, with
    optional action narrowing — backbone for the funder-response readback."""
    log = InMemoryAuditLog()
    merchant = uuid4()
    other = uuid4()

    log.record(
        actor="dashboard",
        action="deal.funder_response",
        subject_type="merchant",
        subject_id=merchant,
        details={"funder_id": "f1", "status": "pending"},
    )
    log.record(
        actor="dashboard",
        action="deal.submit_to_funders",
        subject_type="merchant",
        subject_id=merchant,
        details={"funder_names": ["LAG"]},
    )
    log.record(
        actor="dashboard",
        action="deal.funder_response",
        subject_type="merchant",
        subject_id=merchant,
        details={"funder_id": "f1", "status": "approved"},
    )
    # Noise: same action, different merchant — must not leak.
    log.record(
        actor="dashboard",
        action="deal.funder_response",
        subject_type="merchant",
        subject_id=other,
        details={"funder_id": "f1", "status": "declined"},
    )

    # No action filter — all rows for the subject, newest first.
    all_rows = log.list_for_subject(subject_type="merchant", subject_id=merchant)
    assert len(all_rows) == 3
    assert all_rows[0]["details"]["status"] == "approved"  # latest first

    # action filter — only deal.funder_response rows.
    responses = log.list_for_subject(
        subject_type="merchant",
        subject_id=merchant,
        action="deal.funder_response",
    )
    assert len(responses) == 2
    assert [r["details"]["status"] for r in responses] == ["approved", "pending"]

    # Wrong subject id — empty.
    assert log.list_for_subject(
        subject_type="merchant", subject_id=uuid4()
    ) == []
