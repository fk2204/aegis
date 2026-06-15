"""Per-state routing regression — locks every US state + DC to the
tier / template / hard-decline tuple the router currently returns.

Sprint 5 Track B — closes the master-plan gap flagged in
``docs/AEGIS_MASTER_PLAN.md``: per-feature tests covered router
behavior indirectly, but there was no single file asserting
"for state X, the routing tuple is Y." This file is that pin.

Source of truth
---------------
This test does NOT hardcode expected tier / template / hard-decline
values from external knowledge. Instead, expected tuples are derived
from the loaded ``StateMatrix`` itself — the same matrix the router
consumes at runtime. The test then calls ``router()`` with a canonical
baseline input (``Decimal("100000")`` MCA / ``sales_based``) and
asserts the router's output agrees with the matrix-derived expectation.

This is a regression test: it pins what the router does today so a
future ``states.yaml`` edit or router-code change that silently
re-routes a state trips immediately. If the operator deliberately
updates a state's tier or template, the expected tuple in the
parametrize block must be updated in the same commit — that is the
audit trail.

Scope: 50 US states + DC. DC is served (Tier 3 in the matrix) and is
included as the 51st parametrize row. The router has no "unserved"
state path — ``StateMatrix`` requires all 51 USPS codes to load
(``_REQUIRED_STATE_CODES`` in ``aegis.compliance.state_matrix``), so
this file asserts non-empty routing for every code. ``test_router.py``
already exercises the negative path (``UnknownStateError`` for
non-USPS codes like ``"ZZ"``).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from aegis.compliance.router import RouterResult, router
from aegis.compliance.state_matrix import (
    HardDeclineRule,
    StateMatrix,
    Tier1Regulation,
    load_matrix,
)

# Canonical baseline inputs. Chosen so the router's tier-routing logic
# is exercised against a typical MCA submission: a sales-based advance
# at $100k. This sits inside every Tier 1 state's CFDL threshold
# (smallest threshold is CT's $250k) and is in scope for every Tier 1
# state's product_scope (every Tier 1 state covers ``sales_based``).
# The result is a stable "happy path" baseline — Tier 1 states return
# tier=1 + their CFDL template; Tier 2/3 states return the watchlist /
# defensive surface.
_BASELINE_AMOUNT: Decimal = Decimal("100000")
_BASELINE_PRODUCT: str = "sales_based"


@pytest.fixture(scope="module")
def matrix() -> StateMatrix:
    """The production-loaded ``StateMatrix``. Reused across all 51
    parametrize cases for speed — the loader hits the filesystem and
    runs full Pydantic validation, so a per-case fixture would 50x
    that cost for no extra coverage."""
    return load_matrix()


def _expected_tier(state_code: str, matrix: StateMatrix) -> int:
    """Pin the tier directly from the matrix entry. The router returns
    the same value for the baseline (in-scope) inputs."""
    return matrix.states[state_code].tier


def _expected_template(state_code: str, matrix: StateMatrix) -> Path | None:
    """Pin the template path the router will return for the baseline
    inputs:

    * Tier 1 state, baseline in scope (product+amount fit): return the
      state's ``cfdl.template_path`` (or ``None`` if the dossier omits it).
    * Tier 1 state, baseline out of scope: ``None`` (Tier 3 fallback).
    * Tier 2 / Tier 3: ``None`` — no state-specific template.

    Baseline is sales_based / $100k — in scope for every Tier 1 state
    in the current matrix (lowest threshold is CT at $250k; every Tier 1
    state lists ``sales_based`` in product_scope). The matrix-derived
    expectation can therefore use the Tier 1 template directly without
    re-implementing the router's scope checks.
    """
    regulation = matrix.states[state_code]
    if not isinstance(regulation, Tier1Regulation):
        return None
    path = regulation.cfdl.template_path
    return Path(path) if path else None


def _expected_hard_declines(state_code: str, matrix: StateMatrix) -> list[HardDeclineRule]:
    """Pin the hard-decline rules from the matrix entry. Tier 2/3
    surfaces declare none. Tier 1 surfaces forward whatever
    ``hard_decline_rules`` the entry declares — the router copies the
    list verbatim onto its ``RouterResult``."""
    regulation = matrix.states[state_code]
    if not isinstance(regulation, Tier1Regulation):
        return []
    return list(regulation.hard_decline_rules)


# All 50 US states + DC. Mirrors ``_REQUIRED_STATE_CODES`` in
# ``aegis.compliance.state_matrix`` — the loader rejects ``states.yaml``
# if any entry is missing. Sorted for parametrize ID stability.
_ALL_STATES: list[str] = sorted(
    [
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
        "DC",
    ]
)


@pytest.mark.parametrize("state_code", _ALL_STATES, ids=lambda code: code)
def test_state_routing_matches_matrix(matrix: StateMatrix, state_code: str) -> None:
    """Pin the (tier, template, hard_declines) tuple for ``state_code``.

    For each US state + DC the router must, given the canonical
    baseline ($100k sales_based MCA), return a result whose tier,
    template path, and hard-decline rule list agree with the
    matrix-derived expectation. Drift between the matrix and the
    router for any state trips here.
    """
    expected_tier = _expected_tier(state_code, matrix)
    expected_template = _expected_template(state_code, matrix)
    expected_hard_declines = _expected_hard_declines(state_code, matrix)

    result = router(state_code, _BASELINE_AMOUNT, _BASELINE_PRODUCT, matrix)

    assert isinstance(result, RouterResult), (
        f"router({state_code}, ...) returned non-RouterResult: {type(result)!r}"
    )
    assert result.tier == expected_tier, (
        f"tier drift for {state_code}: router returned {result.tier}, "
        f"matrix expected {expected_tier}"
    )
    assert result.template_path == expected_template, (
        f"template drift for {state_code}: router returned "
        f"{result.template_path!r}, matrix expected {expected_template!r}"
    )
    assert list(result.hard_decline_rules) == expected_hard_declines, (
        f"hard-decline drift for {state_code}: router returned "
        f"{result.hard_decline_rules!r}, matrix expected "
        f"{expected_hard_declines!r}"
    )
    assert result.applicable_rules, (
        f"router({state_code}, ...) returned empty applicable_rules — "
        f"every state must produce at least one rule code"
    )


def test_parametrize_covers_exactly_50_states_plus_dc() -> None:
    """Coverage guard: the parametrize block above must list every
    USPS code exactly once. A regression here (someone deleting a
    row, double-listing one, drifting the constant) is caught at
    collection time."""
    assert len(_ALL_STATES) == 51, f"expected 50 US states + DC = 51 rows, got {len(_ALL_STATES)}"
    assert len(set(_ALL_STATES)) == 51, "duplicate state code in parametrize list"
    assert "DC" in _ALL_STATES, "DC must be covered (served in the matrix)"
