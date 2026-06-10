"""Unit tests for ``scripts/cohort_backtest.py``.

Covers:

* ``_compute_default_rates`` on a synthetic 9-merchant fixture spanning
  every outcome bucket — asserts hand-computed Decimal rates per tier.
* Decimal precision: 1-of-3 yields ``Decimal("0.3333")``, never
  ``0.3333333333333333`` (float drift would silently round the report).
* Empty-corpus path: zero merchants in -> all five tiers present with
  ``default_rate=None``, ``n_total=0``.
* ``_classify_opportunity_status`` mapping table (defaulted / paid_off /
  renewed / current / not_funded / unknown).
* ``_resolve_close_outcome`` worst-case fold across multiple opportunities.
* ``run()`` dependency-injection seam — uses in-memory fakes; never
  touches the real Close client or psycopg.

NO real Close API calls. NO real DB calls. All fakes.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import cohort_backtest as cb  # noqa: E402

_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "cohort_synthetic.json"


# ---------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------


@dataclass
class _FakeDataSource:
    """In-memory ``CohortDataSource`` for ``run()`` tests."""

    rows: list[cb.CohortInputRow]

    def fetch_mature_cohort(self, *, min_age_days: int) -> list[cb.CohortInputRow]:
        # min_age_days is ignored by the fake; the rows are pre-curated
        # to be mature. The real source filters by intake_date in SQL.
        _ = min_age_days
        return list(self.rows)


@dataclass
class _FakeCloseClient:
    """Maps ``lead_id -> opportunities_payload`` so ``_resolve_close_outcome``
    can be exercised without a live Close client."""

    leads: dict[str, dict[str, Any]]

    def get_lead(self, lead_id: str) -> dict[str, Any]:
        if lead_id not in self.leads:
            # Real CloseClient raises CloseError on 404; the script
            # catches BaseException and returns "unknown". Mirror that
            # by raising a stdlib error here.
            raise KeyError(lead_id)
        return self.leads[lead_id]


def _load_fixture() -> list[cb.CohortRow]:
    raw = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    rows: list[cb.CohortRow] = []
    for entry in raw["rows"]:
        rows.append(
            cb.CohortRow(
                merchant_id=UUID(entry["merchant_id"]),
                tier=entry["tier"],
                outcome=entry["outcome"],
                score=entry["score"],
                intake_date=date.fromisoformat(entry["intake_date"]),
            )
        )
    return rows


# ---------------------------------------------------------------------
# _compute_default_rates — fixture-driven assertions
# ---------------------------------------------------------------------


def test_compute_default_rates_matches_hand_computed_fixture() -> None:
    """Tier A: 3 funded (1 paid_off, 1 current, 1 defaulted) -> 1/3 = 0.3333.
    Tier B: 2 funded (1 defaulted, 1 paid_off) -> 1/2 = 0.5000.
    Tier C: 1 funded (1 renewed) + 1 unknown -> 0/1 = 0.0000.
    Tier D: 0 funded (1 not_funded) -> None.
    Tier F: 1 funded (1 defaulted) -> 1/1 = 1.0000."""
    stats = cb._compute_default_rates(_load_fixture())

    assert set(stats.keys()) == set(cb.TIERS), "every tier present, even empties"

    a = stats["A"]
    assert a.n_total == 3
    assert a.n_funded_paid_off == 1
    assert a.n_funded_current == 1
    assert a.n_funded_defaulted == 1
    assert a.default_rate == Decimal("0.3333")

    b = stats["B"]
    assert b.n_total == 2
    assert b.n_funded_defaulted == 1
    assert b.n_funded_paid_off == 1
    assert b.default_rate == Decimal("0.5000")

    c = stats["C"]
    assert c.n_total == 2
    assert c.n_funded_renewed == 1
    assert c.n_unknown == 1
    assert c.default_rate == Decimal("0.0000")

    d = stats["D"]
    assert d.n_total == 1
    assert d.n_not_funded == 1
    # 0 funded-known -> None, NOT 0.0000. Reporting "0% default" on
    # zero data would be a lie.
    assert d.default_rate is None

    f = stats["F"]
    assert f.n_total == 1
    assert f.n_funded_defaulted == 1
    assert f.default_rate == Decimal("1.0000")


def test_decimal_precision_one_of_three_is_exactly_0_3333() -> None:
    """Regression guard against float drift. ``1/3`` as a Python float is
    ``0.3333333333333333``; the script must produce exactly ``Decimal("0.3333")``
    (quantized to four places via ROUND_HALF_UP). The Decimal-only
    money rule in CLAUDE.md applies to rates too — POD on the wrong
    rounding mode would understate or overstate defaults."""
    rows = [
        cb.CohortRow(uuid4(), "A", "funded_paid_off", 80, None),
        cb.CohortRow(uuid4(), "A", "funded_current", 82, None),
        cb.CohortRow(uuid4(), "A", "funded_defaulted", 81, None),
    ]
    stats = cb._compute_default_rates(rows)
    assert stats["A"].default_rate == Decimal("0.3333")
    # Belt-and-suspenders: the value is a real Decimal, never a float.
    assert isinstance(stats["A"].default_rate, Decimal)


def test_empty_corpus_yields_all_tiers_with_no_rate() -> None:
    """The empty-corpus path produces every tier with ``n_total=0`` and
    ``default_rate=None`` — a structurally correct empty report."""
    stats = cb._compute_default_rates([])
    assert set(stats.keys()) == set(cb.TIERS)
    for tier in cb.TIERS:
        s = stats[tier]
        assert s.n_total == 0
        assert s.n_funded_defaulted == 0
        assert s.n_funded_paid_off == 0
        assert s.n_funded_current == 0
        assert s.n_funded_renewed == 0
        assert s.n_not_funded == 0
        assert s.n_unknown == 0
        assert s.default_rate is None


# ---------------------------------------------------------------------
# _classify_opportunity_status
# ---------------------------------------------------------------------


def test_classify_status_default_label_returns_defaulted() -> None:
    assert (
        cb._classify_opportunity_status("won", "Funded - Defaulted")
        == "funded_defaulted"
    )
    assert cb._classify_opportunity_status("lost", "Charged off") == "funded_defaulted"


def test_classify_status_paid_off() -> None:
    assert cb._classify_opportunity_status("won", "Paid Off") == "funded_paid_off"
    assert cb._classify_opportunity_status("won", "Paid In Full") == "funded_paid_off"


def test_classify_status_renewed() -> None:
    assert cb._classify_opportunity_status("won", "Renewed") == "funded_renewed"


def test_classify_status_active_is_current() -> None:
    assert cb._classify_opportunity_status("won", "Funded - Active") == "funded_current"
    assert cb._classify_opportunity_status("won", "Performing") == "funded_current"


def test_classify_status_lost_unrelated_label_is_not_funded() -> None:
    assert (
        cb._classify_opportunity_status("lost", "Dead - No response") == "not_funded"
    )


def test_classify_status_won_unknown_label_stays_unknown() -> None:
    """Conservative: a 'won' opportunity with a label we have not
    validated must NOT silently default to ``funded_current``. We
    rather over-report ``unknown`` than fabricate POD signal."""
    assert cb._classify_opportunity_status("won", "Some New Label") == "unknown"


def test_classify_status_blank_inputs_are_unknown() -> None:
    assert cb._classify_opportunity_status(None, None) == "unknown"
    assert cb._classify_opportunity_status("", "") == "unknown"


# ---------------------------------------------------------------------
# _resolve_close_outcome — worst-case fold
# ---------------------------------------------------------------------


def test_resolve_close_outcome_worst_of_many_opportunities() -> None:
    """A merchant with one paid_off deal AND one defaulted deal counts
    as defaulted — defaults dominate the POD bucket."""
    fake = _FakeCloseClient(
        leads={
            "lead_x": {
                "opportunities": [
                    {"status_type": "won", "status_label": "Paid Off"},
                    {"status_type": "won", "status_label": "Funded - Defaulted"},
                ]
            }
        }
    )
    assert cb._resolve_close_outcome("lead_x", fake) == "funded_defaulted"


def test_resolve_close_outcome_missing_lead_id_is_unknown() -> None:
    fake = _FakeCloseClient(leads={})
    assert cb._resolve_close_outcome(None, fake) == "unknown"


def test_resolve_close_outcome_close_error_is_unknown() -> None:
    """Close errors (404, 5xx, transport) MUST NOT crash the script —
    one bad lead can't poison the rest of the cohort."""
    fake = _FakeCloseClient(leads={})
    assert cb._resolve_close_outcome("missing_lead", fake) == "unknown"


def test_resolve_close_outcome_no_opportunities_is_unknown() -> None:
    fake = _FakeCloseClient(leads={"lead_y": {"opportunities": []}})
    assert cb._resolve_close_outcome("lead_y", fake) == "unknown"


# ---------------------------------------------------------------------
# run() — end-to-end with injected fakes
# ---------------------------------------------------------------------


def test_run_empty_data_source_returns_empty_stats() -> None:
    """No mature merchants -> structurally correct empty report."""
    source = _FakeDataSource(rows=[])
    close = _FakeCloseClient(leads={})

    stats, rows = cb.run(data_source=source, close_client=close)
    assert rows == []
    assert all(s.n_total == 0 for s in stats.values())
    assert all(s.default_rate is None for s in stats.values())


def test_run_resolves_outcomes_via_close_and_buckets() -> None:
    """One tier-B merchant funded then defaulted; one tier-A merchant
    paid off cleanly. Asserts tier-by-tier rollup matches the resolution."""
    merchant_a = uuid4()
    merchant_b = uuid4()
    source = _FakeDataSource(
        rows=[
            cb.CohortInputRow(
                merchant_id=merchant_a,
                close_lead_id="lead_a",
                tier="A",
                score=86,
                intake_date=date(2025, 10, 1),
            ),
            cb.CohortInputRow(
                merchant_id=merchant_b,
                close_lead_id="lead_b",
                tier="B",
                score=71,
                intake_date=date(2025, 11, 1),
            ),
        ]
    )
    close = _FakeCloseClient(
        leads={
            "lead_a": {
                "opportunities": [
                    {"status_type": "won", "status_label": "Paid Off"}
                ]
            },
            "lead_b": {
                "opportunities": [
                    {"status_type": "won", "status_label": "Funded - Defaulted"}
                ]
            },
        }
    )

    stats, rows = cb.run(data_source=source, close_client=close)

    assert {r.merchant_id for r in rows} == {merchant_a, merchant_b}
    assert stats["A"].n_funded_paid_off == 1
    assert stats["A"].default_rate == Decimal("0.0000")
    assert stats["B"].n_funded_defaulted == 1
    assert stats["B"].default_rate == Decimal("1.0000")


def test_format_report_renders_dash_for_no_data_tiers() -> None:
    """An empty tier must render with an em-dash for default_rate so the
    operator can visually distinguish 'no data' from '0% default'."""
    stats = cb._compute_default_rates([])
    report = cb._format_report(stats)
    # All five tier labels must be present, all default_rate cells
    # render the literal em-dash.
    for tier in cb.TIERS:
        assert tier in report
    assert "—" in report


def test_stats_to_json_serializes_decimal_as_string() -> None:
    """JSON dumped to ``reports/cohort_backtest_*.json`` must keep the
    Decimal precision — that's why we render rates as strings, not
    floats, in the persisted output."""
    rows = [
        cb.CohortRow(uuid4(), "A", "funded_paid_off", 80, None),
        cb.CohortRow(uuid4(), "A", "funded_current", 82, None),
        cb.CohortRow(uuid4(), "A", "funded_defaulted", 81, None),
    ]
    stats = cb._compute_default_rates(rows)
    payload = cb._stats_to_json(stats)
    assert payload["tiers"]["A"]["default_rate"] == "0.3333"
    # Empty tiers report null, not "None" / "0".
    assert payload["tiers"]["F"]["default_rate"] is None
