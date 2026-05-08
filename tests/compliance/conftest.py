"""Compliance test fixtures.

`reset_states_table` snapshots `STATES` at session start and restores it
between tests so module-level mutations (a test promoting CA to Tier 1)
don't leak into the next test.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from aegis.compliance import states as states_module


@pytest.fixture(autouse=True)
def restore_states_table() -> Iterator[None]:
    """Save + restore STATES so per-test mutations don't bleed across tests."""
    snapshot = dict(states_module.STATES)
    try:
        yield
    finally:
        states_module.STATES.clear()
        states_module.STATES.update(snapshot)
