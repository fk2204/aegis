"""Funder persistence layer.

`FunderRepository` is the Protocol the rest of the system depends on.
`InMemoryFunderRepository` is a reference implementation backed by a
dict — sufficient for tests and the parser-level integration before
Phase 5 wires Supabase.

Phase 5 adds `SupabaseFunderRepository` (same Protocol, talks to
`funders` Postgres table). At that point matchers + the dashboard will
swap implementations without code changes.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from aegis.funders.models import FunderRow


class FunderNotFoundError(KeyError):
    """Raised when a funder id has no matching row."""


class FunderRepository(Protocol):
    """CRUD interface for funders. Implementations enforce uniqueness on `name`."""

    def get(self, funder_id: UUID) -> FunderRow:
        """Return the funder with this id. Raises `FunderNotFoundError` otherwise."""

    def list_active(self) -> list[FunderRow]:
        """Return all active funders, ordered by name."""

    def upsert(self, funder: FunderRow) -> FunderRow:
        """Insert or replace by id; uniqueness on name is enforced."""

    def delete(self, funder_id: UUID) -> None:
        """Remove the funder. No-op if not present."""


class InMemoryFunderRepository:
    """Dict-backed implementation. Phase 5 swaps for Supabase-backed."""

    def __init__(self) -> None:
        self._by_id: dict[UUID, FunderRow] = {}

    def get(self, funder_id: UUID) -> FunderRow:
        try:
            return self._by_id[funder_id]
        except KeyError as exc:
            raise FunderNotFoundError(str(funder_id)) from exc

    def list_active(self) -> list[FunderRow]:
        return sorted(
            (f for f in self._by_id.values() if f.active),
            key=lambda f: f.name.lower(),
        )

    def upsert(self, funder: FunderRow) -> FunderRow:
        # Enforce name uniqueness across different ids.
        for existing in self._by_id.values():
            if existing.id != funder.id and existing.name.lower() == funder.name.lower():
                raise ValueError(
                    f"funder name conflict: '{funder.name}' already exists "
                    f"under id={existing.id}"
                )
        self._by_id[funder.id] = funder
        return funder

    def delete(self, funder_id: UUID) -> None:
        self._by_id.pop(funder_id, None)


__all__ = ["FunderNotFoundError", "FunderRepository", "InMemoryFunderRepository"]
