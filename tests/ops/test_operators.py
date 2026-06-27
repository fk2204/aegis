"""Tests for operator + role types (mp Phase 11 task #8)."""

from __future__ import annotations

import pytest

from aegis.ops.operators import (
    CF_ACCESS_EMAIL_HEADER,
    Operator,
    OperatorRole,
    resolve_operator_email,
)


def test_operator_role_values_match_db_constraint() -> None:
    """The string values MUST match the operators.role CHECK constraint
    or the table insert will fail at runtime.

    Migration 022 introduced the first three roles
    (``underwriter`` / ``compliance_reviewer`` / ``admin``); migration
    076 widened the CHECK to also accept ``viewer`` for the role-gate
    permission matrix.
    """
    expected = {"underwriter", "compliance_reviewer", "admin", "viewer"}
    assert {r.value for r in OperatorRole} == expected


def test_operator_model_defaults() -> None:
    op = Operator(email="user@example.com", display_name="Filip")
    assert op.role == OperatorRole.UNDERWRITER
    assert op.is_active is True


def test_operator_model_rejects_blank_display_name() -> None:
    with pytest.raises(ValueError, match="display_name"):
        Operator(email="user@example.com", display_name="")


@pytest.mark.asyncio
async def test_resolve_operator_email_present() -> None:
    """A real CF-Access header → lowercased email returned."""
    result = await resolve_operator_email("Filip@Commerafunding.com")
    assert result == "filip@commerafunding.com"


@pytest.mark.asyncio
async def test_resolve_operator_email_none() -> None:
    assert await resolve_operator_email(None) is None


@pytest.mark.asyncio
async def test_resolve_operator_email_malformed() -> None:
    """Malformed input (no @) returns None — caller decides on policy."""
    assert await resolve_operator_email("not-an-email") is None


def test_header_constant_is_lowercase() -> None:
    """Pydantic/FastAPI header lookups are case-insensitive but the
    canonical lowercased form is what we use elsewhere (e.g. tests
    that hit the API with TestClient)."""
    assert CF_ACCESS_EMAIL_HEADER == "cf-access-authenticated-user-email"
