"""Test (b): OFAC cache fail-closed-after-7-days.

Set the cache mtime to 8 days ago, mock the fetcher to fail, and confirm
that scoring blocks. This is the safety guarantee — a Treasury outage
must never silently allow a sanctioned name through.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aegis.scoring.ofac import (
    HARD_CUTOFF,
    REFRESH_WINDOW,
    OFACClient,
    OFACStaleError,
)


def _write_cache(path: Path, entries: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "entries": entries,
                "refreshed_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )


def _set_mtime(path: Path, age: timedelta) -> None:
    target = time.time() - age.total_seconds()
    os.utime(path, (target, target))


def test_fresh_cache_does_not_call_fetcher(tmp_path: Path) -> None:
    cache = tmp_path / "sdn.json"
    _write_cache(cache, [{"primary_name": "Sanctioned Co", "aliases": []}])

    def panic() -> bytes:
        raise AssertionError("fetcher must not run for a fresh cache")

    client = OFACClient(cache_path=cache, fetcher=panic, now=lambda: datetime.now(UTC))
    assert client.is_match("Sanctioned Co") is True


def test_stale_under_cutoff_falls_back_to_stale_when_fetch_fails(tmp_path: Path) -> None:
    """24h-7d window: fetch failure is acceptable; stale cache is used."""
    cache = tmp_path / "sdn.json"
    _write_cache(cache, [{"primary_name": "Sanctioned Co", "aliases": []}])
    _set_mtime(cache, timedelta(days=3))  # 3 days old → between REFRESH_WINDOW and HARD_CUTOFF

    fetch_calls = {"n": 0}

    def failing_fetch() -> bytes:
        fetch_calls["n"] += 1
        raise RuntimeError("treasury timeout")

    client = OFACClient(
        cache_path=cache,
        fetcher=failing_fetch,
        now=lambda: datetime.now(UTC),
    )
    # Should NOT raise: stale cache within hard cutoff is allowed when refresh fails.
    assert client.is_match("Sanctioned Co") is True
    assert fetch_calls["n"] == 1, "fetcher should have been attempted"


def test_8_days_old_cache_with_failing_fetch_blocks(tmp_path: Path) -> None:
    """The criterion-(b) test: 8-day mtime + fetch failure → OFACStaleError.

    Scoring callers see the exception and stop; no sanctioned merchant is
    ever scored against a stale list.
    """
    cache = tmp_path / "sdn.json"
    _write_cache(cache, [{"primary_name": "Anything", "aliases": []}])
    _set_mtime(cache, timedelta(days=8))

    def failing_fetch() -> bytes:
        raise RuntimeError("treasury 503")

    client = OFACClient(
        cache_path=cache,
        fetcher=failing_fetch,
        now=lambda: datetime.now(UTC),
    )
    with pytest.raises(OFACStaleError, match=r"cache .* refresh failed"):
        client.is_match("Acme Co")


def test_no_cache_with_failing_fetch_also_blocks(tmp_path: Path) -> None:
    cache = tmp_path / "missing" / "sdn.json"
    # Don't create the file at all.

    def failing_fetch() -> bytes:
        raise RuntimeError("first run, no network")

    client = OFACClient(
        cache_path=cache,
        fetcher=failing_fetch,
        now=lambda: datetime.now(UTC),
    )
    with pytest.raises(OFACStaleError):
        client.is_match("Acme Co")


def test_8_day_cache_refreshes_when_fetch_succeeds(tmp_path: Path) -> None:
    """If the fetch DOES work, stale-but-too-old should self-heal."""
    cache = tmp_path / "sdn.json"
    _write_cache(cache, [{"primary_name": "Old Entry", "aliases": []}])
    _set_mtime(cache, timedelta(days=8))

    new_payload = json.dumps(
        {
            "entries": [{"primary_name": "Refreshed Entry", "aliases": []}],
            "refreshed_at": datetime.now(UTC).isoformat(),
        }
    ).encode("utf-8")

    def good_fetch() -> bytes:
        return new_payload

    client = OFACClient(
        cache_path=cache,
        fetcher=good_fetch,
        now=lambda: datetime.now(UTC),
    )
    assert client.is_match("Refreshed Entry") is True
    assert client.is_match("Old Entry") is False


def test_constants_are_what_we_documented() -> None:
    assert REFRESH_WINDOW == timedelta(hours=24)
    assert HARD_CUTOFF == timedelta(days=7)


def test_token_match_handles_substrings_and_aliases(tmp_path: Path) -> None:
    cache = tmp_path / "sdn.json"
    _write_cache(
        cache,
        [
            {
                "primary_name": "Putin, Vladimir Vladimirovich",
                "aliases": ["Vladimir Putin"],
            }
        ],
    )

    def panic() -> bytes:
        raise AssertionError("fresh cache should not refresh")

    client = OFACClient(cache_path=cache, fetcher=panic, now=lambda: datetime.now(UTC))
    assert client.is_match("Acme Co (owned by Vladimir Putin)") is True
    assert client.is_match("Acme Co") is False
