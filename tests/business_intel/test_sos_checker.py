"""Tests for ``aegis.business_intel.sos_checker``.

Four concerns mirror ``test_ucc_checker`` plus the new taxonomy:

1. Local SQLite hit short-circuits Bedrock — counts must be 0.
2. Missing state / DB falls back to Bedrock; failures collapse to the
   appropriate ``data_source`` (no_data / bedrock_error /
   bedrock_unparseable / bedrock_not_found / bedrock).
3. Name-normalization absorbs entity-suffix variants.
4. The ``AEGIS_SOS_FALLBACK_MODE`` env var routes between the
   prompt-only default and the legacy web-search path.

The 30-day TTL on ``ensure_sos_check`` is tested in
``test_sos_refresh.py``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from aegis.business_intel.sos_checker import (
    SOSChecker,
    normalize_business_name,
)


# ---------------------------------------------------------------------------
# Bedrock client stubs
# ---------------------------------------------------------------------------
class _StubBedrock:
    """Stub that returns a canned response from BOTH fallback methods.

    The checker dispatches to ``invoke_prompt_only`` by default and to
    ``invoke_with_web_search`` when ``AEGIS_SOS_FALLBACK_MODE=web_search``.
    The stub implements both so a test can flip the env var without
    rewriting the stub.
    """

    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.calls = 0
        self.last_prompt: str | None = None
        self.last_method: str | None = None

    def invoke_prompt_only(self, prompt: str) -> str:
        self.calls += 1
        self.last_prompt = prompt
        self.last_method = "invoke_prompt_only"
        return self.raw

    def invoke_with_web_search(self, prompt: str) -> str:
        self.calls += 1
        self.last_prompt = prompt
        self.last_method = "invoke_with_web_search"
        return self.raw


class _RaisingBedrock:
    def __init__(self) -> None:
        self.calls = 0

    def invoke_prompt_only(self, prompt: str) -> str:
        self.calls += 1
        raise RuntimeError("bedrock_unreachable")

    def invoke_with_web_search(self, prompt: str) -> str:
        self.calls += 1
        raise RuntimeError("bedrock_unreachable")


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE sos_entities (
    id INTEGER PRIMARY KEY,
    business_name TEXT NOT NULL,
    business_name_normalized TEXT NOT NULL,
    state TEXT NOT NULL,
    status TEXT,
    entity_type TEXT,
    formation_date TEXT,
    registered_agent TEXT,
    principal_address TEXT,
    officer_names TEXT,
    data_source TEXT,
    last_updated TEXT
);
CREATE INDEX idx_name_state ON sos_entities(business_name_normalized, state);
CREATE INDEX idx_state ON sos_entities(state);
"""


def _seed_db(path: Path, rows: list[dict[str, str | None]]) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(_SCHEMA)
        conn.executemany(
            """
            INSERT INTO sos_entities (
                business_name, business_name_normalized, state, status,
                entity_type, formation_date, registered_agent,
                principal_address, officer_names, data_source, last_updated
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    r["business_name"],
                    normalize_business_name(r["business_name"] or ""),
                    r["state"],
                    r.get("status"),
                    r.get("entity_type"),
                    r.get("formation_date"),
                    r.get("registered_agent"),
                    r.get("principal_address"),
                    r.get("officer_names"),
                    r.get("data_source", "sos_bulk_test"),
                    "2026-06-27T00:00:00+00:00",
                )
                for r in rows
            ],
        )


# ---------------------------------------------------------------------------
# 1. Local hit short-circuits Bedrock
# ---------------------------------------------------------------------------
def test_fl_entity_in_local_sqlite_no_bedrock_call(tmp_path: Path) -> None:
    db = tmp_path / "sos_entities.db"
    _seed_db(
        db,
        [
            {
                "business_name": "Acme Restaurant, LLC",
                "state": "FL",
                "status": "ACTIVE",
                "entity_type": "LLC",
                "formation_date": "2018-04-12",
            }
        ],
    )
    bedrock = _StubBedrock(json.dumps({"found": False}))
    checker = SOSChecker(db_path=db, bedrock_client=bedrock)

    result = checker.check_entity("Acme Restaurant LLC", "FL")

    assert bedrock.calls == 0
    assert result.found is True
    assert result.is_active is True
    assert result.status == "ACTIVE"
    assert result.data_source == "local_db:FL"
    assert result.formation_date == "2018-04-12"


# ---------------------------------------------------------------------------
# 2. Unknown state → Bedrock fallback path
# ---------------------------------------------------------------------------
def test_uncovered_state_falls_back_to_bedrock(tmp_path: Path) -> None:
    db = tmp_path / "sos_entities.db"
    _seed_db(db, [])  # empty DB
    bedrock = _StubBedrock(
        json.dumps(
            {
                "found": True,
                "status": "ACTIVE",
                "entity_name": "Test Corp",
                "formation_date": "2020-01-01",
            }
        )
    )
    checker = SOSChecker(db_path=db, bedrock_client=bedrock)

    result = checker.check_entity("Test Corp", "VT")

    assert bedrock.calls == 1
    assert bedrock.last_method == "invoke_prompt_only"
    assert result.found is True
    assert result.data_source == "bedrock"
    assert result.is_active is True
    assert "Vermont" in (bedrock.last_prompt or "") or "VT" in (bedrock.last_prompt or "")


# ---------------------------------------------------------------------------
# 3. Dissolved entity → is_active False, red chip semantics
# ---------------------------------------------------------------------------
def test_dissolved_entity_marks_is_active_false(tmp_path: Path) -> None:
    db = tmp_path / "sos_entities.db"
    _seed_db(
        db,
        [
            {
                "business_name": "Dead Corp Inc.",
                "state": "FL",
                "status": "DISSOLVED",
                "entity_type": "INC",
                "formation_date": "2010-01-01",
            }
        ],
    )
    bedrock = _StubBedrock("")
    checker = SOSChecker(db_path=db, bedrock_client=bedrock)

    result = checker.check_entity("Dead Corp Inc", "FL")

    assert result.found is True
    assert result.is_active is False
    assert result.status == "DISSOLVED"
    assert bedrock.calls == 0


# ---------------------------------------------------------------------------
# 4. Missing state → skips local + Bedrock + collapses cleanly
# ---------------------------------------------------------------------------
def test_missing_state_goes_to_bedrock_with_unknown_state(tmp_path: Path) -> None:
    # When state is None we still call Bedrock with "(unknown state — search
    # nationally)" rather than refuse outright — the operator can still get a
    # useful answer for a registered DBA without a state field. Local DB
    # lookup is bypassed entirely because the index requires a state column.
    db = tmp_path / "sos_entities.db"
    _seed_db(db, [])
    bedrock = _StubBedrock(json.dumps({"found": False}))
    checker = SOSChecker(db_path=db, bedrock_client=bedrock)

    result = checker.check_entity("Some Business", None)

    assert bedrock.calls == 1
    assert "unknown state" in (bedrock.last_prompt or "").lower()
    assert result.found is False


def test_empty_business_name_returns_no_data_no_bedrock(tmp_path: Path) -> None:
    db = tmp_path / "sos_entities.db"
    _seed_db(db, [])
    bedrock = _StubBedrock("")
    checker = SOSChecker(db_path=db, bedrock_client=bedrock)

    result = checker.check_entity("   ", "FL")

    assert bedrock.calls == 0
    assert result.found is False
    assert result.data_source == "no_data"
    assert result.error == "business_name_empty"


# ---------------------------------------------------------------------------
# 5. Name normalization absorbs entity-suffix variants
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Acme LLC", "ACMELLC"),
        ("Acme, L.L.C.", "ACMELLC"),
        ("ACME Limited Liability Company", "ACMELLC"),
        ("Acme L L C", "ACMELLC"),
        ("Foo Bar, Inc.", "FOOBARINC"),
        ("Foo Bar Incorporated", "FOOBARINC"),
        ("Sample Corporation", "SAMPLECORP"),
        ("Sample Corp.", "SAMPLECORP"),
    ],
)
def test_normalization_collapses_entity_suffixes(raw: str, expected: str) -> None:
    assert normalize_business_name(raw) == expected


def test_normalization_strips_punctuation_and_spaces() -> None:
    # Smoke: any combination of non-alphanumeric drops
    assert normalize_business_name("ABC-123 & Co., #5!") == "ABC123CO5"


# ---------------------------------------------------------------------------
# 6. Bedrock failure → empty result with bedrock_fallback source
# ---------------------------------------------------------------------------
def test_bedrock_raise_collapses_to_bedrock_error(tmp_path: Path) -> None:
    db = tmp_path / "sos_entities.db"
    _seed_db(db, [])  # empty → forces fallback
    bedrock = _RaisingBedrock()
    checker = SOSChecker(db_path=db, bedrock_client=bedrock)

    result = checker.check_entity("Whatever", "CA")

    assert bedrock.calls == 1
    assert result.found is False
    # Distinguishes "we tried and the call failed" (bedrock_error) from
    # "we never tried" (no_data).
    assert result.data_source == "bedrock_error"
    assert result.error == "bedrock_invoke_failed"


def test_bedrock_malformed_json_collapses_to_bedrock_unparseable(tmp_path: Path) -> None:
    db = tmp_path / "sos_entities.db"
    _seed_db(db, [])
    bedrock = _StubBedrock("not json at all")
    checker = SOSChecker(db_path=db, bedrock_client=bedrock)

    result = checker.check_entity("Whatever", "CA")

    assert bedrock.calls == 1
    assert result.found is False
    assert result.data_source == "bedrock_unparseable"
    assert result.error == "bedrock_parse_failed"


def test_bedrock_explicit_not_found_collapses_to_bedrock_not_found(tmp_path: Path) -> None:
    """The model answered cleanly but said no such entity — distinct
    signal from an unparseable / errored Bedrock call."""
    db = tmp_path / "sos_entities.db"
    _seed_db(db, [])
    bedrock = _StubBedrock(json.dumps({"found": False}))
    checker = SOSChecker(db_path=db, bedrock_client=bedrock)

    result = checker.check_entity("Whatever", "CA")

    assert bedrock.calls == 1
    assert result.found is False
    assert result.data_source == "bedrock_not_found"
    assert result.error is None


# ---------------------------------------------------------------------------
# 7. Missing DB file → fallback path, no crash
# ---------------------------------------------------------------------------
def test_missing_db_falls_back_to_bedrock(tmp_path: Path) -> None:
    db = tmp_path / "does_not_exist.db"
    bedrock = _StubBedrock(json.dumps({"found": True, "status": "ACTIVE", "entity_name": "X"}))
    checker = SOSChecker(db_path=db, bedrock_client=bedrock)

    result = checker.check_entity("X", "WA")

    assert bedrock.calls == 1
    assert result.found is True
    assert result.data_source == "bedrock"


def test_web_search_mode_uses_legacy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AEGIS_SOS_FALLBACK_MODE=web_search routes through the old tool-call
    method; the default ``prompt_only`` (or any other value) uses the new
    prompt-only path."""
    db = tmp_path / "sos_entities.db"
    _seed_db(db, [])
    bedrock = _StubBedrock(json.dumps({"found": True, "status": "ACTIVE", "entity_name": "X"}))
    checker = SOSChecker(db_path=db, bedrock_client=bedrock)

    monkeypatch.setenv("AEGIS_SOS_FALLBACK_MODE", "web_search")
    result = checker.check_entity("X", "NV")

    assert result.found is True
    assert bedrock.last_method == "invoke_with_web_search"
    assert result.data_source == "bedrock"


# ---------------------------------------------------------------------------
# 8. Fuzzy match within state via jellyfish (sanity — handles spacing drift)
# ---------------------------------------------------------------------------
def _has_jellyfish() -> bool:
    try:
        import jellyfish  # noqa: F401 — availability probe
    except ImportError:
        return False
    return True


@pytest.mark.skipif(not _has_jellyfish(), reason="jellyfish required for fuzzy match")
def test_fuzzy_match_within_state(tmp_path: Path) -> None:
    db = tmp_path / "sos_entities.db"
    _seed_db(
        db,
        [
            {
                "business_name": "Mountain View Roofing Services LLC",
                "state": "CO",
                "status": "ACTIVE",
                "entity_type": "LLC",
                "formation_date": "2015-06-01",
            }
        ],
    )
    bedrock = _StubBedrock("")
    checker = SOSChecker(db_path=db, bedrock_client=bedrock)

    # Query w/ minor wording drift; normalized form differs slightly but
    # jaro-winkler should still land within threshold (0.88).
    result = checker.check_entity("Mountain View Roofing Service, LLC", "CO")

    # Exact-match path should catch this (normalization absorbs the
    # comma + missing 's'? — it doesn't absorb 's', so this falls to
    # the fuzzy walk). Either way the row is found.
    assert result.found is True
    assert result.data_source == "local_db:CO"
    assert bedrock.calls == 0
