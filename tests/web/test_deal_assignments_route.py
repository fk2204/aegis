"""Router tests for /ui/merchants/{id}/assign + /unassign + modal.

Covers:
  * Assigning an operator writes the row + audit trail (created).
  * Reassigning writes BOTH a removed + a created audit row.
  * Unassign removes the row + writes a removed audit row.
  * 403 for viewer role.
  * 404 for unknown merchant / unknown operator.
  * GET assignment-modal renders the operator list.
  * Merchants list shows Assignee column + ?mine=1 filter narrows.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_deal_assignment_repository,
    get_merchant_repository,
    get_operator_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.ops.deal_assignment_repository import (
    InMemoryDealAssignmentRepository,
)
from aegis.ops.operator_repository import InMemoryOperatorRepository
from aegis.ops.operators import Operator, OperatorRole

_CF_HEADER = "cf-access-authenticated-user-email"


@pytest.fixture
def env() -> Iterator[
    tuple[
        TestClient,
        InMemoryAuditLog,
        InMemoryMerchantRepository,
        InMemoryDealAssignmentRepository,
        InMemoryOperatorRepository,
        Operator,
        Operator,
        Operator,
        MerchantRow,
    ]
]:
    """Build a TestClient with a pre-seeded merchant + 3 operators
    (admin / underwriter / viewer)."""
    reset_dependency_caches()

    audit = InMemoryAuditLog()
    merchants = InMemoryMerchantRepository()
    assignments = InMemoryDealAssignmentRepository()
    operators = InMemoryOperatorRepository()

    admin = Operator(
        email="admin@aegis.test",
        display_name="Admin",
        role=OperatorRole.ADMIN,
    )
    uw = Operator(
        email="uw@aegis.test",
        display_name="Uw",
        role=OperatorRole.UNDERWRITER,
    )
    viewer = Operator(
        email="viewer@aegis.test",
        display_name="Viewer",
        role=OperatorRole.VIEWER,
    )
    operators._seed(admin)
    operators._seed(uw)
    operators._seed(viewer)

    merchant = MerchantRow(
        id=uuid4(),
        business_name="Test Merchant",
        owner_name="Owner",
        state="CA",
    )
    merchants.upsert(merchant)

    app = create_app()
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_merchant_repository] = lambda: merchants
    app.dependency_overrides[get_deal_assignment_repository] = lambda: assignments
    app.dependency_overrides[get_operator_repository] = lambda: operators

    with TestClient(app) as client:
        yield (
            client,
            audit,
            merchants,
            assignments,
            operators,
            admin,
            uw,
            viewer,
            merchant,
        )

    app.dependency_overrides.clear()
    reset_dependency_caches()


def test_assign_writes_row_and_audit_created(env) -> None:  # type: ignore[no-untyped-def]
    (
        client,
        audit,
        _merchants,
        assignments,
        _operators,
        admin,
        uw,
        _viewer,
        merchant,
    ) = env

    resp = client.post(
        f"/ui/merchants/{merchant.id}/assign",
        data={"operator_id": str(uw.id)},
        headers={_CF_HEADER: admin.email},
    )
    assert resp.status_code == 200
    assert b"Uw" in resp.content

    row = assignments.get_for_merchant(merchant.id)
    assert row is not None
    assert row.operator_id == uw.id
    assert row.assigned_by == admin.id

    actions = [e["action"] for e in audit.entries]
    assert actions == ["merchant.assignment.created"]
    details = audit.entries[0]["details"]
    assert details["operator_id"] == str(uw.id)


def test_reassign_writes_removed_then_created(env) -> None:  # type: ignore[no-untyped-def]
    (
        client,
        audit,
        _merchants,
        _assignments,
        _operators,
        admin,
        uw,
        _viewer,
        merchant,
    ) = env

    client.post(
        f"/ui/merchants/{merchant.id}/assign",
        data={"operator_id": str(uw.id)},
        headers={_CF_HEADER: admin.email},
    )
    client.post(
        f"/ui/merchants/{merchant.id}/assign",
        data={"operator_id": str(admin.id)},
        headers={_CF_HEADER: admin.email},
    )

    actions = [e["action"] for e in audit.entries]
    assert actions == [
        "merchant.assignment.created",
        "merchant.assignment.removed",
        "merchant.assignment.created",
    ]
    assert audit.entries[1]["details"]["previous_operator_id"] == str(uw.id)
    assert audit.entries[1]["details"]["reason"] == "reassign"


def test_unassign_removes_row_and_writes_audit(env) -> None:  # type: ignore[no-untyped-def]
    (
        client,
        audit,
        _merchants,
        assignments,
        _operators,
        admin,
        uw,
        _viewer,
        merchant,
    ) = env

    client.post(
        f"/ui/merchants/{merchant.id}/assign",
        data={"operator_id": str(uw.id)},
        headers={_CF_HEADER: admin.email},
    )
    resp = client.post(
        f"/ui/merchants/{merchant.id}/unassign",
        headers={_CF_HEADER: admin.email},
    )
    assert resp.status_code == 200
    assert assignments.get_for_merchant(merchant.id) is None

    last = audit.entries[-1]
    assert last["action"] == "merchant.assignment.removed"
    assert last["details"]["reason"] == "unassign"


def test_assign_forbidden_for_viewer(env) -> None:  # type: ignore[no-untyped-def]
    client, _audit, _m, _a, _o, _admin, uw, viewer, merchant = env
    resp = client.post(
        f"/ui/merchants/{merchant.id}/assign",
        data={"operator_id": str(uw.id)},
        headers={_CF_HEADER: viewer.email},
    )
    assert resp.status_code == 403
    assert b"forbidden" in resp.content.lower() or b"403" in resp.content


def test_assign_returns_404_for_unknown_merchant(env) -> None:  # type: ignore[no-untyped-def]
    client, _audit, _m, _a, _o, admin, uw, _v, _merchant = env
    resp = client.post(
        f"/ui/merchants/{uuid4()}/assign",
        data={"operator_id": str(uw.id)},
        headers={_CF_HEADER: admin.email},
    )
    assert resp.status_code == 404


def test_assign_returns_404_for_unknown_operator(env) -> None:  # type: ignore[no-untyped-def]
    client, _audit, _m, _a, _o, admin, _uw, _v, merchant = env
    resp = client.post(
        f"/ui/merchants/{merchant.id}/assign",
        data={"operator_id": str(uuid4())},
        headers={_CF_HEADER: admin.email},
    )
    assert resp.status_code == 404


def test_modal_renders_active_operator_list(env) -> None:  # type: ignore[no-untyped-def]
    client, _audit, _m, _a, _o, admin, uw, viewer, merchant = env
    resp = client.get(
        f"/ui/merchants/{merchant.id}/assignment-modal",
        headers={_CF_HEADER: admin.email},
    )
    assert resp.status_code == 200
    body = resp.text
    assert admin.display_name in body
    assert uw.display_name in body
    assert viewer.display_name in body


def test_merchants_list_shows_assignee_column(env) -> None:  # type: ignore[no-untyped-def]
    client, _audit, _merchants, _assignments, _operators, admin, uw, _v, merchant = env

    # Assign uw to the test merchant.
    client.post(
        f"/ui/merchants/{merchant.id}/assign",
        data={"operator_id": str(uw.id)},
        headers={_CF_HEADER: admin.email},
    )

    resp = client.get("/ui/merchants", headers={_CF_HEADER: admin.email})
    assert resp.status_code == 200
    assert b'data-test-id="merchants-list-assignee"' in resp.content
    assert b"Uw" in resp.content


def test_mine_filter_narrows_to_current_operator(env) -> None:  # type: ignore[no-untyped-def]
    client, _audit, merchants, _assignments, _operators, admin, uw, _v, merchant = env

    # Add a second merchant assigned to a different operator (admin).
    other = MerchantRow(
        id=uuid4(),
        business_name="Other Merchant",
        owner_name="Other Owner",
        state="NY",
    )
    merchants.upsert(other)

    # Assign first merchant to uw, second to admin.
    client.post(
        f"/ui/merchants/{merchant.id}/assign",
        data={"operator_id": str(uw.id)},
        headers={_CF_HEADER: admin.email},
    )
    client.post(
        f"/ui/merchants/{other.id}/assign",
        data={"operator_id": str(admin.id)},
        headers={_CF_HEADER: admin.email},
    )

    # Hit /ui/merchants?mine=1 as admin — should ONLY see "Other Merchant".
    resp = client.get("/ui/merchants?mine=1", headers={_CF_HEADER: admin.email})
    assert resp.status_code == 200
    body = resp.text
    assert "Other Merchant" in body
    assert "Test Merchant" not in body


def test_mine_filter_inactive_shows_all(env) -> None:  # type: ignore[no-untyped-def]
    client, _audit, _m, _a, _o, admin, _uw, _v, _merchant = env
    resp = client.get("/ui/merchants", headers={_CF_HEADER: admin.email})
    assert resp.status_code == 200
    assert b'data-test-id="merchants-filter-mine"' in resp.content
