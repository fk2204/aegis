"""Close-specific Settings tests.

Currently covers:

* CSV → tuple parsing for ``close_attachment_filename_filters`` (the
  ``_split_csv_filename_filters`` field_validator). The validator lets
  operators set the env var as a friendly comma-separated string
  (``CLOSE_ATTACHMENT_FILENAME_FILTERS=statement,estmt``) instead of
  JSON-encoded list.
* Cap defaults match the chunk-2 proposal (warn at 10, hard cap at 15).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from aegis.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_filename_filters_default_set() -> None:
    s = Settings(aegis_data_residency_confirmed=True)
    assert s.close_attachment_filename_filters == (
        "statement", "estmt", "stmt", "bank",
    )


def test_filename_filters_csv_env_parses_to_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "CLOSE_ATTACHMENT_FILENAME_FILTERS",
        "statement, estmt ,stmt , bank, monthly",
    )
    s = Settings(aegis_data_residency_confirmed=True)
    assert s.close_attachment_filename_filters == (
        "statement", "estmt", "stmt", "bank", "monthly",
    )


def test_filename_filters_csv_strips_empty_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "CLOSE_ATTACHMENT_FILENAME_FILTERS", "statement,,bank,,,",
    )
    s = Settings(aegis_data_residency_confirmed=True)
    assert s.close_attachment_filename_filters == ("statement", "bank")


def test_cap_defaults_match_chunk2_proposal() -> None:
    s = Settings(aegis_data_residency_confirmed=True)
    assert s.close_attachment_warn_threshold == 10
    assert s.close_attachment_hard_cap == 15
