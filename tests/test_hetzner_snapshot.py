"""Tests for scripts/hetzner_snapshot.py (P7a).

The Hetzner snapshot cron delegates to the standalone script so the
worker never learns the Hetzner API shape. These tests exercise the
script's env-var gating (skip vs. run) and the pruning branch.

No live network calls: ``urllib.request.urlopen`` is monkeypatched with
a fake that records the requests and returns canned JSON.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from scripts import hetzner_snapshot as sh


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Drop Hetzner env vars so tests set exactly what they want."""
    monkeypatch.delenv("HETZNER_API_TOKEN", raising=False)
    monkeypatch.delenv("HETZNER_SERVER_ID", raising=False)
    yield


def test_run_skips_when_token_absent(clean_env: None, caplog: pytest.LogCaptureFixture) -> None:
    del clean_env
    with caplog.at_level("INFO"):
        rc = sh.run()
    assert rc == 0
    assert any("hetzner_snapshot.skipped" in r.message for r in caplog.records)
    assert any("no_token" in r.message for r in caplog.records)


def test_run_skips_when_server_id_absent(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    del clean_env
    monkeypatch.setenv("HETZNER_API_TOKEN", "test-token")
    with caplog.at_level("INFO"):
        rc = sh.run()
    assert rc == 0
    assert any("hetzner_snapshot.skipped" in r.message for r in caplog.records)
    assert any("no_server_id" in r.message for r in caplog.records)


class _FakeUrlopen:
    """Callable stand-in for ``urllib.request.urlopen`` with a queue."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._queue: list[dict[str, Any]] = list(responses)
        self.call_log: list[tuple[str, str]] = []

    def __call__(self, req: Any, timeout: int = 30) -> Any:
        del timeout
        self.call_log.append((req.method, req.full_url))
        payload = self._queue.pop(0) if self._queue else {}

        class _Resp:
            def __init__(self, data: dict[str, Any]) -> None:
                self._data = data

            def read(self) -> bytes:
                return json.dumps(self._data).encode()

            def __enter__(self) -> _Resp:
                return self

            def __exit__(self, *args: Any) -> None:
                return None

        return _Resp(payload)


def test_run_creates_snapshot_when_env_set(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    del clean_env
    monkeypatch.setenv("HETZNER_API_TOKEN", "test-token")
    monkeypatch.setenv("HETZNER_SERVER_ID", "12345")

    fake = _FakeUrlopen(
        [
            {"action": {"id": 999}},  # create_image
            {"images": []},  # list_images
        ]
    )
    monkeypatch.setattr("urllib.request.urlopen", fake)

    rc = sh.run()
    assert rc == 0

    # 2 API calls: create_image + list_images. No delete because
    # the fake returned 0 images.
    assert len(fake.call_log) == 2
    methods = [m for m, _ in fake.call_log]
    assert methods == ["POST", "GET"]


def test_run_prunes_snapshots_beyond_keep_threshold(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    del clean_env
    monkeypatch.setenv("HETZNER_API_TOKEN", "test-token")
    monkeypatch.setenv("HETZNER_SERVER_ID", "12345")

    # 10 snapshot images; KEEP=7 → prune 3.
    images: list[dict[str, Any]] = [
        {"id": i, "labels": {"source": "aegis-auto"}} for i in range(10)
    ]
    responses: list[dict[str, Any]] = [
        {"action": {"id": 999}},
        {"images": images},
    ]
    responses.extend([{}] * 3)  # 3 DELETE responses

    fake = _FakeUrlopen(responses)
    monkeypatch.setattr("urllib.request.urlopen", fake)

    rc = sh.run()
    assert rc == 0

    methods = [m for m, _ in fake.call_log]
    delete_count = sum(1 for m in methods if m == "DELETE")
    assert delete_count == 3
