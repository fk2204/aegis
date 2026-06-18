"""Feature C — operator notes repository round-trip.

Covers the InMemory implementation of ``add_note`` / ``list_notes`` on
``MerchantRepository`` (migration 066). The Supabase implementation is
covered indirectly via the route tests + the same payload shape; this
file pins the in-memory contract that the router relies on.

Three concerns:

  1. ``add_note`` writes a row that round-trips through ``list_notes``
     with the exact body / actor passed in and a ``created_at``
     timestamp.
  2. ``list_notes`` returns rows newest-first when more than one note
     exists for the same merchant.
  3. ``list_notes`` isolates rows by merchant and respects ``limit``.
"""

from __future__ import annotations

import time
from uuid import uuid4

import pytest

from aegis.merchants.models import MERCHANT_NOTE_MAX_CHARS
from aegis.merchants.repository import InMemoryMerchantRepository


@pytest.fixture
def repo() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


def test_add_note_writes_row_returned_by_list_notes(
    repo: InMemoryMerchantRepository,
) -> None:
    merchant_id = uuid4()
    saved = repo.add_note(
        merchant_id=merchant_id,
        body="broker mentioned past stacking issues",
        actor="filip@commerafunding.com",
    )

    assert saved.merchant_id == merchant_id
    assert saved.body == "broker mentioned past stacking issues"
    assert saved.actor == "filip@commerafunding.com"
    assert saved.created_at is not None

    rows = repo.list_notes(merchant_id=merchant_id)
    assert len(rows) == 1
    assert rows[0].id == saved.id
    assert rows[0].body == "broker mentioned past stacking issues"
    assert rows[0].actor == "filip@commerafunding.com"


def test_list_notes_returns_newest_first(
    repo: InMemoryMerchantRepository,
) -> None:
    merchant_id = uuid4()
    repo.add_note(merchant_id=merchant_id, body="first", actor="a@example.com")
    # Force a measurable timestamp gap so the in-memory ``datetime.now``
    # ordering is unambiguous on fast hardware. 1ms is well above the
    # platform-wide ``datetime.utcnow`` resolution on Linux + Windows.
    time.sleep(0.005)
    repo.add_note(merchant_id=merchant_id, body="second", actor="b@example.com")
    time.sleep(0.005)
    repo.add_note(merchant_id=merchant_id, body="third", actor="c@example.com")

    rows = repo.list_notes(merchant_id=merchant_id)
    assert [r.body for r in rows] == ["third", "second", "first"]


def test_list_notes_isolates_by_merchant_and_respects_limit(
    repo: InMemoryMerchantRepository,
) -> None:
    a = uuid4()
    b = uuid4()
    for i in range(5):
        repo.add_note(merchant_id=a, body=f"a-{i}", actor="actor")
    repo.add_note(merchant_id=b, body="b-only", actor="actor")

    a_rows = repo.list_notes(merchant_id=a)
    assert len(a_rows) == 5
    assert all(r.merchant_id == a for r in a_rows)

    b_rows = repo.list_notes(merchant_id=b)
    assert len(b_rows) == 1
    assert b_rows[0].body == "b-only"

    capped = repo.list_notes(merchant_id=a, limit=3)
    assert len(capped) == 3


def test_add_note_rejects_empty_body(
    repo: InMemoryMerchantRepository,
) -> None:
    """The Pydantic model's ``min_length=1`` enforces the contract at the
    repo boundary — a route bug that lets an empty body through is caught
    here instead of producing a row that would later fail the DB CHECK
    constraint."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        repo.add_note(merchant_id=uuid4(), body="", actor="actor")


def test_add_note_rejects_oversize_body(
    repo: InMemoryMerchantRepository,
) -> None:
    """The ``max_length`` mirrors the DB CHECK constraint. The route also
    400s on this case, but the model is the canonical guard so a future
    caller that bypasses the route cannot land an oversize row."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        repo.add_note(
            merchant_id=uuid4(),
            body="x" * (MERCHANT_NOTE_MAX_CHARS + 1),
            actor="actor",
        )
