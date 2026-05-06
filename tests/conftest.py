"""Test fixtures.

Sets the environment so the residency boot guard passes inside tests, and
clears the settings cache so each module-import sees the test env.
"""

from __future__ import annotations

import os

# Set BEFORE any aegis import so module-level get_settings() calls succeed.
os.environ.setdefault("AEGIS_DATA_RESIDENCY_CONFIRMED", "true")
os.environ.setdefault("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
os.environ.setdefault("API_BEARER_TOKEN", "test-token-not-real")

from aegis.config import get_settings

# Clear cache so the test env is honored regardless of prior imports.
get_settings.cache_clear()
