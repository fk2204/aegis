"""OperatorRepository Protocol + in-memory + Supabase impls.

Backs the role-based permission gate (commit 1 of the role/assignments/
notifications wave) and the assignment + notifications surfaces that
follow. The Cloudflare-Access email header is the identity input;
``get_or_create_operator`` is the upsert path used by the per-request
dependency.

Default-role policy: an operator the box has never seen before lands at
``OperatorRole.UNDERWRITER`` — the principle-of-least-surprise default
that mirrors migration 022's table default. ``admin`` is opt-in via the
operator-owner seed insert in `/etc/aegis/aegis.env`-side bootstrap.
"""

from __future__ import annotations

from typing import Any, Protocol, cast
from uuid import UUID

from aegis.db import get_supabase
from aegis.logger import get_logger
from aegis.ops.operators import Operator, OperatorRole

_log = get_logger(__name__)


class OperatorWriteError(RuntimeError):
    """Raised when an operators-row write could not be persisted."""


class OperatorRepository(Protocol):
    """Append-only-ish operator store. Updates are limited to role and
    is_active; email is the natural key and never changes for an
    existing row.
    """

    def get_or_create_by_email(
        self,
        *,
        email: str,
        default_display_name: str | None = None,
    ) -> Operator:
        """Look up the operator by lowercased email. Insert with the
        default role + supplied display_name when absent. Returns the
        live row in either case."""

    def get_by_id(self, operator_id: UUID) -> Operator | None:
        """Return the operator row by id, or None when absent."""

    def list_active(self) -> list[Operator]:
        """Return every ``is_active = TRUE`` operator, ordered by
        display_name. Powers the assignment-modal operator dropdown."""

    def list_admins(self) -> list[Operator]:
        """Return every active ``role = 'admin'`` operator. Powers the
        notifications fan-out ('every admin' recipient set)."""


# ---------------------------------------------------------------------------
# In-memory backend
# ---------------------------------------------------------------------------


class InMemoryOperatorRepository:
    """Dict-backed operator store. Tests + offline."""

    def __init__(self) -> None:
        self._by_email: dict[str, Operator] = {}
        self._by_id: dict[UUID, Operator] = {}

    def get_or_create_by_email(
        self,
        *,
        email: str,
        default_display_name: str | None = None,
    ) -> Operator:
        key = email.strip().lower()
        if not key or "@" not in key:
            raise OperatorWriteError(f"invalid operator email: {email!r}")
        existing = self._by_email.get(key)
        if existing is not None:
            return existing
        display = default_display_name or _display_from_email(key)
        row = Operator(email=key, display_name=display, role=OperatorRole.UNDERWRITER)
        self._by_email[key] = row
        self._by_id[row.id] = row
        return row

    def get_by_id(self, operator_id: UUID) -> Operator | None:
        return self._by_id.get(operator_id)

    def list_active(self) -> list[Operator]:
        return sorted(
            (row for row in self._by_email.values() if row.is_active),
            key=lambda r: r.display_name.lower(),
        )

    def list_admins(self) -> list[Operator]:
        return [row for row in self.list_active() if row.role == OperatorRole.ADMIN]

    # Test-only helper. Lets unit tests seed operators with a specific
    # role / id without going through get_or_create.
    def _seed(self, operator: Operator) -> None:
        key = operator.email.strip().lower()
        self._by_email[key] = operator
        self._by_id[operator.id] = operator


# ---------------------------------------------------------------------------
# Supabase backend
# ---------------------------------------------------------------------------


class SupabaseOperatorRepository:
    """Reads + writes the ``operators`` table (migration 022 / 076)."""

    def get_or_create_by_email(
        self,
        *,
        email: str,
        default_display_name: str | None = None,
    ) -> Operator:
        key = email.strip().lower()
        if not key or "@" not in key:
            raise OperatorWriteError(f"invalid operator email: {email!r}")
        rows = get_supabase().table("operators").select("*").eq("email", key).limit(1).execute()
        data = rows.data or []
        if data:
            return _row_to_operator(cast("dict[str, Any]", data[0]))
        display = default_display_name or _display_from_email(key)
        payload: dict[str, Any] = {
            "email": key,
            "display_name": display,
            "role": OperatorRole.UNDERWRITER.value,
            "is_active": True,
        }
        try:
            inserted = get_supabase().table("operators").insert(cast("Any", payload)).execute()
        except Exception as exc:
            # Race: a parallel request may have created the row between
            # the SELECT and the INSERT. Try once more to read, then
            # surface the original exception if the second read still
            # finds nothing.
            retry = (
                get_supabase().table("operators").select("*").eq("email", key).limit(1).execute()
            )
            retry_data = retry.data or []
            if retry_data:
                return _row_to_operator(cast("dict[str, Any]", retry_data[0]))
            raise OperatorWriteError(f"failed to insert operator row for {key!r}") from exc
        rows_inserted = inserted.data or []
        if not rows_inserted:
            raise OperatorWriteError(f"insert returned no rows for operator {key!r}")
        return _row_to_operator(cast("dict[str, Any]", rows_inserted[0]))

    def get_by_id(self, operator_id: UUID) -> Operator | None:
        rows = (
            get_supabase()
            .table("operators")
            .select("*")
            .eq("id", str(operator_id))
            .limit(1)
            .execute()
        )
        data = rows.data or []
        if not data:
            return None
        return _row_to_operator(cast("dict[str, Any]", data[0]))

    def list_active(self) -> list[Operator]:
        rows = (
            get_supabase()
            .table("operators")
            .select("*")
            .eq("is_active", True)
            .order("display_name")
            .execute()
        )
        data = rows.data or []
        return [_row_to_operator(cast("dict[str, Any]", r)) for r in data]

    def list_admins(self) -> list[Operator]:
        rows = (
            get_supabase()
            .table("operators")
            .select("*")
            .eq("is_active", True)
            .eq("role", OperatorRole.ADMIN.value)
            .order("display_name")
            .execute()
        )
        data = rows.data or []
        return [_row_to_operator(cast("dict[str, Any]", r)) for r in data]


def _row_to_operator(row: dict[str, Any]) -> Operator:
    """Map a Supabase row into the Operator Pydantic model."""
    return Operator(
        id=UUID(str(row["id"])),
        email=str(row["email"]),
        display_name=str(row["display_name"]),
        role=OperatorRole(str(row["role"])),
        is_active=bool(row.get("is_active", True)),
    )


def _display_from_email(email: str) -> str:
    """Derive a sane default display name from an email local-part.

    ``filip@commerafunding.com`` → ``Filip``.
    ``first.last@x.io`` → ``First Last``.
    """
    local = email.split("@", 1)[0]
    parts = [p for p in local.replace("_", ".").replace("-", ".").split(".") if p]
    if not parts:
        return email
    return " ".join(p[:1].upper() + p[1:].lower() for p in parts)


__all__ = [
    "InMemoryOperatorRepository",
    "OperatorRepository",
    "OperatorWriteError",
    "SupabaseOperatorRepository",
]
