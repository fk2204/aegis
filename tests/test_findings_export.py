"""JSON + CSV findings export — round-trip and EIN-exclusion checks.

The findings export is the operator's "extract findings" surface for
funder communication. The tests pin three things:

  1. JSON shape matches ``MerchantFindings`` (Pydantic strict).
  2. EIN never appears in either output, even when set on the merchant.
  3. CSV download has the expected sections + content-disposition.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_funder_repository,
    get_merchant_repository,
    get_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.config import get_settings
from aegis.funders.repository import InMemoryFunderRepository
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.storage import InMemoryDocumentRepository
from tests.test_storage import _make_pipeline_result


@pytest.fixture
def merchant_with_intake() -> MerchantRow:
    return MerchantRow(
        business_name="Acme Bakery LLC",
        dba="Acme",
        owner_name="Jane Doe",
        state="CA",
        entity_type="llc",
        ein="12-3456789",  # PII — must NEVER appear in exports
        requested_amount=Decimal("50000"),
        requested_factor=Decimal("1.30"),
        requested_term_days=120,
        broker_source="Test Broker",
        intake_date=date(2026, 5, 1),
        is_renewal=False,
        credit_score=720,
        time_in_business_months=36,
    )


@pytest.fixture
def merchant_repo(merchant_with_intake: MerchantRow) -> InMemoryMerchantRepository:
    repo = InMemoryMerchantRepository()
    repo.upsert(merchant_with_intake)
    return repo


@pytest.fixture
def doc_repo(merchant_with_intake: MerchantRow) -> InMemoryDocumentRepository:
    repo = InMemoryDocumentRepository()
    row = repo.create_document(
        file_hash="f" * 64, byte_size=2048, original_filename="x.pdf"
    )
    row = row.model_copy(update={"merchant_id": merchant_with_intake.id})
    repo._docs[row.id] = row
    repo.persist_parse_result(
        row.id, result=_make_pipeline_result(), merchant_id=merchant_with_intake.id
    )
    return repo


@pytest.fixture
def client(
    merchant_repo: InMemoryMerchantRepository,
    doc_repo: InMemoryDocumentRepository,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: merchant_repo
    app.dependency_overrides[get_funder_repository] = lambda: InMemoryFunderRepository()
    app.dependency_overrides[get_repository] = lambda: doc_repo
    app.dependency_overrides[get_audit] = lambda: InMemoryAuditLog()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def _bearer() -> dict[str, str]:
    """Pull the configured bearer token for the JSON API."""
    settings = get_settings()
    token = settings.api_bearer_token
    assert token is not None, "tests need API_BEARER_TOKEN configured in .env"
    return {"Authorization": f"Bearer {token.get_secret_value()}"}


def test_findings_json_returns_expected_shape(
    client: TestClient, merchant_with_intake: MerchantRow
) -> None:
    resp = client.get(
        f"/merchants/{merchant_with_intake.id}/findings", headers=_bearer()
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["generator_version"] == "findings-v1"
    assert body["merchant"]["business_name"] == "Acme Bakery LLC"
    assert body["merchant"]["entity_type"] == "llc"
    assert body["compliance"]["state_tier"] == 1  # CA is Tier 1
    assert isinstance(body["documents"], list) and len(body["documents"]) == 1


def test_findings_json_excludes_ein(
    client: TestClient, merchant_with_intake: MerchantRow
) -> None:
    """EIN is PII and must NEVER appear in any export, even when set."""
    resp = client.get(
        f"/merchants/{merchant_with_intake.id}/findings", headers=_bearer()
    )
    assert resp.status_code == 200
    raw = resp.text
    assert "12-3456789" not in raw
    assert "ein" not in resp.json()["merchant"]


def test_findings_csv_downloads_with_attachment_header(
    client: TestClient, merchant_with_intake: MerchantRow
) -> None:
    resp = client.get(f"/ui/merchants/{merchant_with_intake.id}/findings.csv")
    assert resp.status_code == 200
    cd = resp.headers["content-disposition"]
    assert "attachment" in cd
    assert "findings_acme_bakery_llc.csv" in cd
    body = resp.text
    assert "merchant,business_name,Acme Bakery LLC" in body
    assert "compliance,state_tier,1" in body
    # EIN must NOT be in the CSV output.
    assert "12-3456789" not in body


def test_findings_csv_404_for_unknown_merchant(client: TestClient) -> None:
    resp = client.get("/ui/merchants/00000000-0000-0000-0000-000000000000/findings.csv")
    assert resp.status_code == 404


def test_findings_json_404_for_unknown_merchant(client: TestClient) -> None:
    resp = client.get(
        "/merchants/00000000-0000-0000-0000-000000000000/findings",
        headers=_bearer(),
    )
    assert resp.status_code == 404
