"""Quarterly anti-drift test: render disclosure for every Tier 1 state.

Master plan §13 task 4. The operator runs this as part of each
quarterly compliance review; it surfaces silent template-rendering
regressions across the Tier 1 set.

This test is **deliberately skipped** until master plan Phase 5 lands
the disclosure renderer (``aegis.compliance.render.render_disclosure``).
The test FILE exists from Phase 3 forward so the operator sees the gap
in the test report instead of forgetting to wire it up later.

Once Phase 5 ships:
  - Remove the module-level ``pytest.skip``.
  - Replace the synthetic-input stub with the real canonical inputs.
  - The test will assert that ``render_disclosure(state, inputs)``
    matches a frozen snapshot under ``tests/snapshots/quarterly/{ST}.html``.
"""

from __future__ import annotations

import pytest

# All 11 Tier 1 states from master plan §8.1.
TIER1_STATES = ("CA", "NY", "UT", "VA", "FL", "GA", "CT", "KS", "MO", "LA", "TX")

# Master plan Phase 5 builds aegis.compliance.render. Until then, this
# test cannot do anything meaningful. The skip exists from Phase 3 so
# the operator sees the gap during compliance reviews; remove it as part
# of the Phase 5 PR.
pytestmark = pytest.mark.skip(
    reason=(
        "disclosure renderer not yet built — master plan Phase 5. "
        "Remove this skip and implement the snapshot assertion when "
        "aegis.compliance.render.render_disclosure() lands."
    )
)


@pytest.mark.parametrize("state", TIER1_STATES)
def test_quarterly_disclosure_render(state: str) -> None:
    """Render the prescribed disclosure for ``state`` and snapshot it."""
    # Implementation note for the Phase 5 PR:
    #
    #   from aegis.compliance.render import render_disclosure
    #   from aegis.compliance.snapshot_inputs import canonical_inputs_for
    #
    #   inputs = canonical_inputs_for(state)
    #   html = render_disclosure(state, inputs)
    #   snapshot.assert_match(html, f"quarterly/{state}.html")
    raise NotImplementedError(
        "This body activates when master plan Phase 5 ships the disclosure "
        "renderer; until then, pytestmark.skip above keeps the test green."
    )
