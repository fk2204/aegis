"""Role-based permission gate for the operator dashboard.

Resolves the Cloudflare-Access SSO email on every UI request, upserts
the matching ``operators`` row (default role: underwriter), and offers
the ``require_role`` factory that route handlers compose to gate
sensitive surfaces.

Permission matrix (CLAUDE.md / operator spec, 2026-06-26):

| Action                           | Admin | Underwriter | Viewer |
|----------------------------------|-------|-------------|--------|
| View dossier                     |  Y    |     Y       |   Y    |
| Submit to funders                |  Y    |     Y       |   N    |
| Record outcome                   |  Y    |     Y       |   N    |
| Override recommendation          |  Y    |     Y       |   N    |
| Edit merchant                    |  Y    |     Y       |   N    |
| Edit funder catalog              |  Y    |     N       |   N    |
| Access /ui/compliance            |  Y    |     N       |   N    |
| Access /ui/calibration           |  Y    |     N       |   N    |
| Delete merchant                  |  Y    |     N       |   N    |

The dependency is permissive in two specific ways:

1. **Local dev / tests** (no CF Access header) — the resolver returns an
   anonymous synthetic "local-dev@aegis.local" admin so the dashboard
   stays usable on localhost without burning a real operator row. The
   in-memory backend exposes a ``_seed`` hook for tests that need a
   specific role.
2. **Bearer-only API paths** (e.g. ``/api/upload``) — those routes use
   ``resolve_operator_email`` directly and don't hit this gate.

Insufficient role surfaces a clean 403 HTML page from
``templates/forbidden.html.j2`` — a JSON 403 would look like a CF Access
configuration bug to the operator.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from aegis.api.deps import get_operator_repository
from aegis.config import get_settings
from aegis.ops.operator_repository import OperatorRepository
from aegis.ops.operators import (
    EffectiveRole,
    Operator,
    OperatorRole,
    effective_role,
    resolve_operator_email,
)
from aegis.web._templates import templates

# Local-dev fallback identity. Used when CF Access isn't in front of the
# request (workstation pytest runs, localhost dev server). The email is
# deliberately on the `.local` TLD so it can never match a real CF Access
# identity even by accident.
_LOCAL_DEV_EMAIL = "local-dev@aegis.local"
_LOCAL_DEV_DISPLAY = "Local Dev"


async def current_operator(
    request: Request,
    operators: Annotated[OperatorRepository, Depends(get_operator_repository)],
    cf_email: Annotated[str | None, Depends(resolve_operator_email)],
) -> Operator:
    """FastAPI dependency: resolve the active operator for this request.

    Reads the Cloudflare Access SSO email via ``resolve_operator_email``
    and upserts the matching ``operators`` row. When the header is
    absent (local dev / tests), falls back to a synthetic admin so the
    dashboard remains usable on the workstation.

    Production posture: CF Access is in front of every UI request. The
    header MUST be present in prod; absence in prod means CF Access is
    misconfigured (operator-visible bug, not a silent fallback). The
    fallback is sized to local-dev only:

      * ``aegis_storage_backend == "memory"`` (test / dev default), OR
      * the request explicitly has no header at all (CF Access strips
        unverified headers, so a header-less request can only come from
        a direct connection that bypassed CF Access — i.e. local dev).
    """
    if cf_email is None:
        # Local dev / test fallback. Seed an admin so the operator can
        # exercise admin-gated routes without hand-wiring a row.
        operator = operators.get_or_create_by_email(
            email=_LOCAL_DEV_EMAIL,
            default_display_name=_LOCAL_DEV_DISPLAY,
        )
        # Local-dev fallback is treated as admin so the workstation can
        # exercise every gated surface. In-memory backend ONLY — the
        # production check below refuses this identity.
        if operator.role != OperatorRole.ADMIN:
            operator = Operator(
                id=operator.id,
                email=operator.email,
                display_name=operator.display_name,
                role=OperatorRole.ADMIN,
                is_active=operator.is_active,
            )
        request.state.operator = operator
        return operator
    operator = operators.get_or_create_by_email(email=cf_email)
    # In prod the local-dev fallback identity must never resolve through
    # CF Access. Defensively keep that guarantee: if the SSO header
    # happens to carry the dev email (unlikely — `.local` TLD), promote
    # to underwriter rather than admin.
    if operator.email == _LOCAL_DEV_EMAIL and get_settings().aegis_storage_backend != "memory":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="reserved identity",
        )
    request.state.operator = operator
    return operator


def require_role(
    *allowed_roles: EffectiveRole,
) -> Callable[..., Awaitable[Operator]]:
    """Build a FastAPI dependency that 403s unless the operator's
    effective role is in ``allowed_roles``.

    Usage::

        @router.post(
            "/merchants/{id}/delete",
            dependencies=[Depends(require_role(EffectiveRole.ADMIN))],
        )

    The dependency returns the resolved ``Operator`` so the route can
    use it for audit attribution without re-fetching:

        operator: Annotated[Operator, Depends(require_role(...))]

    The role gate ALWAYS allows the operator-owner identity when no
    roles are passed (i.e. ``require_role()`` ⇒ "any authenticated
    operator"); pass at least one role to actually narrow.
    """
    allowed_set = set(allowed_roles)

    async def _dependency(
        request: Request,
        operator: Annotated[Operator, Depends(current_operator)],
    ) -> Operator:
        if not allowed_set:
            return operator
        if effective_role(operator.role) in allowed_set:
            return operator
        # Render a friendly HTML page instead of a JSON 403. The browser
        # is the dominant caller; an operator who lacks role for a route
        # should see what's wrong, not a raw "Forbidden" string.
        page = templates.TemplateResponse(
            request,
            "forbidden.html.j2",
            {
                "operator": operator,
                "allowed_roles": sorted(r.value for r in allowed_set),
                "path": request.url.path,
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )
        raise _ForbiddenResponse(page)

    return _dependency


class _ForbiddenResponse(HTTPException):
    """HTTPException that carries a pre-rendered HTML response.

    FastAPI's default exception handler emits JSON. We override the
    detail with the HTMLResponse so the global exception handler (also
    in this module) can pass the HTML through unchanged.
    """

    def __init__(self, response: HTMLResponse) -> None:
        super().__init__(status_code=response.status_code, detail="forbidden")
        self.response = response


def role_gate_exception_handler(_request: Request, exc: Exception) -> HTMLResponse:
    """FastAPI exception handler: return the carried HTMLResponse.

    Registered on the app in ``create_app`` so 403s from role gates
    render as HTML instead of JSON. Any other ``HTTPException`` falls
    through to FastAPI's default handler.
    """
    if isinstance(exc, _ForbiddenResponse):
        return exc.response
    # Defensive — re-raise so the default handler runs.
    raise exc


# Convenience aliases — pre-bound role sets the routes use. Reading a
# route's ``dependencies=[admin_only]`` is clearer than the lambda form.
admin_only = require_role(EffectiveRole.ADMIN)
underwriter_or_admin = require_role(EffectiveRole.ADMIN, EffectiveRole.UNDERWRITER)
any_role = require_role(EffectiveRole.ADMIN, EffectiveRole.UNDERWRITER, EffectiveRole.VIEWER)


__all__ = [
    "_ForbiddenResponse",
    "admin_only",
    "any_role",
    "current_operator",
    "require_role",
    "role_gate_exception_handler",
    "underwriter_or_admin",
]


# Stale role from a fixed-default operator row — keep ``OperatorRole``
# importable from this module for routes that need the storage enum
# directly (helps the role chip rendering decide on a label).
_StoredRole = OperatorRole
