"""Supabase client wrapper.

Lazy: the client is built on first use, never at import. This keeps tests
that don't touch the DB from needing Supabase credentials.

Usage:
    from aegis.db import get_supabase
    rows = get_supabase().table("documents").select("*").execute()

The client is process-wide (single Supabase HTTPS session). Reset with
``reset_supabase()`` in tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aegis.config import get_settings

if TYPE_CHECKING:
    from supabase import Client

_client: Client | None = None


class SupabaseConfigError(RuntimeError):
    """Raised when the DB is requested but settings are not populated."""


def get_supabase() -> Client:
    """Return the process-wide Supabase client; build it on first call.

    Raises:
        SupabaseConfigError: when SUPABASE_URL or the service key is unset.
    """
    global _client
    if _client is not None:
        return _client

    settings = get_settings()
    if not settings.supabase_url or settings.supabase_service_key is None:
        raise SupabaseConfigError(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set to use the database"
        )

    from supabase import create_client

    _client = create_client(
        settings.supabase_url,
        settings.supabase_service_key.get_secret_value(),
    )
    return _client


def reset_supabase() -> None:
    """Drop the cached client. For tests + key rotation."""
    global _client
    _client = None


__all__ = ["SupabaseConfigError", "get_supabase", "reset_supabase"]
