"""POST /ui/funders/{funder_id}/operator-notes — operator-authored
commentary save route.

Verifies:
  * happy save updates operator_notes + writes audit row
  * empty save (clearing) writes audit row with cleared=True
  * no-change save does NOT write audit row but still re-renders form
  * 404 for unknown funder
  * soft cap on length truncates silently
  * operator_notes survives a subsequent re-extract
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_funder_repository,
    get_llm,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.funders.models import FunderRow
from aegis.funders.repository import InMemoryFunderRepository


@pytest.fixture
def funder() -> FunderRow:
    return FunderRow(name="Note Capital", active=True)


@pytest.fixture
def funder_repo(funder: FunderRow) -> InMemoryFunderRepository:
    repo = InMemoryFunderRepository()
    repo.upsert(funder)
    return repo


@pytest.fixture
def audit_log() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def client(
    funder_repo: InMemoryFunderRepository,
    audit_log: InMemoryAuditLog,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_funder_repository] = lambda: funder_repo
    app.dependency_overrides[get_audit] = lambda: audit_log
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def test_save_updates_operator_notes_and_writes_audit(
    client: TestClient,
    funder: FunderRow,
    funder_repo: InMemoryFunderRepository,
    audit_log: InMemoryAuditLog,
) -> None:
    resp = client.post(
        f"/ui/funders/{funder.id}/operator-notes",
        data={"operator_notes": "Erik prefers ACH on funding day, not lockbox."},
    )
    assert resp.status_code == 200
    # Returned HTML fragment includes the new content + Saved indicator.
    assert "Erik prefers ACH" in resp.text
    assert "✓ Saved" in resp.text

    after = funder_repo.get(funder.id)
    assert after.operator_notes == "Erik prefers ACH on funding day, not lockbox."

    rows = [e for e in audit_log.entries if e["action"] == "funder.operator_notes_updated"]
    assert len(rows) == 1
    d = rows[0]["details"]
    assert d["funder_name"] == "Note Capital"
    assert d["before_length"] == 0
    assert d["after_length"] == len("Erik prefers ACH on funding day, not lockbox.")
    assert d["cleared"] is False


def test_clearing_operator_notes_writes_audit_with_cleared_flag(
    client: TestClient,
    funder: FunderRow,
    funder_repo: InMemoryFunderRepository,
    audit_log: InMemoryAuditLog,
) -> None:
    funder_repo.upsert(funder.model_copy(update={"operator_notes": "old note"}))
    resp = client.post(
        f"/ui/funders/{funder.id}/operator-notes",
        data={"operator_notes": ""},
    )
    assert resp.status_code == 200

    after = funder_repo.get(funder.id)
    assert after.operator_notes == ""

    rows = [e for e in audit_log.entries if e["action"] == "funder.operator_notes_updated"]
    assert len(rows) == 1
    d = rows[0]["details"]
    assert d["cleared"] is True
    assert d["before_length"] == len("old note")
    assert d["after_length"] == 0


def test_no_change_save_does_not_write_audit_row(
    client: TestClient,
    funder: FunderRow,
    funder_repo: InMemoryFunderRepository,
    audit_log: InMemoryAuditLog,
) -> None:
    """Operator clicks Save without editing → no audit churn."""
    funder_repo.upsert(funder.model_copy(update={"operator_notes": "stable note"}))
    resp = client.post(
        f"/ui/funders/{funder.id}/operator-notes",
        data={"operator_notes": "stable note"},
    )
    assert resp.status_code == 200
    rows = [e for e in audit_log.entries if e["action"] == "funder.operator_notes_updated"]
    assert rows == []


def test_save_404_for_unknown_funder(client: TestClient) -> None:
    resp = client.post(
        f"/ui/funders/{uuid4()}/operator-notes",
        data={"operator_notes": "anything"},
    )
    assert resp.status_code == 404


def test_save_soft_caps_at_10k_chars(
    client: TestClient,
    funder: FunderRow,
    funder_repo: InMemoryFunderRepository,
) -> None:
    """Operator pastes a huge blob → silently truncated to 10K chars."""
    huge = "x" * 12_000
    resp = client.post(
        f"/ui/funders/{funder.id}/operator-notes",
        data={"operator_notes": huge},
    )
    assert resp.status_code == 200
    after = funder_repo.get(funder.id)
    assert len(after.operator_notes) == 10_000


def test_operator_notes_survives_reextract(
    client: TestClient,
    funder: FunderRow,
    funder_repo: InMemoryFunderRepository,
) -> None:
    """Re-extract must NEVER touch operator_notes. The contract that
    makes operator_notes durable across re-extractions is what
    distinguishes it from notes_residual."""
    # Operator sets a note.
    funder_repo.upsert(funder.model_copy(update={"operator_notes": "long-lived rep contact info"}))

    # Stub LLM that returns a minimal extraction (no operator_notes
    # field in the schema; even if it did, the route should ignore it).
    class _StubLLM:
        def extract_raw_json(
            self, pdf_bytes: bytes, prompt: str
        ) -> tuple[dict[str, object], bool]:
            return (
                {
                    "draft": {"name": "Will Be Ignored", "accepts_stacking": False},
                    "confidence_by_field": {},
                    "unparseable_fragments": [],
                    "overall_confidence": 50,
                },
                False,
            )

        def extract_raw_json_from_images(
            self, page_images_png: list[bytes], prompt: str
        ) -> tuple[dict[str, object], bool]:
            raise NotImplementedError

        def classify_batch_json(self, prompt: str) -> dict[str, object]:
            raise NotImplementedError

    app = cast(FastAPI, client.app)
    app.dependency_overrides[get_llm] = lambda: _StubLLM()

    resp = client.post(
        f"/ui/funders/{funder.id}/reextract",
        files={"pdf": ("guidelines.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # Funder is re-saved, but operator_notes is untouched.
    after = funder_repo.get(funder.id)
    assert after.operator_notes == "long-lived rep contact info"
