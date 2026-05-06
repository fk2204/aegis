"""Smoke test: app boots, /healthz returns ok, and the boot guard rejects bad config."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import app
from aegis.config import DataResidencyError, Settings, get_settings


def test_healthz_returns_ok() -> None:
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_boot_guard_rejects_unconfirmed_residency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEGIS_DATA_RESIDENCY_CONFIRMED", "false")
    get_settings.cache_clear()
    with pytest.raises(DataResidencyError, match="AEGIS_DATA_RESIDENCY_CONFIRMED"):
        get_settings()
    get_settings.cache_clear()


def test_boot_guard_rejects_non_us_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-4-6")
    with pytest.raises(DataResidencyError, match="us\\."):
        Settings()
