"""Tests for InMemoryDealAssignmentRepository."""

from __future__ import annotations

from uuid import uuid4

import pytest

from aegis.ops.deal_assignment_repository import (
    InMemoryDealAssignmentRepository,
)


def test_unassigned_merchant_returns_none() -> None:
    repo = InMemoryDealAssignmentRepository()
    assert repo.get_for_merchant(uuid4()) is None


def test_assign_creates_and_returns_row() -> None:
    repo = InMemoryDealAssignmentRepository()
    merchant_id = uuid4()
    operator_id = uuid4()
    assigner_id = uuid4()

    row = repo.assign(
        merchant_id=merchant_id,
        operator_id=operator_id,
        assigned_by=assigner_id,
    )

    assert row.merchant_id == merchant_id
    assert row.operator_id == operator_id
    assert row.assigned_by == assigner_id
    fetched = repo.get_for_merchant(merchant_id)
    assert fetched is not None
    assert fetched.id == row.id


def test_reassign_overwrites_previous_row() -> None:
    repo = InMemoryDealAssignmentRepository()
    merchant_id = uuid4()
    op_a, op_b = uuid4(), uuid4()
    assigner = uuid4()

    repo.assign(merchant_id=merchant_id, operator_id=op_a, assigned_by=assigner)
    second = repo.assign(merchant_id=merchant_id, operator_id=op_b, assigned_by=assigner)

    current = repo.get_for_merchant(merchant_id)
    assert current is not None
    assert current.operator_id == op_b
    assert current.id == second.id


def test_unassign_returns_previous_row_then_none() -> None:
    repo = InMemoryDealAssignmentRepository()
    merchant_id = uuid4()
    operator_id = uuid4()
    assigner = uuid4()

    repo.assign(merchant_id=merchant_id, operator_id=operator_id, assigned_by=assigner)
    removed = repo.unassign(merchant_id)
    assert removed is not None
    assert removed.operator_id == operator_id

    assert repo.get_for_merchant(merchant_id) is None
    assert repo.unassign(merchant_id) is None  # idempotent


def test_list_for_operator_only_returns_that_operators_rows() -> None:
    repo = InMemoryDealAssignmentRepository()
    op_a, op_b, assigner = uuid4(), uuid4(), uuid4()
    m1, m2, m3 = uuid4(), uuid4(), uuid4()

    repo.assign(merchant_id=m1, operator_id=op_a, assigned_by=assigner)
    repo.assign(merchant_id=m2, operator_id=op_a, assigned_by=assigner)
    repo.assign(merchant_id=m3, operator_id=op_b, assigned_by=assigner)

    a_rows = repo.list_for_operator(op_a)
    assert {r.merchant_id for r in a_rows} == {m1, m2}

    b_rows = repo.list_for_operator(op_b)
    assert {r.merchant_id for r in b_rows} == {m3}


def test_map_by_merchant_returns_only_requested_ids() -> None:
    repo = InMemoryDealAssignmentRepository()
    op, assigner = uuid4(), uuid4()
    m1, m2, m3 = uuid4(), uuid4(), uuid4()
    not_in_input = uuid4()

    repo.assign(merchant_id=m1, operator_id=op, assigned_by=assigner)
    repo.assign(merchant_id=m2, operator_id=op, assigned_by=assigner)
    repo.assign(merchant_id=not_in_input, operator_id=op, assigned_by=assigner)

    result = repo.map_by_merchant([m1, m2, m3])
    assert set(result.keys()) == {m1, m2}  # m3 unassigned, not_in_input excluded


def test_map_by_merchant_empty_input_returns_empty() -> None:
    repo = InMemoryDealAssignmentRepository()
    assert repo.map_by_merchant([]) == {}


@pytest.mark.parametrize("ids_count", [0, 1, 5])
def test_map_by_merchant_handles_various_sizes(ids_count: int) -> None:
    repo = InMemoryDealAssignmentRepository()
    ids = [uuid4() for _ in range(ids_count)]
    op, assigner = uuid4(), uuid4()
    for m in ids:
        repo.assign(merchant_id=m, operator_id=op, assigned_by=assigner)
    result = repo.map_by_merchant(ids)
    assert len(result) == ids_count
