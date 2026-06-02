"""Test fixtures.

Sets the environment so the residency boot guard passes inside tests, and
clears the settings cache so each module-import sees the test env.

``API_BEARER_TOKEN`` and ``AEGIS_STORAGE_BACKEND`` are set **unconditionally**
(not via ``setdefault``) because tests assume specific values for both. If
the test shell happens to have ``/etc/aegis/aegis.env`` sourced — i.e. prod
values present — ``setdefault`` becomes a no-op and the test client sends
the wrong bearer or boots against Supabase. Force the test values here.

``AEGIS_DATA_RESIDENCY_CONFIRMED`` and ``BEDROCK_MODEL_ID`` are kept as
``setdefault``: the prod values match the test values, and respecting
operator overrides is harmless.
"""

from __future__ import annotations

import os

# Set BEFORE any aegis import so module-level get_settings() calls succeed.
os.environ.setdefault("AEGIS_DATA_RESIDENCY_CONFIRMED", "true")
os.environ.setdefault("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")

# Force-set: tests use a known fake token + the in-memory backend, regardless
# of what's in the shell. Lets `pytest` work in a shell that has prod env
# sourced (e.g. on the prod box for smoke-testing) without auth failures.
os.environ["API_BEARER_TOKEN"] = "test-token-not-real"  # noqa: S105 — test stub
os.environ["AEGIS_STORAGE_BACKEND"] = "memory"

# Force-set: PDF encryption keys for chunk-B worker tests. Deterministic
# all-zeros 32-byte key (base64-encoded). Safe because the test data is
# ephemeral and never persisted, reproducible across CI runs, AND forces
# the test value even when /etc/aegis/aegis.env is sourced on the box —
# pytest must not accidentally seal test fixtures with the real prod key.
import base64 as _b64

os.environ["PDF_ENCRYPTION_KEYS_CURRENT"] = "1"
os.environ["PDF_ENCRYPTION_KEY_V1"] = _b64.b64encode(bytes(32)).decode("ascii")

from aegis.config import get_settings

# Clear cache so the test env is honored regardless of prior imports.
get_settings.cache_clear()
