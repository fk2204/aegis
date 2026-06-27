"""Router tests for /ui/notifications/* + bell partial integration."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_notification_repository,
    get_operator_repository,
    reset_dependency_caches,
)
from aegis.ops.notification_repository import (
    InMemoryNotificationRepository,
)
from aegis.ops.operator_repository import InMemoryOperatorRepository
from aegis.ops.operators import Operator, OperatorRole

_CF_HEADER = "cf-access-authenticated-user-email"


@pytest.fixture
def env() -> Iterator[
    tuple[
        TestClient,
        InMemoryOperatorRepository,
        InMemoryNotificationRepository,
        Operator,
    ]
]:
    reset_dependency_caches()
    operators = InMemoryOperatorRepository()
    notifications = InMemoryNotificationRepository()
    admin = Operator(
        email="admin@aegis.test",
        display_name="Admin",
        role=OperatorRole.ADMIN,
    )
    operators._seed(admin)
    app = create_app()
    app.dependency_overrides[get_notification_repository] = lambda: notifications
    app.dependency_overrides[get_operator_repository] = lambda: operators
    with TestClient(app) as client:
        yield client, operators, notifications, admin
    app.dependency_overrides.clear()
    reset_dependency_caches()


def test_unread_count_returns_empty_when_zero(env) -> None:  # type: ignore[no-untyped-def]
    client, _o, _n, admin = env
    resp = client.get("/ui/notifications/unread-count", headers={_CF_HEADER: admin.email})
    assert resp.status_code == 200
    # When count is 0 the template returns empty content (no badge).
    assert b"bell-badge" not in resp.content


def test_unread_count_renders_badge_when_unread_present(env) -> None:  # type: ignore[no-untyped-def]
    client, _o, notifications, admin = env
    notifications.create(
        recipient_operator_id=admin.id,
        event_type="parse_complete",
    )
    resp = client.get("/ui/notifications/unread-count", headers={_CF_HEADER: admin.email})
    assert resp.status_code == 200
    assert b"bell-badge" in resp.content
    assert b"1" in resp.content


def test_dropdown_renders_empty_state(env) -> None:  # type: ignore[no-untyped-def]
    client, _o, _n, admin = env
    resp = client.get("/ui/notifications/dropdown", headers={_CF_HEADER: admin.email})
    assert resp.status_code == 200
    assert b"No notifications yet" in resp.content


def test_dropdown_lists_recent(env) -> None:  # type: ignore[no-untyped-def]
    client, _o, notifications, admin = env
    notifications.create(
        recipient_operator_id=admin.id,
        event_type="merchant_created",
        payload={"summary": "New merchant: Acme Co"},
    )
    notifications.create(
        recipient_operator_id=admin.id,
        event_type="parse_complete",
        payload={"summary": "Parse finished (proceed)"},
    )
    resp = client.get("/ui/notifications/dropdown", headers={_CF_HEADER: admin.email})
    assert resp.status_code == 200
    body = resp.text
    assert "Acme Co" in body
    assert "proceed" in body


def test_mark_read_returns_decremented_badge(env) -> None:  # type: ignore[no-untyped-def]
    client, _o, notifications, admin = env
    row = notifications.create(
        recipient_operator_id=admin.id,
        event_type="parse_complete",
    )
    notifications.create(
        recipient_operator_id=admin.id,
        event_type="merchant_created",
    )
    resp = client.post(
        f"/ui/notifications/{row.id}/mark-read",
        headers={_CF_HEADER: admin.email},
    )
    assert resp.status_code == 200
    assert b"bell-badge" in resp.content
    assert b"1" in resp.content
    # Repo state confirms the flip.
    assert notifications.unread_count(admin.id) == 1


def test_unknown_notification_id_is_noop(env) -> None:  # type: ignore[no-untyped-def]
    client, _o, notifications, admin = env
    notifications.create(
        recipient_operator_id=admin.id,
        event_type="parse_complete",
    )
    resp = client.post(
        f"/ui/notifications/{uuid4()}/mark-read",
        headers={_CF_HEADER: admin.email},
    )
    assert resp.status_code == 200
    # Mark-read on unknown id is a no-op; count remains 1.
    assert notifications.unread_count(admin.id) == 1


def test_dropdown_is_scoped_to_current_operator(env) -> None:  # type: ignore[no-untyped-def]
    client, operators, notifications, admin = env
    other = Operator(
        email="other@aegis.test",
        display_name="Other",
        role=OperatorRole.UNDERWRITER,
    )
    operators._seed(other)
    notifications.create(
        recipient_operator_id=admin.id,
        event_type="merchant_created",
        payload={"summary": "For Admin"},
    )
    notifications.create(
        recipient_operator_id=other.id,
        event_type="parse_complete",
        payload={"summary": "For Other"},
    )
    resp = client.get("/ui/notifications/dropdown", headers={_CF_HEADER: other.email})
    assert resp.status_code == 200
    body = resp.text
    assert "For Other" in body
    assert "For Admin" not in body
