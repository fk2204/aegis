"""Tests for ``GET /ui/admin/applied-migrations`` + ``GET /ui/admin/audit-log``
(U32 — operator visibility for migrations + audit log).

Covers:

  * applied-migrations: empty + populated, banner copy, sha256 truncation,
    newest-first ordering.
  * audit-log: empty + populated, banner copy, action-prefix filter,
    ``?days=N`` narrows the window, ``?limit=N`` caps row count,
    details keys render without values.

Both routes are exercised through ``TestClient`` against in-memory
fakes injected via ``app.dependency_overrides``. Mirrors the
``test_disclosure_events_route`` / ``test_triage_route`` patterns.
"""

from __future__ import annotations

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
def admin_client() -> Iterator[
    tuple[TestClient, InMemorySchemaMigrationsReader, InMemoryAuditLog]
]:
    """TestClient with the migrations reader + audit log pinned in-memory.

    Yields the live fakes so each test pre-seeds rows directly.
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
# applied-migrations route
# ---------------------------------------------------------------------------


def test_applied_migrations_empty_renders_banner_and_empty_state(
    admin_client: tuple[
        TestClient, InMemorySchemaMigrationsReader, InMemoryAuditLog
    ],
) -> None:
    """Empty reader → 200 + banner copy + empty-state message."""
    client, _reader, _audit = admin_client
    resp = client.get("/ui/admin/applied-migrations")
    assert resp.status_code == 200, resp.text
    body = resp.text

    # Banner + heading.
    assert "schema migrations applied to prod" in body
    assert "schema_migrations" in body
    assert "make migrate TARGET=prod" in body

    # Empty-state copy.
    assert "No migrations recorded" in body


def test_applied_migrations_renders_rows_newest_first(
    admin_client: tuple[
        TestClient, InMemorySchemaMigrationsReader, InMemoryAuditLog
    ],
) -> None:
    """Two rows render in newest-first order with sha256 truncated to 12 chars."""
    client, reader, _audit = admin_client
    older_sha = "a" * 64
    newer_sha = "b" * 64
    reader.rows = [
        SchemaMigrationRow(
            filename="049_logic_ucs_tiers.sql",
            sha256=older_sha,
            applied_at=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
            applied_by="apply_migrations:filip",
        ),
        SchemaMigrationRow(
            filename="052_shor_notes_enrichment.sql",
            sha256=newer_sha,
            applied_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
            applied_by="apply_migrations:filip",
        ),
    ]

    resp = client.get("/ui/admin/applied-migrations")
    assert resp.status_code == 200, resp.text
    body = resp.text

    assert "049_logic_ucs_tiers.sql" in body
    assert "052_shor_notes_enrichment.sql" in body

    # Newest-first ordering: 052 appears before 049 in the rendered body.
    assert body.index("052_shor_notes_enrichment.sql") < body.index(
        "049_logic_ucs_tiers.sql"
    )

    # sha256 truncation — first 12 chars surface, full hash is in the
    # title attribute for copy/paste diffing.
    assert newer_sha[:12] in body
    assert older_sha[:12] in body
    # The 12-char prefix is "bbbbbbbbbbbb" / "aaaaaaaaaaaa"; assert that
    # the rendered cell shows the truncation marker.
    assert "bbbbbbbbbbbb…" in body
    assert "aaaaaaaaaaaa…" in body

    # applied_by surface.
    assert "apply_migrations:filip" in body


# ---------------------------------------------------------------------------
# audit-log route
# ---------------------------------------------------------------------------


def test_audit_log_empty_renders_banner_and_empty_state(
    admin_client: tuple[
        TestClient, InMemorySchemaMigrationsReader, InMemoryAuditLog
    ],
) -> None:
    """Empty audit log → 200 + banner + empty-state message."""
    client, _reader, _audit = admin_client
    resp = client.get("/ui/admin/audit-log")
    assert resp.status_code == 200, resp.text
    body = resp.text

    # Banner copy.
    assert "recent audit_log rows" in body
    assert "Append-only state-change log" in body

    # Empty-state message.
    assert "No audit rows matching the current filter" in body


def test_audit_log_renders_seeded_rows(
    admin_client: tuple[
        TestClient, InMemorySchemaMigrationsReader, InMemoryAuditLog
    ],
) -> None:
    """Seed three audit rows; all three actions surface in the rendered body."""
    client, _reader, audit = admin_client
    audit.entries.append(
        {
            "actor": "system",
            "action": "close.orchestration.enqueued",
            "subject_type": "merchant",
            "subject_id": str(uuid4()),
            "details": {"close_lead_id": "lead_abc"},
            "created_at": datetime.now(UTC).isoformat(),
        }
    )
    audit.entries.append(
        {
            "actor": "filip",
            "action": "deal.score",
            "subject_type": "deal",
            "subject_id": str(uuid4()),
            "details": {"tier": "B", "score": 70},
            "created_at": datetime.now(UTC).isoformat(),
        }
    )
    audit.entries.append(
        {
            "actor": "system",
            "action": "deal.submit_to_funders",
            "subject_type": "merchant",
            "subject_id": str(uuid4()),
            "details": {"funder_ids": ["fid_a", "fid_b"]},
            "created_at": datetime.now(UTC).isoformat(),
        }
    )

    resp = client.get("/ui/admin/audit-log")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "close.orchestration.enqueued" in body
    assert "deal.score" in body
    assert "deal.submit_to_funders" in body
    # The empty-state copy must NOT appear when rows are present.
    assert "No audit rows matching the current filter" not in body


def test_audit_log_action_prefix_filter_narrows(
    admin_client: tuple[
        TestClient, InMemorySchemaMigrationsReader, InMemoryAuditLog
    ],
) -> None:
    """``?action=close.`` returns only ``close.*`` rows."""
    client, _reader, audit = admin_client
    audit.entries.append(
        {
            "actor": "system",
            "action": "close.orchestration.enqueued",
            "subject_type": "merchant",
            "subject_id": str(uuid4()),
            "details": {"close_lead_id": "lead_abc"},
            "created_at": datetime.now(UTC).isoformat(),
        }
    )
    audit.entries.append(
        {
            "actor": "filip",
            "action": "deal.score",
            "subject_type": "deal",
            "subject_id": str(uuid4()),
            "details": {"tier": "A"},
            "created_at": datetime.now(UTC).isoformat(),
        }
    )

    resp = client.get("/ui/admin/audit-log?action=close.")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "close.orchestration.enqueued" in body
    # The deal.score row's action token must not surface under the close.
    # prefix filter. Assert on the literal action token rather than the
    # filter input field value (which is empty when no filter applied
    # and "close." here — neither matches "deal.score").
    assert "deal.score" not in body


def test_audit_log_days_filter_excludes_old_rows(
    admin_client: tuple[
        TestClient, InMemorySchemaMigrationsReader, InMemoryAuditLog
    ],
) -> None:
    """``?days=7`` excludes rows older than 7 days."""
    client, _reader, audit = admin_client
    now = datetime.now(UTC)
    audit.entries.append(
        {
            "actor": "system",
            "action": "deal.score",
            "subject_type": "deal",
            "subject_id": str(uuid4()),
            "details": {"tier": "B", "score_marker": "fresh-row-marker"},
            "created_at": now.isoformat(),
        }
    )
    audit.entries.append(
        {
            "actor": "system",
            "action": "deal.score",
            "subject_type": "deal",
            "subject_id": str(uuid4()),
            "details": {"tier": "C", "score_marker": "stale-row-marker"},
            "created_at": (now - timedelta(days=30)).isoformat(),
        }
    )

    resp = client.get("/ui/admin/audit-log?days=7")
    assert resp.status_code == 200, resp.text
    body = resp.text
    # Fresh row's details key surfaces (the template renders the key
    # list, not the values, so we assert on a key whose presence
    # uniquely tags the fresh row).
    # Both rows have action ``deal.score`` so we can't differentiate via
    # action alone — instead, count visible rows by counting unique
    # subject_id suffixes after we strip them out. Simpler approach:
    # assert ``n = 1 row`` appears in the meta heading.
    assert "n = 1 row" in body


def test_audit_log_limit_caps_row_count(
    admin_client: tuple[
        TestClient, InMemorySchemaMigrationsReader, InMemoryAuditLog
    ],
) -> None:
    """``?limit=2`` caps the rendered row count to 2 even with 5 seeded."""
    client, _reader, audit = admin_client
    for i in range(5):
        audit.entries.append(
            {
                "actor": "system",
                "action": f"test.action_{i}",
                "subject_type": "deal",
                "subject_id": str(uuid4()),
                "details": {"i": i},
                "created_at": datetime.now(UTC).isoformat(),
            }
        )

    resp = client.get("/ui/admin/audit-log?limit=2")
    assert resp.status_code == 200, resp.text
    body = resp.text
    # The meta heading reports the rendered count.
    assert "n = 2 rows" in body


def test_audit_log_above_max_limit_rejected(
    admin_client: tuple[
        TestClient, InMemorySchemaMigrationsReader, InMemoryAuditLog
    ],
) -> None:
    """``?limit=10000`` exceeds the validator bound → 422."""
    client, _reader, _audit = admin_client
    resp = client.get("/ui/admin/audit-log?limit=10000")
    assert resp.status_code == 422, resp.text


def test_audit_log_renders_details_keys_not_values(
    admin_client: tuple[
        TestClient, InMemorySchemaMigrationsReader, InMemoryAuditLog
    ],
) -> None:
    """PII posture — the keys in the JSONB surface; the values do NOT."""
    client, _reader, audit = admin_client
    audit.entries.append(
        {
            "actor": "system",
            "action": "deal.score",
            "subject_type": "deal",
            "subject_id": str(uuid4()),
            # Sentinel value the template must NOT render.
            "details": {
                "tier": "A",
                "sensitive_value_should_not_render": "PII_LEAK_SENTINEL_XYZ",
            },
            "created_at": datetime.now(UTC).isoformat(),
        }
    )

    resp = client.get("/ui/admin/audit-log")
    assert resp.status_code == 200, resp.text
    body = resp.text
    # Both keys render.
    assert "tier" in body
    assert "sensitive_value_should_not_render" in body
    # The value does NOT render — the template only walks the key list.
    assert "PII_LEAK_SENTINEL_XYZ" not in body
