"""Tests for the /ui/bank-layouts list + hints-update flow.

Covers:
  * ``GET /ui/bank-layouts`` returns 200 and renders every row, with
    an empty-state copy when no rows exist.
  * ``POST /ui/bank-layouts/{bank_name}/hints`` with an unseen bank
    creates a primed row (successful_parses stays 0) and writes the
    ``bank_layouts.hints_updated`` audit row.
  * ``POST .../hints`` with an existing bank updates only the hints
    column; ``successful_parses`` and ``last_seen`` are unchanged.
  * Empty ``hints`` form value clears the column.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_bank_layout_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.bank_layouts import InMemoryBankLayoutRepository
from aegis.ops.operators import CF_ACCESS_EMAIL_HEADER


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def repo() -> InMemoryBankLayoutRepository:
    return InMemoryBankLayoutRepository()


@pytest.fixture
def client(
    audit: InMemoryAuditLog,
    repo: InMemoryBankLayoutRepository,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_bank_layout_repository] = lambda: repo
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def test_list_empty_state_renders(
    client: TestClient,
) -> None:
    resp = client.get("/ui/bank-layouts")
    assert resp.status_code == 200
    assert "No bank layouts on file yet" in resp.text


def test_list_renders_all_rows(
    client: TestClient,
    repo: InMemoryBankLayoutRepository,
) -> None:
    repo.upsert_success(bank_name="Chase", fingerprint={"k": 1})
    repo.upsert_success(bank_name="Bank of America", fingerprint={"k": 2})
    resp = client.get("/ui/bank-layouts")
    assert resp.status_code == 200
    assert "Chase" in resp.text
    assert "Bank of America" in resp.text


def test_post_hints_creates_primed_row(
    client: TestClient,
    repo: InMemoryBankLayoutRepository,
    audit: InMemoryAuditLog,
) -> None:
    resp = client.post(
        "/ui/bank-layouts/Bank%20of%20America/hints",
        data={"hints": "Multi-column layout; description column wraps."},
        headers={CF_ACCESS_EMAIL_HEADER: "filip@commerafunding.com"},
    )
    assert resp.status_code == 200
    assert "Bank of America" in resp.text
    row = repo.find_by_bank_name("Bank of America")
    assert row is not None
    assert row.successful_parses == 0
    assert row.extraction_hints == "Multi-column layout; description column wraps."

    actions = [e["action"] for e in audit.entries]
    assert "bank_layouts.hints_updated" in actions
    added = next(e for e in audit.entries if e["action"] == "bank_layouts.hints_updated")
    assert added["actor_email"] == "filip@commerafunding.com"
    # Audit details deliberately exclude PII — bank_name is in the
    # logger's PII-key set. ``subject_id`` references the row UUID for
    # join-back; ``hints_chars`` is the only detail key.
    assert added["details"]["hints_chars"] == len("Multi-column layout; description column wraps.")
    assert added["subject_type"] == "bank_layout"
    assert added["subject_id"] == str(row.id)


def test_post_hints_updates_existing_row_without_touching_parse_count(
    client: TestClient,
    repo: InMemoryBankLayoutRepository,
) -> None:
    for _ in range(5):
        repo.upsert_success(bank_name="Chase", fingerprint={"k": 1})
    before = repo.find_by_bank_name("Chase")
    assert before is not None
    before_parses = before.successful_parses
    before_last_seen = before.last_seen

    resp = client.post(
        "/ui/bank-layouts/Chase/hints",
        data={"hints": "Two-line bank header."},
    )
    assert resp.status_code == 200
    after = repo.find_by_bank_name("Chase")
    assert after is not None
    assert after.extraction_hints == "Two-line bank header."
    assert after.successful_parses == before_parses
    assert after.last_seen == before_last_seen


def test_post_empty_hints_clears_existing_hints(
    client: TestClient,
    repo: InMemoryBankLayoutRepository,
) -> None:
    repo.set_hints(bank_name="Chase", hints="initial text")
    resp = client.post(
        "/ui/bank-layouts/Chase/hints",
        data={"hints": ""},
    )
    assert resp.status_code == 200
    row = repo.find_by_bank_name("Chase")
    assert row is not None
    assert row.extraction_hints is None
