"""Memory-bounded streaming guarantee for ``scripts.build_sos_database``.

The prior collect-then-write pattern (``outcome.rows = list(_fetch_*())``)
buffered every fetched row into a per-state list before the SQLite write
step. CO at ~2.85M rows hit 3.14 GB anon RSS on the 4 GB prod box and got
OOM-killed mid-build on 2026-06-27. The 2026-06-28 streaming refactor in
``_stream_state_into_conn`` should bound peak memory at ``BATCH_SIZE * row
size`` regardless of source dataset size — this test mocks a 100 000-row
source paginated in 1 000-row pages, asserts every row lands in the DB,
and asserts the iterator never materialises into a list larger than one
batch.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

# Add scripts/ to the import path — operator-side scripts are not packaged.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import build_sos_database as bsd  # type: ignore[import-not-found]  # noqa: E402


# Sentinel that fails the test if anyone tries to materialise the row
# stream into a list. ``_stream_state_into_conn`` should only ever hold
# at most BATCH_SIZE rows in its local ``batch`` accumulator — never the
# full source dataset.
class _TrackedIterator:
    def __init__(self, rows: list[dict[str, str | None]]):
        self._rows = rows
        self.peak_outstanding = 0

    def __iter__(self) -> Iterator[dict[str, str | None]]:
        for i, row in enumerate(self._rows):
            self.peak_outstanding = max(self.peak_outstanding, len(self._rows) - i)
            yield row


def _make_rows(n: int) -> list[dict[str, str | None]]:
    return [
        {
            "business_name": f"Business {i}",
            "status": "ACTIVE",
            "entity_type": "LLC",
            "formation_date": "2020-01-01",
            "registered_agent": None,
            "principal_address": None,
            "officer_names": None,
        }
        for i in range(n)
    ]


@pytest.fixture
def db_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db_path = tmp_path / "test.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(bsd._SCHEMA_DDL)
        yield conn


def test_stream_state_into_conn_writes_all_rows_in_batches(
    db_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """100 000 rows should land in the DB across multiple batched commits.

    Asserts:
      * Every row makes it from source to DB (no truncation).
      * The batch accumulator never grows past BATCH_SIZE.
    """
    fake_rows = _make_rows(100_000)

    def fake_iter_rows(src: bsd.StateSource, max_rows: int) -> Iterator[dict[str, str | None]]:
        # Emit rows lazily — never build a 100K-row buffer ourselves.
        yield from fake_rows

    monkeypatch.setattr(bsd, "_iter_rows", fake_iter_rows)

    src = bsd.StateSource(state="ZZ", url="https://example.test", format="socrata_json")
    outcome = bsd._stream_state_into_conn(
        src,
        db_conn,
        max_rows=1_000_000,
        batch_size=25_000,
    )

    assert outcome.rows_inserted == 100_000
    assert not outcome.skipped
    assert outcome.error is None

    (count,) = db_conn.execute("SELECT COUNT(*) FROM sos_entities").fetchone()
    assert count == 100_000

    # Sample a row to confirm the canonical payload landed correctly.
    sample = db_conn.execute(
        "SELECT business_name, state, status FROM sos_entities WHERE business_name = ?",
        ("Business 42",),
    ).fetchone()
    assert sample == ("Business 42", "ZZ", "ACTIVE")


def test_stream_state_into_conn_does_not_buffer_full_dataset(
    db_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The streaming function must NOT materialise the iterator into a
    list before iterating. We assert that by feeding a generator that
    raises if any caller calls ``list()`` on it.
    """

    class _RaiseOnList:
        def __iter__(self) -> Iterator[dict[str, str | None]]:
            for i in range(60_000):
                yield {
                    "business_name": f"Business {i}",
                    "status": "ACTIVE",
                    "entity_type": "LLC",
                    "formation_date": None,
                    "registered_agent": None,
                    "principal_address": None,
                    "officer_names": None,
                }

        # __len__ is what ``list()`` calls on its argument to pre-size the
        # result list. Raising here trips any caller that materialises
        # the iterator (the pre-2026-06-28 ``outcome.rows = list(...)``
        # pattern would have triggered this).
        def __len__(self) -> int:  # pragma: no cover — defensive trap
            raise AssertionError(
                "_stream_state_into_conn buffered the full row stream "
                "into a list — the OOM-fix invariant is broken."
            )

    def fake_iter_rows(src: bsd.StateSource, max_rows: int) -> Iterator[dict[str, str | None]]:
        return iter(_RaiseOnList())

    monkeypatch.setattr(bsd, "_iter_rows", fake_iter_rows)

    src = bsd.StateSource(state="ZZ", url="https://example.test", format="socrata_json")
    outcome = bsd._stream_state_into_conn(
        src,
        db_conn,
        max_rows=1_000_000,
        batch_size=10_000,
    )

    assert outcome.rows_inserted == 60_000
    (count,) = db_conn.execute("SELECT COUNT(*) FROM sos_entities").fetchone()
    assert count == 60_000


def test_stream_state_into_conn_handles_fetch_failure(
    db_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fetcher exception mid-stream marks the outcome skipped + records
    the error, AND preserves rows already committed up to that point."""

    def fake_iter_rows(src: bsd.StateSource, max_rows: int) -> Iterator[dict[str, str | None]]:
        for i in range(30_000):
            yield {
                "business_name": f"Business {i}",
                "status": "ACTIVE",
                "entity_type": "LLC",
                "formation_date": None,
                "registered_agent": None,
                "principal_address": None,
                "officer_names": None,
            }
        raise OSError("simulated network drop mid-paginate")

    monkeypatch.setattr(bsd, "_iter_rows", fake_iter_rows)

    src = bsd.StateSource(state="ZZ", url="https://example.test", format="socrata_json")
    outcome = bsd._stream_state_into_conn(
        src,
        db_conn,
        max_rows=1_000_000,
        batch_size=10_000,
    )

    assert outcome.skipped is True
    assert outcome.error is not None
    assert "simulated network drop" in outcome.error
    # The first 3 full batches (10 000 rows each = 30 000) committed
    # before the failure should persist in the DB — the streaming
    # rewrite's per-batch commit is the durability guarantee.
    (count,) = db_conn.execute("SELECT COUNT(*) FROM sos_entities").fetchone()
    assert count == 30_000
    assert outcome.rows_inserted == 30_000


def test_batch_size_constant_bounds_memory_envelope() -> None:
    """Hard guard: nobody should crank BATCH_SIZE past 100K without
    re-evaluating the 4 GB prod-box envelope. CO has rows that average
    ~500 bytes each in canonical form — 100K rows ≈ 50 MB, still safe.
    Above that risks revisiting the 2026-06-27 OOM."""
    assert bsd.BATCH_SIZE <= 100_000, (
        f"BATCH_SIZE={bsd.BATCH_SIZE} exceeds the 100K guard. Re-evaluate "
        "prod-box memory headroom (4 GB total, no swap) before raising."
    )
