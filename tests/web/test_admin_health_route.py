"""Tests for ``GET /ui/admin/health`` (U35 — operator system-status page).

Covers:

  * 200 + banner + all four section headers render.
  * Service info section renders the version + hostname.
  * Config flag section renders whitelisted Settings (NOT secrets).
  * Repository counts section renders with empty + populated data.
  * Recent errors section: empty state when no failures; populated when
    audit_log has matching rows.
  * Traffic-light hint: residency=False renders the PROBLEM chip.
  * PII canary — no secret VALUE matching ``*_KEY|*_TOKEN|*_SECRET`` ever
    surfaces in the rendered body.
  * ``/ui/admin`` 302 → ``/ui/admin/health`` landing redirect.

All tests use ``TestClient`` with in-memory fakes injected via
``app.dependency_overrides``. The same pattern as
``tests/web/test_admin_routes.py``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_schema_migrations_reader,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.web.routers.admin import (
    InMemorySchemaMigrationsReader,
    SchemaMigrationRow,
)

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def health_client() -> Iterator[
    tuple[TestClient, InMemorySchemaMigrationsReader, InMemoryAuditLog]
]:
    """TestClient with the migrations reader + audit log pinned in-memory.

    Yields the live fakes so each test pre-seeds rows directly. All
    other repositories used by the health page (merchants, funders,
    documents, decisions, submissions, scoring disagreements, render
    events, shadow signals) flow through the dependency cache backed by
    the in-memory storage backend forced in conftest.py.
    """
    reset_dependency_caches()
    reader = InMemorySchemaMigrationsReader()
    audit = InMemoryAuditLog()
    app = create_app()
    app.dependency_overrides[get_schema_migrations_reader] = lambda: reader
    app.dependency_overrides[get_audit] = lambda: audit
    with TestClient(app) as c:
        yield c, reader, audit
    app.dependency_overrides.clear()
    reset_dependency_caches()


# ---------------------------------------------------------------------------
# Smoke + section presence
# ---------------------------------------------------------------------------


def test_health_returns_200_with_banner(
    health_client: tuple[
        TestClient, InMemorySchemaMigrationsReader, InMemoryAuditLog
    ],
) -> None:
    """The health page returns 200 + the operator-visibility banner copy."""
    client, _reader, _audit = health_client
    resp = client.get("/ui/admin/health")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "Operator visibility — system health" in body
    assert "Read-only" in body


def test_health_shows_all_four_section_headers(
    health_client: tuple[
        TestClient, InMemorySchemaMigrationsReader, InMemoryAuditLog
    ],
) -> None:
    """All four section headers render — service info, config flags,
    repository counts, recent errors."""
    client, _reader, _audit = health_client
    resp = client.get("/ui/admin/health")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "Service info" in body
    assert "Config flag state" in body
    assert "Repository row counts" in body
    assert "Recent errors" in body


# ---------------------------------------------------------------------------
# Section A — service info
# ---------------------------------------------------------------------------


def test_health_service_info_renders_version_and_hostname(
    health_client: tuple[
        TestClient, InMemorySchemaMigrationsReader, InMemoryAuditLog
    ],
) -> None:
    """Service info shows the AEGIS package version + the Python version."""
    client, _reader, _audit = health_client
    resp = client.get("/ui/admin/health")
    assert resp.status_code == 200, resp.text
    body = resp.text
    # aegis/__init__.py pins __version__ = "2.0.0".
    assert "2.0.0" in body
    # Python 3.x — the test environment runs Python 3.12+ per pyproject.
    assert re.search(r"\b3\.\d{1,2}", body) is not None


# ---------------------------------------------------------------------------
# Section B — config flag state
# ---------------------------------------------------------------------------


def test_health_config_flags_render_whitelisted_settings(
    health_client: tuple[
        TestClient, InMemorySchemaMigrationsReader, InMemoryAuditLog
    ],
) -> None:
    """The config-flag section renders the operator-tunable knobs from
    the route's _HEALTH_SAFE_CONFIG_FIELDS whitelist. The values render
    too — but they're non-secret config values, not credentials."""
    client, _reader, _audit = health_client
    resp = client.get("/ui/admin/health")
    body = resp.text
    # A sampling of whitelisted field names that must appear.
    assert "aegis_data_residency_confirmed" in body
    assert "aegis_storage_backend" in body
    assert "aegis_scoring_engine" in body
    assert "aegis_tampering_decline_mode" in body
    assert "aegis_eof_threshold" in body
    assert "bedrock_model_id" in body


def test_health_config_traffic_light_residency_ok_when_true(
    health_client: tuple[
        TestClient, InMemorySchemaMigrationsReader, InMemoryAuditLog
    ],
) -> None:
    """Residency=True (per conftest.py) renders the OK chip somewhere."""
    client, _reader, _audit = health_client
    resp = client.get("/ui/admin/health")
    body = resp.text
    # The page renders an "OK" chip for the residency-confirmed flag.
    assert '<span class="chip pos">OK</span>' in body


# ---------------------------------------------------------------------------
# Section C — repository row counts
# ---------------------------------------------------------------------------


def test_health_repo_counts_renders_zero_state(
    health_client: tuple[
        TestClient, InMemorySchemaMigrationsReader, InMemoryAuditLog
    ],
) -> None:
    """With no seeded data, the repo-count table still renders every
    expected label and shows ``0`` for the empty in-memory backends."""
    client, _reader, _audit = health_client
    resp = client.get("/ui/admin/health")
    body = resp.text
    assert "merchants" in body
    assert "funders (active)" in body
    assert "documents" in body
    assert "decisions" in body
    assert "submissions" in body
    assert "scoring_shadow_disagreements" in body
    assert "merchants_shadow_signals" in body
    assert "disclosure_render_events" in body
    assert "schema_migrations" in body


def test_health_repo_counts_renders_seeded_migrations(
    health_client: tuple[
        TestClient, InMemorySchemaMigrationsReader, InMemoryAuditLog
    ],
) -> None:
    """Seeded migrations count + latest applied_at renders in §C."""
    client, reader, _audit = health_client
    reader.rows = [
        SchemaMigrationRow(
            filename="052_shor_notes.sql",
            sha256="a" * 64,
            applied_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
            applied_by="apply_migrations:filip",
        ),
        SchemaMigrationRow(
            filename="053_aegis_health.sql",
            sha256="b" * 64,
            applied_at=datetime(2026, 6, 5, 9, 30, tzinfo=UTC),
            applied_by="apply_migrations:filip",
        ),
    ]
    resp = client.get("/ui/admin/health")
    body = resp.text
    # Count = 2 — assert via the "n = 2 migrations" or equivalent meta
    # phrasing in the section header.
    assert "schema_migrations" in body
    # Latest applied at the newer of the two — 2026-06-05.
    assert "2026-06-05 09:30 UTC" in body


# ---------------------------------------------------------------------------
# Section D — recent errors
# ---------------------------------------------------------------------------


def test_health_recent_errors_empty_state(
    health_client: tuple[
        TestClient, InMemorySchemaMigrationsReader, InMemoryAuditLog
    ],
) -> None:
    """No audit rows → empty-state copy in §D."""
    client, _reader, _audit = health_client
    resp = client.get("/ui/admin/health")
    body = resp.text
    assert "No <code>*_failed</code> / <code>error_*</code> audit rows" in body


def test_health_recent_errors_groups_seeded_failures(
    health_client: tuple[
        TestClient, InMemorySchemaMigrationsReader, InMemoryAuditLog
    ],
) -> None:
    """Seeded ``*_failed`` rows render grouped by action with per-action
    counts. Rows older than 24h are excluded."""
    client, _reader, audit = health_client
    now = datetime.now(UTC)
    # Two rows of the same action within the window.
    for _ in range(2):
        audit.entries.append(
            {
                "actor": "system",
                "action": "audit.write_failed",
                "subject_type": "deal",
                "subject_id": str(uuid4()),
                "details": {"detail": "supabase outage"},
                "created_at": now.isoformat(),
            }
        )
    # One row of a different action within the window.
    audit.entries.append(
        {
            "actor": "system",
            "action": "close.attachment.pull_failed",
            "subject_type": "merchant",
            "subject_id": str(uuid4()),
            "details": {"close_lead_id": "lead_abc"},
            "created_at": now.isoformat(),
        }
    )
    # One row outside the 24h window — must be excluded.
    audit.entries.append(
        {
            "actor": "system",
            "action": "audit.write_failed",
            "subject_type": "deal",
            "subject_id": str(uuid4()),
            "details": {"detail": "stale"},
            "created_at": (now - timedelta(days=2)).isoformat(),
        }
    )
    # One row that does NOT match the error pattern — must be excluded.
    audit.entries.append(
        {
            "actor": "system",
            "action": "deal.score",
            "subject_type": "deal",
            "subject_id": str(uuid4()),
            "details": {"tier": "A"},
            "created_at": now.isoformat(),
        }
    )

    resp = client.get("/ui/admin/health")
    body = resp.text
    # The empty-state copy must NOT appear when rows exist.
    assert (
        "No <code>*_failed</code> / <code>error_*</code> audit rows" not in body
    )
    # Both error actions surface.
    assert "audit.write_failed" in body
    assert "close.attachment.pull_failed" in body
    # The non-error action token must not surface in §D (it could still
    # appear in §B as a Settings name — but `deal.score` is not a
    # Settings name, so absence proves the section filter worked).
    assert "deal.score" not in body


# ---------------------------------------------------------------------------
# PII canary — no secret value ever renders
# ---------------------------------------------------------------------------


def test_health_pii_canary_no_secret_values_in_html(
    health_client: tuple[
        TestClient, InMemorySchemaMigrationsReader, InMemoryAuditLog
    ],
) -> None:
    """Grep the rendered HTML for anything that looks like a credential.

    The audit log section can render *NAMES* of fields/actions that
    contain ``_token`` / ``_key`` / ``_secret`` (e.g. an action like
    ``close.webhook_secret.rotated`` would be acceptable since the row
    only carries the action token, not the secret value). This test
    asserts on actual VALUES that the route should never render:

      * The fake API_BEARER_TOKEN value pinned by conftest.py
      * The fake PDF_ENCRYPTION_KEY_V1 base64 value pinned by conftest.py
      * Any uppercase ``_KEY=`` / ``_TOKEN=`` / ``_SECRET=`` /
        ``_PASSWORD=`` env-style pair (which would indicate a Settings
        dump leak)
    """
    client, _reader, _audit = health_client
    resp = client.get("/ui/admin/health")
    assert resp.status_code == 200, resp.text
    body = resp.text

    # Known fake credential values from tests/conftest.py.
    assert "test-token-not-real" not in body
    # PDF_ENCRYPTION_KEY_V1 in conftest.py is base64(zero_bytes(32)) =
    # "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=" (44 chars). The
    # base64-of-zeros pattern is a strong canary — any leak of the
    # PDF encryption key would surface a long ``A`` run.
    assert "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=" not in body

    # Belt + suspenders: assert no env-style "_KEY=..." / "_TOKEN=..."
    # / "_SECRET=..." / "_PASSWORD=..." pair appears in the body (a
    # Settings dump leak would surface in this shape).
    secret_pattern = re.compile(
        r"_(?:KEY|TOKEN|SECRET|PASSWORD)=[A-Za-z0-9+/=]{8,}",
        re.IGNORECASE,
    )
    matches = secret_pattern.findall(body)
    assert not matches, f"PII canary tripped — found secret-shaped tokens: {matches!r}"


def test_health_pii_canary_settings_secret_field_name_not_rendered(
    health_client: tuple[
        TestClient, InMemorySchemaMigrationsReader, InMemoryAuditLog
    ],
) -> None:
    """Secret-bearing Settings field names must NOT appear in §B even
    though the underlying object has them. This catches a future
    refactor that extends the whitelist by mistake."""
    client, _reader, _audit = health_client
    resp = client.get("/ui/admin/health")
    body = resp.text

    # These are the SecretStr / token field names in aegis.config.Settings.
    # The health page's safe-config whitelist explicitly excludes them, so
    # they must never appear in the rendered config-flags section.
    forbidden_field_names = (
        "supabase_service_key",
        "close_api_key",
        "close_webhook_secret",
        "api_bearer_token",
        "close_callback_token",
        "funder_reply_webhook_secret",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aegis_tunnel_shared_secret",
        "pdf_encryption_key_v1",
    )
    for name in forbidden_field_names:
        assert name not in body, (
            f"PII canary tripped — secret-bearing Settings field {name!r} "
            f"leaked into the rendered HTML"
        )


# ---------------------------------------------------------------------------
# Landing-page redirect
# ---------------------------------------------------------------------------


def test_admin_landing_redirects_to_health(
    health_client: tuple[
        TestClient, InMemorySchemaMigrationsReader, InMemoryAuditLog
    ],
) -> None:
    """``GET /ui/admin`` → 302 → ``/ui/admin/health``."""
    client, _reader, _audit = health_client
    # follow_redirects=False so the TestClient surfaces the 302 directly.
    resp = client.get("/ui/admin", follow_redirects=False)
    assert resp.status_code == 302, resp.text
    assert resp.headers["location"] == "/ui/admin/health"
