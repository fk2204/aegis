"""Tests for the in-memory OperatorRepository + role helpers."""

from __future__ import annotations

from uuid import uuid4

import pytest

from aegis.ops.operator_repository import (
    InMemoryOperatorRepository,
    OperatorWriteError,
)
from aegis.ops.operators import (
    EffectiveRole,
    Operator,
    OperatorRole,
    effective_role,
)


def test_get_or_create_defaults_to_underwriter() -> None:
    repo = InMemoryOperatorRepository()
    op = repo.get_or_create_by_email(email="new@aegis.test")
    assert op.role == OperatorRole.UNDERWRITER
    assert op.is_active is True


def test_get_or_create_is_idempotent() -> None:
    repo = InMemoryOperatorRepository()
    a = repo.get_or_create_by_email(email="filip@aegis.test")
    b = repo.get_or_create_by_email(email="FILIP@aegis.test")  # different casing
    assert a.id == b.id


def test_get_or_create_rejects_invalid_email() -> None:
    repo = InMemoryOperatorRepository()
    with pytest.raises(OperatorWriteError):
        repo.get_or_create_by_email(email="not-an-email")


def test_list_admins_excludes_other_roles() -> None:
    repo = InMemoryOperatorRepository()
    repo._seed(
        Operator(
            id=uuid4(),
            email="a@aegis.test",
            display_name="A",
            role=OperatorRole.ADMIN,
        )
    )
    repo._seed(
        Operator(
            id=uuid4(),
            email="u@aegis.test",
            display_name="U",
            role=OperatorRole.UNDERWRITER,
        )
    )
    repo._seed(
        Operator(
            id=uuid4(),
            email="v@aegis.test",
            display_name="V",
            role=OperatorRole.VIEWER,
        )
    )
    admins = repo.list_admins()
    assert {a.email for a in admins} == {"a@aegis.test"}


def test_list_active_excludes_inactive() -> None:
    repo = InMemoryOperatorRepository()
    repo._seed(
        Operator(
            id=uuid4(),
            email="a@aegis.test",
            display_name="A",
            role=OperatorRole.ADMIN,
            is_active=True,
        )
    )
    repo._seed(
        Operator(
            id=uuid4(),
            email="b@aegis.test",
            display_name="B",
            role=OperatorRole.ADMIN,
            is_active=False,
        )
    )
    active = repo.list_active()
    assert {a.email for a in active} == {"a@aegis.test"}


def test_effective_role_compliance_reviewer_is_viewer() -> None:
    assert effective_role(OperatorRole.COMPLIANCE_REVIEWER) == EffectiveRole.VIEWER


def test_effective_role_one_to_one_otherwise() -> None:
    assert effective_role(OperatorRole.ADMIN) == EffectiveRole.ADMIN
    assert effective_role(OperatorRole.UNDERWRITER) == EffectiveRole.UNDERWRITER
    assert effective_role(OperatorRole.VIEWER) == EffectiveRole.VIEWER
