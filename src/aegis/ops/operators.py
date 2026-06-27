"""Operator + role types (mp Phase 11 task #8).

The schema in migration 022 introduces an ``operators`` table that
binds an email (the Cloudflare Access SSO identity) to one of three
roles:

  * ``underwriter``         — default; can score deals, push to Close.
  * ``compliance_reviewer`` — can approve overrides + read audit_log.
  * ``admin``               — full surface, the operator-owner role.

This module gives the application a Pydantic-typed view of the table
plus a request-context resolver that reads the
``CF-Access-Authenticated-User-Email`` header set by Cloudflare
Tunnel. The resolver is used by future role-gated endpoints; the
audit-log integration already lives at the call sites (every
``audit.record(...)`` call passes an ``actor=`` and, post-merge,
will also pass ``actor_email=`` derived from the resolver).
"""

from __future__ import annotations

# Compiled lazily so importing this module doesn't pull in ``re`` for
# nothing. Pattern intentionally simple — full RFC 5322 validation
# isn't worth a new dep (``email-validator``); the DB CHECK constraint
# on ``operators.email`` is the actual enforcement.
import re
from enum import StrEnum
from typing import Annotated, Final
from uuid import UUID, uuid4

from fastapi import Header
from pydantic import BaseModel, Field

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class OperatorRole(StrEnum):
    """Roles accepted by the ``operators.role`` CHECK constraint.

    Migration 022 introduced the first three (``underwriter`` /
    ``compliance_reviewer`` / ``admin``). Migration 076 widened the
    CHECK to also accept ``viewer`` — the read-only role used by the
    role-gate permission matrix. ``compliance_reviewer`` is kept for
    back-compat with rows created before 2026-06; the role gate treats
    it as a viewer at the application layer.
    """

    UNDERWRITER = "underwriter"
    COMPLIANCE_REVIEWER = "compliance_reviewer"
    ADMIN = "admin"
    VIEWER = "viewer"


# ---------------------------------------------------------------------------
# Effective-role mapping for the permission gate.
#
# The product matrix only cares about admin / underwriter / viewer.
# ``compliance_reviewer`` rows collapse to ``viewer`` for permission
# decisions but keep their stored value (so an audit reading the
# ``operators`` table sees the original assignment).
# ---------------------------------------------------------------------------


class EffectiveRole(StrEnum):
    """Effective role used by the permission gate.

    Distinct from ``OperatorRole`` so the gate never has to think about
    legacy values. ``compliance_reviewer`` collapses to ``viewer`` here.
    """

    ADMIN = "admin"
    UNDERWRITER = "underwriter"
    VIEWER = "viewer"


def effective_role(role: OperatorRole) -> EffectiveRole:
    """Map a stored OperatorRole to the role used by the permission gate.

    ``compliance_reviewer`` → ``viewer``. Everything else maps 1:1.
    """
    if role == OperatorRole.COMPLIANCE_REVIEWER:
        return EffectiveRole.VIEWER
    return EffectiveRole(role.value)


class Operator(BaseModel):
    """Row from the ``operators`` table.

    Email format is validated by a simple regex mirroring the DB
    CHECK constraint. We deliberately do NOT pull in
    ``email-validator`` (Pydantic's ``EmailStr`` dependency) because
    the DB column has the same CHECK constraint and the operator
    only ever populates this from Cloudflare Access — never from an
    untrusted input source.
    """

    id: UUID = Field(default_factory=uuid4)
    email: str = Field(pattern=_EMAIL_RE.pattern)
    display_name: str = Field(min_length=1)
    role: OperatorRole = OperatorRole.UNDERWRITER
    is_active: bool = True


#: The Cloudflare Access header that carries the authenticated
#: operator's email. Cloudflare's docs:
#:
#:   https://developers.cloudflare.com/cloudflare-one/identity/authorization-cookie/application-token/
#:
#: Header is set on every request that traverses an Access-protected
#: app. Local/dev requests don't have it; the resolver returns None
#: in that case so the caller's downstream logic can fall back to the
#: bearer-only auth path.
CF_ACCESS_EMAIL_HEADER: Final[str] = "cf-access-authenticated-user-email"


async def resolve_operator_email(
    cf_access_authenticated_user_email: Annotated[
        str | None, Header(alias=CF_ACCESS_EMAIL_HEADER)
    ] = None,
) -> str | None:
    """FastAPI dependency that returns the Cloudflare Access operator email.

    Returns ``None`` when the header is absent (local dev, internal
    healthcheck pings, tests). Callers that REQUIRE an authenticated
    operator (e.g. a Settings page that adds operators) compose a
    second dependency that 401s when this returns None.

    The header value is NOT trusted as a security boundary on its
    own — Cloudflare Access already verified the JWT upstream. We
    just propagate the identity into audit_log + role lookups.
    """
    if cf_access_authenticated_user_email is None:
        return None
    cleaned = cf_access_authenticated_user_email.strip().lower()
    if not cleaned or "@" not in cleaned:
        return None
    return cleaned


__all__ = [
    "CF_ACCESS_EMAIL_HEADER",
    "EffectiveRole",
    "Operator",
    "OperatorRole",
    "effective_role",
    "resolve_operator_email",
]
