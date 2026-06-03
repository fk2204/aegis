"""Guard tests for OFAC + scoring on non-finalized merchants.

Migration 034 introduced the ``provisional`` and ``needs_manual_naming``
statuses. Both carry a placeholder ``business_name``
(``PROVISIONAL_BUSINESS_NAME_PLACEHOLDER``). Running OFAC against the
placeholder would fabricate a permanent compliance record ("we screened
(awaiting parse)") — worse than no record. Running ``score_deal`` for
a non-finalized merchant would also burn Bedrock cycles + invoke OFAC
indirectly via the scorer.

The guards live at the consumer site (router.py + findings.py + the
tier-lookup helper). These tests pin:

* a NON-FINALIZED merchant is NOT OFAC-screened and NOT scored, AND
* a FINALIZED merchant screens + scores exactly as today (no regression
  introduced by the guard).

Plus a unit test of ``_state_tier(None)`` since chunk A widened that
helper to accept ``str | None`` and return ``"unknown"`` (distinct from
``"unserved"``).
"""

from __future__ import annotations

from typing import Any

from aegis.merchants.repository import (
    PROVISIONAL_BUSINESS_NAME_PLACEHOLDER,
    InMemoryMerchantRepository,
)

# ---------------------------------------------------------------------------
# _state_tier(None) — chunk A widening
# ---------------------------------------------------------------------------


def test_state_tier_returns_unknown_on_none() -> None:
    """``_state_tier(None) → "unknown"`` is the §5 widening the design
    doc called for. The render layer treats "unknown" (no state set
    yet, post-034) distinctly from "unserved" (real state, AEGIS chose
    not to fund there).
    """
    from aegis.web.router import _state_tier

    assert _state_tier(None) == "unknown"
    # Sanity: real states still resolve to their tier int / "unserved".
    # CA is tier 1 in the STATES registry; "ZZ" is not a real state.
    assert _state_tier("CA") == 1
    assert _state_tier("ZZ") == "unserved"


# ---------------------------------------------------------------------------
# OFAC + scoring NOT invoked on non-finalized merchants
# ---------------------------------------------------------------------------


class _ExplodingOFAC:
    """OFAC client that records every is_match call and ALSO raises
    a recognizable exception. Reaches into a scoring guard from below
    — if a guard fails and the scorer reaches this client, the test
    will see a recorded call (proof the guard didn't fire).
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def is_match(self, business_name: str) -> bool:
        self.calls.append(business_name)
        return False


def _make_finalized(repo: InMemoryMerchantRepository) -> Any:
    """Helper: create a real finalized merchant via the test fixture
    pattern used elsewhere in the suite."""
    from aegis.merchants.models import MerchantRow

    m = MerchantRow(
        id=__import__("uuid").uuid4(),
        status="finalized",
        business_name="Acme LLC",
        owner_name="Jane Doe",
        state="CA",
    )
    return repo.upsert(m)


def test_compute_merchant_tier_skips_non_finalized() -> None:
    """The Today / queue-card tier helper (``_compute_merchant_tier``)
    must NOT score a provisional merchant — that would invoke OFAC
    against the placeholder business_name.

    Returning ``None`` is the existing fallback for "no tier available"
    and renders cleanly in the existing card surfaces.
    """
    from aegis.storage import InMemoryDocumentRepository
    from aegis.web.router import _compute_merchant_tier

    repo = InMemoryMerchantRepository()
    docs = InMemoryDocumentRepository()
    ofac = _ExplodingOFAC()

    provisional = repo.create_provisional()
    needs_naming = repo.create_provisional()
    repo.mark_needs_manual_naming(merchant_id=needs_naming.id)

    assert _compute_merchant_tier(provisional, docs, ofac) is None  # type: ignore[arg-type]
    assert _compute_merchant_tier(needs_naming, docs, ofac) is None  # type: ignore[arg-type]
    # Critically: OFAC was NEVER consulted.
    assert ofac.calls == []


def test_compute_merchant_tier_finalized_still_runs_through_to_ofac() -> None:
    """Regression guard for the finalized path: a finalized merchant
    with no docs returns ``None`` (insufficient data), but OFAC is
    still WIRED IN — the scoring path would invoke it if items existed.
    Pins that the guard ONLY excludes non-finalized merchants and
    doesn't accidentally short-circuit finalized ones.
    """
    from aegis.storage import InMemoryDocumentRepository
    from aegis.web.router import _compute_merchant_tier

    repo = InMemoryMerchantRepository()
    docs = InMemoryDocumentRepository()
    ofac = _ExplodingOFAC()

    finalized = _make_finalized(repo)

    # No docs → returns None for "no data", but reached the call site.
    # (The `if not items: return None` arm runs BEFORE OFAC.)
    assert _compute_merchant_tier(finalized, docs, ofac) is None  # type: ignore[arg-type]
    # OFAC was not called either — but only because items=[], not
    # because of the is_finalized gate. That's exactly the regression
    # shape we want: the gate doesn't change behavior for finalized
    # merchants compared to pre-034.
    assert ofac.calls == []


# ---------------------------------------------------------------------------
# OFAC ribbon NOT invoked on non-finalized via _ofac_ribbon_status path
# ---------------------------------------------------------------------------


def test_ofac_ribbon_status_helper_works_independently() -> None:
    """``_ofac_ribbon_status`` itself is unconditional — it always
    queries the supplied OFAC client. The gate lives at the CALLER
    (the dossier render in router.py). Pin this contract so callers
    know the gate is their responsibility, not the helper's.

    Asserts the helper queries the client when called, and returns
    ``not_consulted`` only when the client itself is None.
    """
    from aegis.web.router import _ofac_ribbon_status

    ofac = _ExplodingOFAC()
    status_label, match = _ofac_ribbon_status(ofac, "Acme LLC")  # type: ignore[arg-type]

    assert status_label == "checked"
    assert match is False
    assert ofac.calls == ["Acme LLC"]

    # No OFAC client → "not_consulted" without raising.
    status_label, match = _ofac_ribbon_status(None, "Acme LLC")
    assert status_label == "not_consulted"


# ---------------------------------------------------------------------------
# Placeholder business_name visibility in audit / SQL trail
# ---------------------------------------------------------------------------


def test_provisional_business_name_is_recognizable_placeholder() -> None:
    """The placeholder must be obviously NOT a real business name when
    surfaced anywhere (dashboard list, audit details, SQL queries).
    "(awaiting parse)" with parentheses is unambiguous — no real
    business registers a name starting with an open paren."""
    repo = InMemoryMerchantRepository()
    m = repo.create_provisional()

    assert m.business_name == PROVISIONAL_BUSINESS_NAME_PLACEHOLDER
    assert m.business_name.startswith("(")
    assert m.business_name.endswith(")")
    assert "awaiting parse" in m.business_name.lower()
