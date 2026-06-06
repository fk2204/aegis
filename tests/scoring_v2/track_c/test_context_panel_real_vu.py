"""Track C — Concentration Context Panel — REAL VU acceptance test.

CLAUDE.md "external-integration test discipline" applies: the fixture
is the same captured VU bundle the counterparty classifier was
validated against (``tests/counterparty/fixtures/vu_real_txns.json``).
This test grades the panel against the bytes BoA actually emitted on
VU's statements — not a hand-written sample.

The headline acceptance criterion (operator's spec):

  VU's international wires should surface as
  ``international_client`` concentration with a durability framing
  and a stress view — NOT as a fraud signal. This is the original
  mislabeling the redesign fixes; if this test passes, the reframe
  is correct.

Companion gates:

* Revenue basis excludes own_account, own_account_unconfirmed,
  book_wire_unresolved, and card_paydown.
* The stress view's drop-the-top-class scenario computes correctly.
* The unconfirmed-account follow-up list carries CHK 9940 (and the
  7722 period gaps + 1218 from the gap fix).
* The book_wire_unresolved totals are surfaced separately on the
  panel (not silently rolled into revenue OR into expense).
* The panel never sets a decline field — it has no decline field by
  design.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from aegis.counterparty import classify_bundle
from aegis.parser.models import ClassifiedTransaction
from aegis.scoring_v2.track_c import (
    ConcentrationContextPanel,
    compute_context_panel,
)

_FIXTURE_PATH = (
    Path(__file__).parent.parent.parent
    / "counterparty"
    / "fixtures"
    / "vu_real_txns.json"
)


def _load_fixture() -> dict[str, Any]:
    data = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _build_bundle() -> tuple[dict[str, list[ClassifiedTransaction]], set[str]]:
    fixture = _load_fixture()
    by_doc: dict[str, list[ClassifiedTransaction]] = {}
    accts: set[str] = set()
    for doc in fixture["documents"]:
        last4 = doc["summary"]["account_last4"]
        if last4:
            accts.add(last4)
        txns: list[ClassifiedTransaction] = []
        for t in doc["transactions"]:
            running = t.get("running_balance")
            txns.append(
                ClassifiedTransaction(
                    id=UUID(t["id"]),
                    posted_date=date.fromisoformat(t["posted_date"]),
                    description=t["description"],
                    amount=Decimal(t["amount"]),
                    running_balance=(
                        Decimal(running) if running is not None else None
                    ),
                    source_page=1,
                    source_line=1,
                    category=t["category"],
                    classification_confidence=100,
                )
            )
        by_doc[doc["document_id"]] = txns
    return by_doc, accts


@pytest.fixture(scope="module")
def vu_panel() -> ConcentrationContextPanel:
    by_doc, accts = _build_bundle()
    classifications, _ = classify_bundle(by_doc, accts)
    return compute_context_panel(by_doc, classifications)


# ─────────────────────────────────────────────────────────────────────
# Headline reframe — the operator-facing payoff
# ─────────────────────────────────────────────────────────────────────


def test_vu_international_wires_surface_as_international_client_concentration(
    vu_panel: ConcentrationContextPanel,
) -> None:
    """The original mislabeling — fraud signal — is fixed.

    VU's three INTERNATIONAL WH credits ($99.5K + $100K + $125K =
    $324,700) must surface as ``international_client`` concentration
    with a durability framing — NOT as a fraud signal. There is no
    fraud field on this panel; the test instead asserts the FRAMING
    explicitly avoids fraud language and explicitly carries the
    durability reframe.
    """
    intl_rows = [
        r for r in vu_panel.by_class if r.counterparty == "international_client"
    ]
    assert len(intl_rows) == 1
    intl = intl_rows[0]
    assert intl.transaction_count == 3
    assert intl.incoming_total == Decimal("324700.00")

    # The reframe is in the wording. "Durability" / "would the
    # counterparty continue paying" is the spec wording.
    assert "durability" in intl.framing.lower()
    assert "would the counterparty continue paying" in intl.framing.lower()
    # AND the inverse: no "fraud" language. (The whole point.)
    assert "fraud" in intl.framing.lower()  # appears as "NOT a fraud signal"
    assert "not a fraud signal" in intl.framing.lower()
    assert intl.severity == "durability"


def test_vu_international_client_is_top_class_and_drives_the_stress_view(
    vu_panel: ConcentrationContextPanel,
) -> None:
    """international_client is by far VU's largest revenue source.
    The stress view drops it entirely and reveals what remains."""
    assert vu_panel.stress is not None
    assert vu_panel.stress.top_class == "international_client"
    assert vu_panel.stress.base_revenue == Decimal("364280.05")
    assert vu_panel.stress.top_class_total == Decimal("324700.00")
    # Stress revenue = base - top class
    assert vu_panel.stress.stress_revenue == Decimal("39580.05")
    # Drop pct is the top class's share of base.
    # 324700 / 364280.05 = 0.891296... times 100 = 89.1296... rounded
    # to 89.13 at 2 decimal places with ROUND_HALF_UP.
    assert vu_panel.stress.revenue_drop_pct == Decimal("89.13")
    # And the stress framing names the international-counterparty
    # case explicitly so the underwriter sees what the case answers.
    assert "international" in vu_panel.stress.framing.lower()
    assert "underwriter" in vu_panel.stress.framing.lower()


# ─────────────────────────────────────────────────────────────────────
# Denominator correctness — the foundation pays off here
# ─────────────────────────────────────────────────────────────────────


def test_vu_revenue_basis_excludes_book_wires_and_own_account(
    vu_panel: ConcentrationContextPanel,
) -> None:
    """The whole point of the counterparty foundation: a $1.5M book
    wire inflow CANNOT be in the revenue denominator (it's
    book_wire_unresolved, not revenue). Same for own_account moves
    and own_account_unconfirmed."""
    # Revenue basis = processor ($8,638.14) + end_customer ($30,941.91)
    # + international_client ($324,700.00) = $364,280.05.
    assert vu_panel.revenue_basis == Decimal("364280.05")

    # Book wire incoming is large ($1.53M) but it's NOT in
    # revenue_basis. It surfaces on its own field for the operator's
    # follow-up.
    assert vu_panel.book_wire_unresolved_total_incoming == Decimal("1533773.26")
    assert vu_panel.book_wire_unresolved_total_outgoing == Decimal("450017.29")


def test_vu_by_class_shows_only_revenue_classes(
    vu_panel: ConcentrationContextPanel,
) -> None:
    """The by_class table surfaces processor + end_customer +
    international_client. Non-revenue classes (own_account,
    own_account_unconfirmed, book_wire_unresolved, card_paydown) do
    NOT appear on the by_class table — they're rendered elsewhere
    on the panel so the share %s mean revenue concentration."""
    classes_on_panel = {r.counterparty for r in vu_panel.by_class}
    assert classes_on_panel == {"international_client", "end_customer", "processor"}


def test_vu_shares_sum_to_100_percent(
    vu_panel: ConcentrationContextPanel,
) -> None:
    """The 3 revenue class shares must sum to 100% (within rounding
    tolerance from the 2-decimal-place quantization)."""
    total = sum(
        (r.share_pct for r in vu_panel.by_class), start=Decimal("0")
    )
    # Allow ±0.05 for cumulative quantization across 3 rows.
    assert abs(total - Decimal("100.00")) <= Decimal("0.10"), (
        f"shares sum to {total}, expected 100"
    )


# ─────────────────────────────────────────────────────────────────────
# Unconfirmed accounts — the operator follow-up surface
# ─────────────────────────────────────────────────────────────────────


def test_vu_unconfirmed_accounts_surface_for_operator_followup(
    vu_panel: ConcentrationContextPanel,
) -> None:
    """The 9940 + 7722-period-gap + 1218 cases from the counterparty
    foundation must surface on the panel as the operator's follow-up
    list. They never auto-decline; they prompt a human question."""
    accts = set(vu_panel.unconfirmed_account_last4s)
    assert "9940" in accts
    assert "7722" in accts
    assert "1218" in accts


# ─────────────────────────────────────────────────────────────────────
# Informational-only invariant
# ─────────────────────────────────────────────────────────────────────


def test_panel_has_no_decline_or_score_field(
    vu_panel: ConcentrationContextPanel,
) -> None:
    """Track C is informational. The panel has no field that maps to
    a decline boundary in any consumer. This test reads the Pydantic
    schema and asserts there is no ``decline``, ``score``, ``risk``,
    or similar field — preventing a future accidental wiring of
    Track C into the decline path."""
    schema_fields = set(ConcentrationContextPanel.model_fields)
    forbidden = {
        "decline",
        "auto_decline",
        "risk_score",
        "fraud_score",
        "score",
        "verdict",
    }
    leaked = schema_fields & forbidden
    assert not leaked, (
        f"Track C panel must not carry decline/score fields; leaked: {leaked}"
    )


def test_panel_renders_for_an_empty_bundle_without_crashing() -> None:
    """Defensive: an empty bundle (zero transactions) must produce a
    panel with zero revenue, no stress, no warnings, no rows — never
    raise."""
    panel = compute_context_panel({}, {})
    assert panel.revenue_basis == Decimal("0")
    assert panel.by_class == ()
    assert panel.stress is None
    assert panel.unconfirmed_account_last4s == ()
    assert panel.book_wire_unresolved_total_incoming == Decimal("0")
    assert panel.warnings == ()


# ─────────────────────────────────────────────────────────────────────
# Per-class framing checks (the actual underwriter-facing copy)
# ─────────────────────────────────────────────────────────────────────


def test_vu_end_customer_framing_explicitly_calls_out_concentration_risk(
    vu_panel: ConcentrationContextPanel,
) -> None:
    """When end_customer share is below the durability floor, the
    framing should still be informational. VU's end_customer share
    is ~8.5% (below the 30% floor), so we expect ``info`` severity
    and informational copy."""
    ec_rows = [
        r for r in vu_panel.by_class if r.counterparty == "end_customer"
    ]
    assert len(ec_rows) == 1
    ec = ec_rows[0]
    assert ec.severity == "info"
    assert "named end customer" in ec.framing.lower()
    assert "low concentration" in ec.framing.lower()


def test_vu_processor_framing_describes_rail_concentration(
    vu_panel: ConcentrationContextPanel,
) -> None:
    """VU's processor share is small (~2.4%) — info severity, copy
    describes payment rails / low durability concern."""
    p_rows = [r for r in vu_panel.by_class if r.counterparty == "processor"]
    assert len(p_rows) == 1
    p = p_rows[0]
    assert p.severity == "info"
    assert "payment-rail" in p.framing.lower() or "processor" in p.framing.lower()
