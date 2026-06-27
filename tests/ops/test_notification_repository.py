"""Tests for InMemoryNotificationRepository."""

from __future__ import annotations

from uuid import uuid4

from aegis.ops.notification_repository import (
    InMemoryNotificationRepository,
)


def test_create_appends_and_lists_for_recipient() -> None:
    repo = InMemoryNotificationRepository()
    op = uuid4()
    row = repo.create(
        recipient_operator_id=op,
        event_type="parse_complete",
        payload={"document_id": "doc-1"},
        link_url="/ui/x",
    )
    assert row.recipient_operator_id == op
    assert row.event_type == "parse_complete"
    assert row.read_at is None

    listed = repo.list_for_operator(op)
    assert len(listed) == 1
    assert listed[0].id == row.id


def test_list_filters_to_recipient() -> None:
    repo = InMemoryNotificationRepository()
    op_a, op_b = uuid4(), uuid4()
    repo.create(recipient_operator_id=op_a, event_type="parse_complete")
    repo.create(recipient_operator_id=op_b, event_type="merchant_created")
    assert len(repo.list_for_operator(op_a)) == 1
    assert len(repo.list_for_operator(op_b)) == 1


def test_unread_count_excludes_read() -> None:
    repo = InMemoryNotificationRepository()
    op = uuid4()
    r1 = repo.create(recipient_operator_id=op, event_type="parse_complete")
    repo.create(recipient_operator_id=op, event_type="merchant_created")
    assert repo.unread_count(op) == 2

    repo.mark_read(r1.id)
    assert repo.unread_count(op) == 1


def test_only_unread_filter() -> None:
    repo = InMemoryNotificationRepository()
    op = uuid4()
    r1 = repo.create(recipient_operator_id=op, event_type="parse_complete")
    repo.create(recipient_operator_id=op, event_type="merchant_created")
    repo.mark_read(r1.id)
    assert len(repo.list_for_operator(op, only_unread=True)) == 1
    assert len(repo.list_for_operator(op, only_unread=False)) == 2


def test_mark_all_read_returns_flipped_count() -> None:
    repo = InMemoryNotificationRepository()
    op = uuid4()
    for _ in range(3):
        repo.create(recipient_operator_id=op, event_type="parse_complete")
    flipped = repo.mark_all_read(op)
    assert flipped == 3
    assert repo.unread_count(op) == 0
    # Idempotent — second call flips zero.
    assert repo.mark_all_read(op) == 0


def test_mark_read_idempotent() -> None:
    repo = InMemoryNotificationRepository()
    op = uuid4()
    row = repo.create(recipient_operator_id=op, event_type="parse_complete")
    repo.mark_read(row.id)
    first_read_at = repo.list_for_operator(op, only_unread=False)[0].read_at
    repo.mark_read(row.id)  # idempotent — should not change read_at
    second_read_at = repo.list_for_operator(op, only_unread=False)[0].read_at
    assert first_read_at == second_read_at


def test_list_orders_newest_first() -> None:
    repo = InMemoryNotificationRepository()
    op = uuid4()
    r1 = repo.create(recipient_operator_id=op, event_type="parse_complete")
    r2 = repo.create(recipient_operator_id=op, event_type="merchant_created")
    listed = repo.list_for_operator(op, only_unread=False)
    # InMemory uses datetime.now() which has microsecond resolution; the
    # second insert lands strictly after the first.
    assert listed[0].id == r2.id
    assert listed[1].id == r1.id
