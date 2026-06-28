"""Phase D — state-targeted UCC prompt + 50-state portal map (migration 086).

Covers the new ``UCC_STATE_PORTALS`` table, the rewritten
``build_ucc_prompt`` helper, the new structured-signal fields on
``UCCResult`` (``blanket_lien`` / ``mca_funder_detected`` /
``lien_position``), the migration 086 additive shape, and the dossier
verify-UCC HTMX flow.

All 50 states + DC must have a non-empty portal URL — the test makes
that contract explicit so a future trim or rename surfaces in CI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from aegis.business_intel.ucc_checker import (
    UCC_STATE_PORTALS,
    UCCResult,
    build_ucc_prompt,
    check_ucc_and_defaults,
)
from aegis.merchants.models import MerchantRow
from aegis.web.routers.merchants import _build_background_checks_context

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubClient:
    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.last_prompt: str | None = None

    def invoke_with_web_search(self, prompt: str) -> str:
        self.last_prompt = prompt
        return self.raw


# ---------------------------------------------------------------------------
# 1-3. build_ucc_prompt state injection
# ---------------------------------------------------------------------------


def test_build_ucc_prompt_includes_wy_portal_url() -> None:
    prompt = build_ucc_prompt("Acme LLC", "WY")
    assert "wyobiz.wy.gov" in prompt
    assert "Acme LLC" in prompt


def test_build_ucc_prompt_includes_fl_portal_url() -> None:
    prompt = build_ucc_prompt("Acme LLC", "FL")
    assert "sunbiz.org" in prompt


def test_build_ucc_prompt_unknown_state_falls_back_to_generic() -> None:
    prompt = build_ucc_prompt("Acme LLC", "XX")
    assert "the state Secretary of State website" in prompt
    assert "Acme LLC" in prompt


def test_build_ucc_prompt_case_insensitive_state() -> None:
    upper = build_ucc_prompt("Acme LLC", "ny")
    assert "appext20.dos.ny.gov" in upper or "dos.ny.gov" in upper


# ---------------------------------------------------------------------------
# 4. Portal-URL contract — every state + DC populated
# ---------------------------------------------------------------------------


_US_STATES = {
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "DC",
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
}


def test_all_states_and_dc_have_portal_url() -> None:
    missing = _US_STATES - set(UCC_STATE_PORTALS)
    assert not missing, f"states without portal URL: {sorted(missing)}"
    for state, url in UCC_STATE_PORTALS.items():
        assert url, f"empty portal URL for {state}"
        assert url.startswith("http"), f"non-http URL for {state}: {url!r}"


# ---------------------------------------------------------------------------
# 5. Migration 086 additive shape
# ---------------------------------------------------------------------------


def test_migration_086_uses_add_column_if_not_exists() -> None:
    """086 must be idempotent — re-applying it on a partially-bootstrapped
    DB must not error. Enforces ``ADD COLUMN IF NOT EXISTS`` on every
    new column and asserts the migration does NOT touch the existing
    068 columns (``ucc_filings`` / ``ucc_default_indicators`` /
    ``ucc_checked_at``)."""
    sql = (
        Path(__file__).resolve().parents[2] / "migrations" / "086_merchants_ucc_enhanced.sql"
    ).read_text(encoding="utf-8")

    # Every ALTER must be ADD COLUMN IF NOT EXISTS — no drop, no rename.
    assert "ADD COLUMN IF NOT EXISTS ucc_portal_url" in sql
    assert "ADD COLUMN IF NOT EXISTS ucc_operator_verified" in sql
    assert "ADD COLUMN IF NOT EXISTS ucc_verified_at" in sql

    # No-touch on 068 columns.
    assert "DROP COLUMN ucc_filings" not in sql
    assert "DROP COLUMN ucc_default_indicators" not in sql
    assert "DROP COLUMN ucc_checked_at" not in sql

    # Wrapped in a transaction.
    assert "BEGIN;" in sql
    assert "COMMIT;" in sql


def test_migration_086_probe_registered() -> None:
    """The bootstrap probe registry needs an entry for 086 keyed on the
    distinguishing column ``ucc_portal_url`` so backfilled databases
    are detected correctly."""
    from scripts.apply_migrations import MIGRATION_PROBES

    assert "086_merchants_ucc_enhanced.sql" in MIGRATION_PROBES
    probe = MIGRATION_PROBES["086_merchants_ucc_enhanced.sql"]
    assert "ucc_portal_url" in probe
    assert "information_schema.columns" in probe


# ---------------------------------------------------------------------------
# 6. Structured-signal parsing — blanket lien + MCA funder
# ---------------------------------------------------------------------------


def test_blanket_lien_flag_detected_from_structured_response() -> None:
    raw = (
        '{"ucc_filings": ["OnDeck Capital LLC"], '
        '"default_indicators": ["blanket_lien_all_assets"], '
        '"blanket_lien": true, '
        '"mca_funder_detected": true, '
        '"lien_position": "1st", '
        '"source_summary": "Active filing on WY portal; all assets."}'
    )
    client = _StubClient(raw)
    result = check_ucc_and_defaults("Acme Inc.", "WY", "Jane Doe", client=client)

    assert isinstance(result, UCCResult)
    assert result.blanket_lien is True
    assert result.mca_funder_detected is True
    assert result.lien_position == "1st"
    assert "OnDeck Capital LLC" in result.ucc_filings


def test_legacy_response_shape_still_parses() -> None:
    """Older Bedrock responses without the structured signals must still
    succeed; the new optional fields collapse to ``None``."""
    raw = (
        '{"ucc_filings": ["Some Lender"], '
        '"default_indicators": [], '
        '"source_summary": "One filing found."}'
    )
    client = _StubClient(raw)
    result = check_ucc_and_defaults("Acme Inc.", "WY", client=client)

    assert result.ucc_filings == ("Some Lender",)
    assert result.blanket_lien is None
    assert result.mca_funder_detected is None
    assert result.lien_position is None


def test_invalid_lien_position_collapses_to_none() -> None:
    raw = (
        '{"ucc_filings": [], "default_indicators": [], '
        '"blanket_lien": false, "mca_funder_detected": false, '
        '"lien_position": "fourth", '
        '"source_summary": "No active filings."}'
    )
    client = _StubClient(raw)
    result = check_ucc_and_defaults("Acme Inc.", "WY", client=client)
    assert result.lien_position is None


# ---------------------------------------------------------------------------
# 7. Prompt is the one sent to Bedrock (integration test)
# ---------------------------------------------------------------------------


def test_check_uses_state_targeted_prompt_for_known_state() -> None:
    raw = (
        '{"ucc_filings": [], "default_indicators": [], "source_summary": "No active UCC filings."}'
    )
    client = _StubClient(raw)
    check_ucc_and_defaults("Acme Inc.", "WY", "Jane Doe", client=client)
    assert client.last_prompt is not None
    assert "wyobiz.wy.gov" in client.last_prompt


# ---------------------------------------------------------------------------
# 8. _build_background_checks_context — portal URL fallback
# ---------------------------------------------------------------------------


def test_background_checks_context_falls_back_to_portal_map(
    fresh_merchant_factory: Any,
) -> None:
    """When a merchant row hasn't yet had ``ucc_portal_url`` persisted
    (pre-086 + post-086 backfill window), the dossier context must
    still surface the canonical URL keyed by state."""
    merchant = fresh_merchant_factory(state="WY", ucc_portal_url=None)
    ctx = _build_background_checks_context(merchant)
    assert ctx["ucc_portal_url"]
    assert "wyobiz.wy.gov" in ctx["ucc_portal_url"]


def test_background_checks_context_prefers_persisted_portal_url(
    fresh_merchant_factory: Any,
) -> None:
    merchant = fresh_merchant_factory(
        state="WY", ucc_portal_url="https://operator-override.example/"
    )
    ctx = _build_background_checks_context(merchant)
    assert ctx["ucc_portal_url"] == "https://operator-override.example/"


def test_background_checks_context_no_state_no_portal_url(
    fresh_merchant_factory: Any,
) -> None:
    merchant = fresh_merchant_factory(state=None, ucc_portal_url=None)
    ctx = _build_background_checks_context(merchant)
    assert ctx["ucc_portal_url"] is None


# ---------------------------------------------------------------------------
# Shared helper — fresh MerchantRow factory
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_merchant_factory() -> Any:
    def _make(**overrides: Any) -> MerchantRow:
        defaults: dict[str, Any] = {
            "id": uuid4(),
            "business_name": "Acme LLC",
            "state": "WY",
        }
        defaults.update(overrides)
        return MerchantRow(**defaults)

    return _make
