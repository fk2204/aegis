"""POST /ui/funders/{funder_id}/reextract — one-click re-extract from a new PDF.

Covers:
  * happy path replaces structured fields, preserves id/name/active
  * legacy `notes` prose atomically migrates to `notes_residual`
  * no migration when residual is already populated
  * contact fields preserved per-field when extraction returns empty
  * audit row recorded
  * 404 for unknown funder
  * redirect-with-error on PDF / extraction failure
  * modal-fragment route renders the upload form
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
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
from aegis.funders.models import FunderRow, FunderTier
from aegis.funders.repository import InMemoryFunderRepository


class _ExtractionStubLLM:
    """Returns a canned LLM response for funder extraction tests."""

    def __init__(self, draft_overrides: dict[str, object] | None = None) -> None:
        self._overrides = draft_overrides or {}

    def extract_raw_json(
        self, pdf_bytes: bytes, prompt: str
    ) -> tuple[dict[str, object], bool]:
        _ = (pdf_bytes, prompt)
        draft: dict[str, object] = {
            "name": "Will Be Overwritten Capital",
            "contact_name": "New Rep",
            "contact_phone": "555-NEW-EXTR",
            "contact_email": "new@funder.com",
            "submission_email": "submit@funder.com",
            "min_monthly_revenue": 75000,
            "min_credit_score": 650,
            "accepts_stacking": False,
            "excluded_industries": ["cannabis"],
            "excluded_states": ["NV"],
            "tiers": [
                {
                    "name": "Premium",
                    "buy_rate_low": 1.25,
                    "buy_rate_high": 1.30,
                    "min_credit_score": 700,
                    "min_monthly_revenue": 100000,
                    "max_advance": 2000000,
                    "max_holdback": 0.15,
                },
                {
                    "name": "Standard",
                    "buy_rate_low": 1.32,
                    "buy_rate_high": 1.40,
                    "min_credit_score": 620,
                    "min_monthly_revenue": 50000,
                    "max_advance": 500000,
                    "max_holdback": 0.20,
                },
            ],
            "auto_decline_conditions": ["Active bankruptcy"],
            "conditional_requirements": ["Trucking: MVR required"],
            "notes_residual": "Same-day funding available.",
        }
        draft.update(self._overrides)
        return (
            {
                "draft": draft,
                "confidence_by_field": {"min_monthly_revenue": 90},
                "unparseable_fragments": [],
                "overall_confidence": 88,
            },
            False,
        )

    def extract_raw_json_from_images(
        self, page_images_png: list[bytes], prompt: str
    ) -> tuple[dict[str, object], bool]:
        raise NotImplementedError

    def classify_batch_json(self, prompt: str) -> dict[str, object]:
        raise NotImplementedError


@pytest.fixture
def existing_funder() -> FunderRow:
    return FunderRow(
        name="Logic Advance Group",
        active=True,
        min_monthly_revenue=Decimal("30000"),
        min_credit_score=600,
        accepts_stacking=False,
        contact_name="Old Rep",
        contact_phone="555-OLD",
        contact_email="old@logicadvance.com",
        submission_email="iso-old@logicadvance.com",
        tiers=(FunderTier(name="LegacyTier"),),
        notes="Legacy notes prose from before step C re-extract.",
    )


@pytest.fixture
def funder_repo(existing_funder: FunderRow) -> InMemoryFunderRepository:
    repo = InMemoryFunderRepository()
    repo.upsert(existing_funder)
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
    app.dependency_overrides[get_llm] = lambda: _ExtractionStubLLM()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def _post_pdf(client: TestClient, funder_id: object, content: bytes = b"%PDF-1.4\n%%EOF\n") -> object:
    return client.post(
        f"/ui/funders/{funder_id}/reextract",
        files={"pdf": ("guidelines.pdf", content, "application/pdf")},
        follow_redirects=False,
    )


# --- modal fragment ---------------------------------------------------------


def test_reextract_modal_renders(
    client: TestClient, existing_funder: FunderRow
) -> None:
    resp = client.get(f"/ui/funders/{existing_funder.id}/reextract-modal")
    assert resp.status_code == 200
    body = resp.text
    assert "Re-extract" in body
    assert existing_funder.name in body
    assert 'enctype="multipart/form-data"' in body
    assert f'/ui/funders/{existing_funder.id}/reextract' in body


def test_reextract_modal_404_for_unknown_funder(client: TestClient) -> None:
    resp = client.get(f"/ui/funders/{uuid4()}/reextract-modal")
    assert resp.status_code == 404


# --- POST happy path --------------------------------------------------------


def test_reextract_replaces_structured_fields(
    client: TestClient,
    existing_funder: FunderRow,
    funder_repo: InMemoryFunderRepository,
) -> None:
    resp = _post_pdf(client, existing_funder.id)
    assert resp.status_code == 303
    assert "reextracted=1" in resp.headers["location"]

    after = funder_repo.get(existing_funder.id)
    # Extracted structured fields land on the funder.
    assert after.min_monthly_revenue == Decimal("75000")
    assert after.min_credit_score == 650
    assert "cannabis" in after.excluded_industries
    assert "NV" in after.excluded_states
    # Tiers wholesale replaced.
    assert len(after.tiers) == 2
    assert {t.name for t in after.tiers} == {"Premium", "Standard"}
    assert after.tiers[0].max_holdback == Decimal("0.15")
    # Conditions land.
    assert "Active bankruptcy" in after.auto_decline_conditions
    assert "Trucking: MVR required" in after.conditional_requirements


def test_reextract_preserves_id_name_active(
    client: TestClient,
    existing_funder: FunderRow,
    funder_repo: InMemoryFunderRepository,
) -> None:
    """Admin metadata is preserved even though the LLM returned a
    different name in its extraction draft."""
    _post_pdf(client, existing_funder.id)
    after = funder_repo.get(existing_funder.id)
    assert after.id == existing_funder.id
    assert after.name == "Logic Advance Group"  # NOT the LLM's name
    assert after.active is True


def test_reextract_migrates_legacy_notes_to_residual(
    client: TestClient,
    existing_funder: FunderRow,
    funder_repo: InMemoryFunderRepository,
) -> None:
    """Pre-step-C funders have prose in `notes`; re-extract moves it
    into `notes_residual` so the new field semantics hold."""
    _post_pdf(client, existing_funder.id)
    after = funder_repo.get(existing_funder.id)
    # Legacy notes cleared.
    assert after.notes == ""
    # Migrated into residual, then new extraction's residual appended.
    assert "Legacy notes prose" in after.notes_residual
    assert "Same-day funding" in after.notes_residual


def test_reextract_no_migration_when_residual_already_populated(
    client: TestClient,
    funder_repo: InMemoryFunderRepository,
) -> None:
    """If notes_residual is already non-empty, the extractor's residual
    replaces it wholesale (no legacy-migration path needed)."""
    f = FunderRow(
        name="Already Migrated",
        notes="",
        notes_residual="Operator already cleaned this up.",
    )
    funder_repo.upsert(f)
    _post_pdf(client, f.id)
    after = funder_repo.get(f.id)
    assert after.notes == ""
    # New extraction's residual wins; old residual is replaced.
    assert after.notes_residual == "Same-day funding available."


def test_reextract_preserves_contact_when_extraction_empty(
    client: TestClient,
    funder_repo: InMemoryFunderRepository,
) -> None:
    """Contact preservation rule — re-extracting against a PDF without
    a contact block should not blank a manually-set rep."""
    f = FunderRow(
        name="Contactful Capital",
        contact_name="Jim",
        contact_phone="555-JIM",
        contact_email="jim@cap.com",
        submission_email="iso@cap.com",
    )
    funder_repo.upsert(f)

    # Override the stub to return EMPTY contact fields.
    app = cast(FastAPI, client.app)
    app.dependency_overrides[get_llm] = lambda: _ExtractionStubLLM(
        draft_overrides={
            "contact_name": "",
            "contact_phone": "",
            "contact_email": "",
            "submission_email": "",
        }
    )
    _post_pdf(client, f.id)
    after = funder_repo.get(f.id)
    # All four contact fields preserved.
    assert after.contact_name == "Jim"
    assert after.contact_phone == "555-JIM"
    assert after.contact_email == "jim@cap.com"
    assert after.submission_email == "iso@cap.com"


def test_reextract_audit_row_recorded(
    client: TestClient,
    existing_funder: FunderRow,
    audit_log: InMemoryAuditLog,
) -> None:
    _post_pdf(client, existing_funder.id)
    rows = [e for e in audit_log.entries if e["action"] == "funder.reextracted"]
    assert len(rows) == 1
    details = rows[0]["details"]
    assert details["funder_name"] == existing_funder.name
    assert details["notes_migrated_to_residual"] is True
    assert details["tier_count_before"] == 1
    assert details["tier_count_after"] == 2
    assert details["overall_confidence"] == 88
    # sha hashes are 64-char hex.
    assert isinstance(details["new_pdf_sha256"], str)
    assert len(details["new_pdf_sha256"]) == 64


# --- failure paths ----------------------------------------------------------


def test_reextract_404_for_unknown_funder(client: TestClient) -> None:
    resp = _post_pdf(client, uuid4())
    assert resp.status_code == 404


def test_reextract_redirects_with_error_on_empty_pdf(
    client: TestClient, existing_funder: FunderRow
) -> None:
    resp = _post_pdf(client, existing_funder.id, content=b"")
    assert resp.status_code == 303
    assert "reextract_error=" in resp.headers["location"]
    assert "empty" in resp.headers["location"].lower()


def test_reextract_redirects_with_error_on_extraction_failure(
    client: TestClient,
    existing_funder: FunderRow,
    funder_repo: InMemoryFunderRepository,
) -> None:
    """An LLM stub that returns a payload missing the 'draft' key
    triggers FunderExtractionError; route should 303 to funder detail
    with the error in query params, NOT replace the funder."""
    class _BadStub:
        def extract_raw_json(
            self, pdf_bytes: bytes, prompt: str
        ) -> tuple[dict[str, object], bool]:
            return ({"confidence_by_field": {}}, False)

        def extract_raw_json_from_images(
            self, page_images_png: list[bytes], prompt: str
        ) -> tuple[dict[str, object], bool]:
            raise NotImplementedError

        def classify_batch_json(self, prompt: str) -> dict[str, object]:
            raise NotImplementedError

    app = cast(FastAPI, client.app)
    app.dependency_overrides[get_llm] = lambda: _BadStub()
    resp = _post_pdf(client, existing_funder.id)
    assert resp.status_code == 303
    assert "reextract_error=" in resp.headers["location"]
    # Funder unchanged.
    after = funder_repo.get(existing_funder.id)
    assert after.min_monthly_revenue == existing_funder.min_monthly_revenue
    assert after.notes == existing_funder.notes
