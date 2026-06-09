"""U22 — dossier render of merchant-level shadow signals (migration 044).

Two concerns covered:

  1. A merchant with persisted ``merchants_shadow_signals`` rows renders
     the new "Merchant-level shadow signals" section with the U18-
     humanized titles ("Duplicate PDF upload", "Related account
     suspected") — not the raw codes. Confirms the route runs each
     persisted row through the existing ``humanize_flag`` registry per
     ``_humanize_merchant_shadow_signals``.

  2. A merchant with no persisted rows renders WITHOUT the section.
     The summary substring "Merchant-level shadow signals" must not
     appear on a clean dossier.

The dossier route is reached via the in-memory MerchantRepository +
DocumentRepository + MerchantShadowSignalRepository wired through
``app.dependency_overrides``. The route is internal-only behind
Cloudflare Access in production, so no bearer token is needed in tests
— same convention as ``test_renewals_route``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_merchant_repository,
    get_merchant_shadow_signal_repository,
    get_repository,
    reset_dependency_caches,
)
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.merchants.shadow_signals import (
    InMemoryMerchantShadowSignalRepository,
)
from aegis.storage import InMemoryDocumentRepository

# ---------------------------------------------------------------------------
# Fixtures


def _merchant() -> MerchantRow:
    """A finalized merchant — minimum fields needed for the dossier
    render. The dossier handles missing analyses / documents gracefully
    by routing through the "Score unavailable" branch, which still
    emits the shadow-signals section we're testing."""
    return MerchantRow(
        id=uuid4(),
        business_name="Acme LLC",
        state="NY",
        is_renewal=False,
    )


@pytest.fixture
def merchants() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture
def docs() -> InMemoryDocumentRepository:
    return InMemoryDocumentRepository()


@pytest.fixture
def shadow_signals() -> InMemoryMerchantShadowSignalRepository:
    return InMemoryMerchantShadowSignalRepository()


@pytest.fixture
def client(
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    shadow_signals: InMemoryMerchantShadowSignalRepository,
) -> Iterator[TestClient]:
    """TestClient whose repository deps are pinned to the in-memory
    instances above so every request in the test hits the same state.

    Mirrors ``tests/web/test_renewals_route.py::client_with_renewals``.
    """
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: merchants
    app.dependency_overrides[get_repository] = lambda: docs
    app.dependency_overrides[get_merchant_shadow_signal_repository] = (
        lambda: shadow_signals
    )
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


# ---------------------------------------------------------------------------
# Populated merchant — section renders with humanized titles


def test_dossier_renders_merchant_shadow_signals_with_humanized_titles(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    shadow_signals: InMemoryMerchantShadowSignalRepository,
) -> None:
    """A merchant with persisted shadow-signal rows renders the new
    "Merchant-level shadow signals" section with the U18-humanized
    titles. The raw code is also present via ``data-code`` for the
    audit CSV, but the operator-facing title is the humanized form.
    """
    merchant = _merchant()
    merchants.upsert(merchant)

    # Two signals — one of each U12 code.
    shadow_signals.record(
        merchant_id=merchant.id,
        signal_code="duplicate_pdf_upload",
        signal_severity=0,
        detail="sha256_match_with_doc=abc:uploaded=2026-06-08T14:32:00+00:00",
        source_document_id=uuid4(),
        source_ids=[uuid4()],
        metadata={"emitted_by": "cross_statement_detector"},
        detected_by="worker",
        detected_at=datetime(2026, 6, 8, 14, 32, tzinfo=UTC),
    )
    shadow_signals.record(
        merchant_id=merchant.id,
        signal_code="related_account_suspected",
        signal_severity=0,
        detail="holder=Acme LLC:existing_last4=9999:new_last4=1234",
        source_document_id=uuid4(),
        source_ids=[uuid4()],
        metadata={"emitted_by": "cross_statement_detector"},
        detected_by="worker",
        detected_at=datetime(2026, 6, 8, 15, 0, tzinfo=UTC),
    )

    resp = client.get(f"/ui/merchants/{merchant.id}")
    assert resp.status_code == 200, resp.text
    body = resp.text

    # The section heading is present.
    assert "Merchant-level shadow signals" in body

    # U18-humanized titles, NOT the raw codes-as-titles.
    assert "Duplicate PDF upload" in body
    assert "Related account suspected" in body

    # Raw codes still attached as data-code for the audit CSV.
    assert 'data-code="duplicate_pdf_upload"' in body
    assert 'data-code="related_account_suspected"' in body

    # Persisted detected_at timestamp is rendered.
    assert "2026-06-08" in body


def test_dossier_section_carries_data_signal_id_attribute(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    shadow_signals: InMemoryMerchantShadowSignalRepository,
) -> None:
    """The U22 section emits ``data-signal-id`` so an operator audit
    drill-down can map a rendered ``<li>`` back to its
    ``merchants_shadow_signals`` row."""
    merchant = _merchant()
    merchants.upsert(merchant)

    rec = shadow_signals.record(
        merchant_id=merchant.id,
        signal_code="duplicate_pdf_upload",
        signal_severity=0,
        detail="d",
        source_document_id=uuid4(),
        source_ids=[uuid4()],
        metadata={"emitted_by": "cross_statement_detector"},
        detected_by="worker",
    )

    resp = client.get(f"/ui/merchants/{merchant.id}")
    assert resp.status_code == 200, resp.text
    assert f'data-signal-id="{rec.id}"' in resp.text


# ---------------------------------------------------------------------------
# Empty merchant — no section


def test_dossier_without_shadow_signals_omits_section(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
) -> None:
    """A merchant with no persisted shadow-signal rows must NOT render
    the section heading. The ``{% if merchant_shadow_signals %}`` guard
    keeps the section out of the way of hard-decline reasoning on
    clean dossiers.
    """
    merchant = _merchant()
    merchants.upsert(merchant)

    resp = client.get(f"/ui/merchants/{merchant.id}")
    assert resp.status_code == 200, resp.text
    assert "Merchant-level shadow signals" not in resp.text
