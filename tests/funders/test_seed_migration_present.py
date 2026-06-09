"""Migration 035 — production funder catalog seed (R0.1).

Validates the seed migration file directly:

  * At least 8 funders are seeded (the eight named in the audit:
    OnDeck, Rapid Finance, Forward Financing, Credibly, Kapitus,
    Mulligan Funding, CFG Merchant Solutions, Pearl Capital).
  * Every seeded row sets `active = true`.
  * Every seeded row populates the columns the matcher reads
    (`src/aegis/scoring/match_funders.py`): min_monthly_revenue,
    min_credit_score, min_months_in_business, max_positions,
    min_advance, max_advance, max_nsf_tolerance, factor / holdback
    envelopes, excluded_industries, excluded_states.
  * Every seeded row uses `ON CONFLICT (name) DO NOTHING` (idempotent
    re-run safety).
  * Every parsed name+criteria set produces a model-valid `FunderRow`
    via `InMemoryFunderRepository.upsert()` — proves the SQL values
    round-trip through the Pydantic model the repository hands to the
    matcher.
  * Once those rows are inserted into an InMemoryFunderRepository,
    `list_active()` returns >= 8 rows.

Test scope intentionally NOT covered
------------------------------------
This test does NOT exercise SupabaseFunderRepository against a live
Postgres instance — that is an integration concern, not a migration-
content concern. The contract enforced here is "the migration file's
INSERT block, when its values are turned into FunderRow objects, is
valid and discoverable through the repository protocol the matcher
depends on." Per CLAUDE.md operating principle #4, we do not seed the
real production database from a test.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from aegis.funders.models import FunderRow
from aegis.funders.repository import InMemoryFunderRepository

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "035_seed_funders_production.sql"
)

EXPECTED_FUNDER_NAMES: tuple[str, ...] = (
    "OnDeck",
    "Rapid Finance",
    "Forward Financing",
    "Credibly",
    "Kapitus",
    "Mulligan Funding",
    "CFG Merchant Solutions",
    "Pearl Capital",
)


# ---------------------------------------------------------------------
# Helpers — parse the migration's INSERT into structured Python rows.
# ---------------------------------------------------------------------

# Column order is fixed by the INSERT column list; we parse that
# header dynamically to stay robust against future re-ordering.
_INSERT_RE = re.compile(
    r"INSERT\s+INTO\s+funders\s*\((?P<cols>[^)]+)\)\s*VALUES\s*(?P<body>.*?)"
    r"ON\s+CONFLICT",
    re.IGNORECASE | re.DOTALL,
)


def _parse_columns(raw: str) -> list[str]:
    return [c.strip() for c in raw.split(",") if c.strip()]


def _split_value_tuples(body: str) -> list[str]:
    """Split top-level (...) tuples from the VALUES body, ignoring
    parentheses inside ARRAY[...]::TEXT[] casts.

    Tracks depth across both '(' and '['.
    """
    tuples: list[str] = []
    depth = 0
    start: int | None = None
    for i, ch in enumerate(body):
        if ch in "([":
            if depth == 0 and ch == "(":
                start = i + 1
            depth += 1
        elif ch in ")]":
            depth -= 1
            if depth == 0 and start is not None and ch == ")":
                tuples.append(body[start:i])
                start = None
    return tuples


def _split_value_fields(tup: str) -> list[str]:
    """Split a single VALUES tuple's fields on commas at top level,
    skipping commas inside ARRAY[...] / quoted strings."""
    fields: list[str] = []
    depth = 0
    in_quote = False
    start = 0
    i = 0
    while i < len(tup):
        ch = tup[i]
        if ch == "'" and (i == 0 or tup[i - 1] != "\\"):
            in_quote = not in_quote
        elif not in_quote:
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            elif ch == "," and depth == 0:
                fields.append(tup[start:i].strip())
                start = i + 1
        i += 1
    if start < len(tup):
        fields.append(tup[start:].strip())
    return fields


def _coerce(raw: str) -> Any:
    """Coerce a SQL literal token into a Python value usable by FunderRow."""
    token = raw.strip()
    if token.lower() == "true":
        return True
    if token.lower() == "false":
        return False
    if token.lower() == "null":
        return None
    # SQL string literal -> peel quotes, but only if quotes wrap the whole token.
    if token.startswith("'") and token.endswith("'") and len(token) >= 2:
        return token[1:-1].replace("''", "'")
    # ARRAY[...]::TEXT[] -> list[str]
    array_match = re.match(
        r"ARRAY\s*\[(.*)\]\s*::\s*TEXT\s*\[\s*\]",
        token,
        re.IGNORECASE | re.DOTALL,
    )
    if array_match:
        inner = array_match.group(1).strip()
        if not inner:
            return []
        # Items are quoted strings separated by commas.
        items = [s.strip() for s in inner.split(",")]
        return [
            s[1:-1].replace("''", "'") if s.startswith("'") and s.endswith("'") else s
            for s in items
        ]
    # Numeric (Decimal-preserving via str).
    try:
        if "." in token:
            return Decimal(token)
        return int(token)
    except (ValueError, ArithmeticError):
        return token


def _parse_migration(path: Path) -> list[dict[str, Any]]:
    sql = path.read_text(encoding="utf-8")
    # Strip line comments so they don't appear inside the regex body.
    sql_no_comments = "\n".join(
        line.split("--", 1)[0] for line in sql.splitlines()
    )
    match = _INSERT_RE.search(sql_no_comments)
    if not match:
        # ruff S608 false positive: this is an AssertionError message
        # describing what the test expects to find, not a SQL string
        # being executed. The regex `_INSERT_RE` is a static fixture
        # matched against migration-file content read from disk.
        raise AssertionError(
            "Could not locate `INSERT INTO funders (...) VALUES ... ON CONFLICT` "  # noqa: S608
            f"in {path}. The migration's structure has changed; update this test."
        )
    cols = _parse_columns(match.group("cols"))
    raw_tuples = _split_value_tuples(match.group("body"))
    rows: list[dict[str, Any]] = []
    for tup in raw_tuples:
        fields = _split_value_fields(tup)
        if len(fields) != len(cols):
            raise AssertionError(
                f"Migration row has {len(fields)} fields but header lists "
                f"{len(cols)} columns. Mismatched VALUES tuple:\n{tup!r}"
            )
        rows.append({col: _coerce(val) for col, val in zip(cols, fields, strict=True)})
    return rows


def _row_to_funder(row: dict[str, Any]) -> FunderRow:
    """Build a FunderRow from a parsed-migration dict."""
    def _dec(key: str) -> Decimal | None:
        v = row.get(key)
        if v is None:
            return None
        return v if isinstance(v, Decimal) else Decimal(str(v))

    return FunderRow(
        name=row["name"],
        active=bool(row.get("active", True)),
        min_monthly_revenue=_dec("min_monthly_revenue"),
        min_avg_daily_balance=_dec("min_avg_daily_balance"),
        min_credit_score=row.get("min_credit_score"),
        min_months_in_business=row.get("min_months_in_business"),
        max_positions=row.get("max_positions"),
        accepts_stacking=bool(row.get("accepts_stacking", False)),
        min_advance=_dec("min_advance"),
        max_advance=_dec("max_advance"),
        max_nsf_tolerance=row.get("max_nsf_tolerance"),
        requires_coj=bool(row.get("requires_coj", False)),
        charges_merchant_advance_fees=bool(
            row.get("charges_merchant_advance_fees", False)
        ),
        typical_factor_low=_dec("typical_factor_low"),
        typical_factor_high=_dec("typical_factor_high"),
        typical_holdback_low=_dec("typical_holdback_low"),
        typical_holdback_high=_dec("typical_holdback_high"),
        excluded_industries=tuple(row.get("excluded_industries") or ()),
        excluded_states=tuple(row.get("excluded_states") or ()),
        notes_residual=row.get("notes_residual") or "",
    )


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def parsed_rows() -> list[dict[str, Any]]:
    assert MIGRATION_PATH.exists(), f"missing migration file: {MIGRATION_PATH}"
    return _parse_migration(MIGRATION_PATH)


def test_migration_file_present() -> None:
    assert MIGRATION_PATH.exists(), (
        f"Expected seed migration at {MIGRATION_PATH}. R0.1 not implemented."
    )


def test_migration_uses_on_conflict_do_nothing() -> None:
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    assert re.search(
        r"ON\s+CONFLICT\s*\(\s*name\s*\)\s*DO\s+NOTHING",
        sql,
        re.IGNORECASE,
    ), "Migration must be idempotent via ON CONFLICT (name) DO NOTHING."


def test_at_least_eight_funders_seeded(parsed_rows: list[dict[str, Any]]) -> None:
    assert len(parsed_rows) >= 8, (
        f"R0.1 requires at least 8 funders; migration seeds {len(parsed_rows)}."
    )


def test_all_expected_funder_names_present(parsed_rows: list[dict[str, Any]]) -> None:
    seeded = {r["name"] for r in parsed_rows}
    missing = set(EXPECTED_FUNDER_NAMES) - seeded
    assert not missing, (
        f"Migration is missing required funder names: {sorted(missing)}. "
        f"Found: {sorted(seeded)}"
    )


def test_every_seeded_funder_is_active(parsed_rows: list[dict[str, Any]]) -> None:
    for row in parsed_rows:
        assert row.get("active") is True, (
            f"Funder {row.get('name')!r} must be seeded with active=true; "
            f"got active={row.get('active')!r}"
        )


def test_every_seeded_funder_populates_matcher_required_fields(
    parsed_rows: list[dict[str, Any]],
) -> None:
    """Matcher (src/aegis/scoring/match_funders.py) reads these columns.

    Treat a NULL on any of them as a seed gap — a NULL silently means
    "no policy", but for a launch-blocker seed we want every funder
    to participate in every check the matcher runs.
    """
    required_keys = (
        "min_monthly_revenue",
        "min_credit_score",
        "min_months_in_business",
        "max_positions",
        "min_advance",
        "max_advance",
        "max_nsf_tolerance",
        "typical_factor_low",
        "typical_factor_high",
        "typical_holdback_low",
        "typical_holdback_high",
        "excluded_industries",
        "excluded_states",
    )
    for row in parsed_rows:
        name = row["name"]
        for key in required_keys:
            assert row.get(key) is not None, (
                f"Funder {name!r} has NULL for matcher-critical column {key!r}; "
                f"R0.1 requires defensible non-null values."
            )
            if key in ("excluded_industries", "excluded_states"):
                val = row[key]
                assert isinstance(val, list), (
                    f"Funder {name!r} column {key!r} must be a list; got {type(val)}"
                )


def test_money_columns_are_decimal_compatible(
    parsed_rows: list[dict[str, Any]],
) -> None:
    money_keys = (
        "min_monthly_revenue",
        "min_avg_daily_balance",
        "min_advance",
        "max_advance",
    )
    for row in parsed_rows:
        for key in money_keys:
            val = row.get(key)
            if val is None:
                continue
            # Migration writes literals like 20000.00; parser returns Decimal.
            assert isinstance(val, Decimal), (
                f"Funder {row['name']!r} column {key!r}={val!r} should "
                f"parse as Decimal (use NN.NN literal form in the migration)."
            )


def test_factor_envelope_low_le_high(parsed_rows: list[dict[str, Any]]) -> None:
    for row in parsed_rows:
        low = row.get("typical_factor_low")
        high = row.get("typical_factor_high")
        if low is not None and high is not None:
            assert low <= high, (
                f"{row['name']!r}: typical_factor_low ({low}) "
                f"must be <= typical_factor_high ({high})."
            )


def test_holdback_envelope_low_le_high(parsed_rows: list[dict[str, Any]]) -> None:
    for row in parsed_rows:
        low = row.get("typical_holdback_low")
        high = row.get("typical_holdback_high")
        if low is not None and high is not None:
            assert low <= high, (
                f"{row['name']!r}: typical_holdback_low ({low}) "
                f"must be <= typical_holdback_high ({high})."
            )


def test_min_advance_le_max_advance(parsed_rows: list[dict[str, Any]]) -> None:
    for row in parsed_rows:
        mn = row.get("min_advance")
        mx = row.get("max_advance")
        if mn is not None and mx is not None:
            assert mn <= mx, (
                f"{row['name']!r}: min_advance ({mn}) must be <= max_advance ({mx})."
            )


def test_every_row_is_pydantic_valid(parsed_rows: list[dict[str, Any]]) -> None:
    """Every parsed migration row must validate as a FunderRow."""
    for row in parsed_rows:
        funder = _row_to_funder(row)
        assert funder.name == row["name"]
        assert funder.active is True


def test_in_memory_repository_round_trip_reaches_eight(
    parsed_rows: list[dict[str, Any]],
) -> None:
    """End-to-end repository contract: after inserting every parsed
    migration row, list_active() returns >= 8 rows in name-sorted order
    and all the matcher-facing fields are accessible per funder."""
    repo = InMemoryFunderRepository()
    for row in parsed_rows:
        repo.upsert(_row_to_funder(row))
    active = repo.list_active()
    assert len(active) >= 8, (
        f"Expected >= 8 active funders post-seed, got {len(active)}: "
        f"{[f.name for f in active]}"
    )
    # Spot-check the matcher-facing surface on every funder.
    for funder in active:
        assert funder.min_monthly_revenue is not None
        assert funder.min_credit_score is not None
        assert funder.min_months_in_business is not None
        assert funder.max_positions is not None
        assert funder.min_advance is not None
        assert funder.max_advance is not None
        assert funder.max_nsf_tolerance is not None
        assert funder.excluded_industries, (
            f"{funder.name!r} has empty excluded_industries; even permissive "
            f"funders should exclude cannabis / firearms / payday."
        )
        assert funder.excluded_states, (
            f"{funder.name!r} has empty excluded_states; SD/ND/NV at minimum."
        )


def test_common_industry_exclusions_present(parsed_rows: list[dict[str, Any]]) -> None:
    """Standard MCA exclusion list should be on every seeded funder."""
    must_exclude = {
        "cannabis",
        "firearms",
        "adult-entertainment",
        "multi-level-marketing",
        "debt-consolidation",
        "payday-lending",
    }
    for row in parsed_rows:
        present = set(row.get("excluded_industries") or [])
        missing = must_exclude - present
        assert not missing, (
            f"{row['name']!r} is missing required industry exclusions: "
            f"{sorted(missing)}. Present: {sorted(present)}"
        )
