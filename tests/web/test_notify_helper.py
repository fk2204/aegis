"""Tests for aegis.web._notify recipient-selection policy."""

from __future__ import annotations

from uuid import uuid4

from aegis.ops.deal_assignment_repository import (
    InMemoryDealAssignmentRepository,
)
from aegis.ops.notification_repository import (
    InMemoryNotificationRepository,
)
from aegis.ops.operator_repository import InMemoryOperatorRepository
from aegis.ops.operators import Operator, OperatorRole
from aegis.web._notify import notify_merchant_created, notify_parse_complete


def _admin(email: str = "admin@aegis.test") -> Operator:
    return Operator(
        email=email,
        display_name=email.split("@", 1)[0],
        role=OperatorRole.ADMIN,
    )


def _underwriter(email: str = "uw@aegis.test") -> Operator:
    return Operator(
        email=email,
        display_name=email.split("@", 1)[0],
        role=OperatorRole.UNDERWRITER,
    )


def test_notify_merchant_created_fans_to_all_admins() -> None:
    operators = InMemoryOperatorRepository()
    notifications = InMemoryNotificationRepository()
    admin_a = _admin("a@aegis.test")
    admin_b = _admin("b@aegis.test")
    not_admin = _underwriter("uw@aegis.test")
    operators._seed(admin_a)
    operators._seed(admin_b)
    operators._seed(not_admin)

    written = notify_merchant_created(
        merchant_id=uuid4(),
        business_name="Acme Co",
        operators=operators,
        notifications=notifications,
    )
    assert written == 2
    # Underwriter does NOT receive a merchant_created notification.
    assert notifications.unread_count(not_admin.id) == 0
    assert notifications.unread_count(admin_a.id) == 1
    assert notifications.unread_count(admin_b.id) == 1


def test_notify_merchant_created_with_zero_admins_returns_zero() -> None:
    operators = InMemoryOperatorRepository()
    notifications = InMemoryNotificationRepository()
    operators._seed(_underwriter())
    written = notify_merchant_created(
        merchant_id=uuid4(),
        business_name="X",
        operators=operators,
        notifications=notifications,
    )
    assert written == 0


def test_notify_parse_complete_targets_assignee_when_set() -> None:
    operators = InMemoryOperatorRepository()
    notifications = InMemoryNotificationRepository()
    assignments = InMemoryDealAssignmentRepository()

    admin = _admin()
    uw = _underwriter()
    operators._seed(admin)
    operators._seed(uw)

    merchant_id = uuid4()
    assignments.assign(
        merchant_id=merchant_id,
        operator_id=uw.id,
        assigned_by=admin.id,
    )

    written = notify_parse_complete(
        merchant_id=merchant_id,
        document_id=uuid4(),
        parse_status="proceed",
        operators=operators,
        assignments=assignments,
        notifications=notifications,
    )
    assert written == 1
    assert notifications.unread_count(uw.id) == 1
    # Admin is NOT notified when an assignee exists.
    assert notifications.unread_count(admin.id) == 0


def test_notify_parse_complete_falls_back_to_admins_when_unassigned() -> None:
    operators = InMemoryOperatorRepository()
    notifications = InMemoryNotificationRepository()
    assignments = InMemoryDealAssignmentRepository()

    admin_a = _admin("a@aegis.test")
    admin_b = _admin("b@aegis.test")
    operators._seed(admin_a)
    operators._seed(admin_b)

    written = notify_parse_complete(
        merchant_id=uuid4(),
        document_id=uuid4(),
        parse_status="manual_review",
        operators=operators,
        assignments=assignments,
        notifications=notifications,
    )
    assert written == 2
    assert notifications.unread_count(admin_a.id) == 1
    assert notifications.unread_count(admin_b.id) == 1
