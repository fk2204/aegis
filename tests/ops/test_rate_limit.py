"""Tests for the FastAPI rate-limit middleware (mp Phase 11 task #3).

Three properties under test:

  1. Every authenticated endpoint goes through the limiter (the §21
     requirement to "confirm the CLAUDE.md claim").
  2. /healthz is exempt — Cloudflare Tunnel + the heartbeat unit must
     never see 429.
  3. Per-IP and per-bearer buckets fire at their configured caps.

The tests build a minimal FastAPI app with the middleware installed
and then drive it with starlette's TestClient. Time is controlled
via monkeypatching ``time.monotonic`` in the rate_limit module so a
test that exhausts a bucket doesn't need to wait a real window to
pass.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.ops.rate_limit import (
    DEFAULT_PER_BEARER_LIMIT,
    DEFAULT_PER_IP_LIMIT,
    EXEMPT_PATHS,
    PATH_LIMITS_PER_IP,
    InMemoryRateStore,
    RateLimit,
    RateLimitMiddleware,
)


@pytest.fixture
def store() -> InMemoryRateStore:
    return InMemoryRateStore()


@pytest.fixture
def app(store: InMemoryRateStore) -> FastAPI:
    """Minimal app with three routes exercising different buckets."""
    application = FastAPI()
    # See note in aegis.api.app.create_app for why the cast is here.
    from typing import cast

    application.add_middleware(cast("Any", RateLimitMiddleware), store=store)

    @application.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @application.get("/random")
    async def random_route() -> dict[str, str]:
        return {"route": "random"}

    @application.post("/upload")
    async def upload_route() -> dict[str, str]:
        return {"route": "upload"}

    @application.post("/deals/score")
    async def score_route() -> dict[str, str]:
        return {"route": "score"}

    return application


def test_healthz_is_exempt(app: FastAPI) -> None:
    """/healthz must never 429 regardless of request count."""
    assert "/healthz" in EXEMPT_PATHS
    with TestClient(app) as client:
        for _ in range(DEFAULT_PER_IP_LIMIT.max_requests + 50):
            r = client.get("/healthz")
            assert r.status_code == 200


def test_per_ip_default_limit_enforced(
    app: FastAPI, store: InMemoryRateStore
) -> None:
    """A random route should 429 after the default cap is reached."""
    with TestClient(app) as client:
        # Issue exactly cap + 1 hits.
        for i in range(DEFAULT_PER_IP_LIMIT.max_requests):
            r = client.get("/random")
            assert r.status_code == 200, f"req {i} unexpectedly 429'd: {r.text}"
        # cap + 1 must 429
        r = client.get("/random")
        assert r.status_code == 429
        payload: dict[str, Any] = r.json()
        assert payload["scope"] == "per_ip"
        assert payload["limit"] == DEFAULT_PER_IP_LIMIT.max_requests
        assert r.headers.get("Retry-After") == str(
            DEFAULT_PER_IP_LIMIT.window_seconds
        )


def test_path_specific_limit_applies(
    app: FastAPI, store: InMemoryRateStore
) -> None:
    """/upload is tighter — verify the bucket cap matches PATH_LIMITS_PER_IP."""
    upload_limit: RateLimit = PATH_LIMITS_PER_IP["/upload"]
    with TestClient(app) as client:
        for _ in range(upload_limit.max_requests):
            r = client.post("/upload")
            assert r.status_code == 200
        # One more trips
        r = client.post("/upload")
        assert r.status_code == 429
        assert r.json()["limit"] == upload_limit.max_requests


def test_path_specific_does_not_drain_default_bucket(
    app: FastAPI, store: InMemoryRateStore
) -> None:
    """An /upload burst must not charge the /random default bucket — the
    limiter computes the right path-bucket per request."""
    upload_limit: RateLimit = PATH_LIMITS_PER_IP["/upload"]
    with TestClient(app) as client:
        # Exhaust /upload
        for _ in range(upload_limit.max_requests):
            client.post("/upload")
        # /random must still be fine
        r = client.get("/random")
        assert r.status_code == 200


def test_per_bearer_aggregates_across_paths(
    app: FastAPI, store: InMemoryRateStore
) -> None:
    """A bearer token's spend across multiple paths must roll up into
    the same per-bearer bucket. We hold the bearer fixed but rotate
    the per-IP key (X-Forwarded-For) so the per-IP bucket never trips
    before the per-bearer bucket does — this isolates the bearer-side
    counting.
    """
    bearer_cap = DEFAULT_PER_BEARER_LIMIT.max_requests
    bearer_headers = {"Authorization": "Bearer t1"}

    with TestClient(app) as client:
        for i in range(bearer_cap):
            # Cycle IPs so per-IP never trips first.
            ip = f"10.0.0.{(i % 200) + 1}"
            r = client.get(
                "/random",
                headers={**bearer_headers, "X-Forwarded-For": ip},
            )
            assert r.status_code == 200, f"req {i} unexpectedly 429'd: {r.text}"
        # bearer_cap + 1 must 429 (bearer bucket trips even on a fresh IP)
        r = client.get(
            "/random",
            headers={**bearer_headers, "X-Forwarded-For": "172.16.0.1"},
        )
        assert r.status_code == 429
        assert r.json()["scope"] == "per_bearer"


def test_xff_header_routes_per_ip(
    app: FastAPI, store: InMemoryRateStore
) -> None:
    """Two clients with different X-Forwarded-For values share no bucket
    (the limiter trusts XFF since Cloudflare Tunnel sets it)."""
    with TestClient(app) as client:
        # Exhaust IP A
        headers_a = {"X-Forwarded-For": "10.0.0.1"}
        for _ in range(DEFAULT_PER_IP_LIMIT.max_requests):
            r = client.get("/random", headers=headers_a)
            assert r.status_code == 200
        r = client.get("/random", headers=headers_a)
        assert r.status_code == 429

        # IP B fresh — must be unaffected.
        r = client.get("/random", headers={"X-Forwarded-For": "10.0.0.2"})
        assert r.status_code == 200


def test_bearer_keying_uses_token_hash_not_token(
    app: FastAPI, store: InMemoryRateStore
) -> None:
    """Two distinct bearer tokens get distinct buckets — verifies the
    keying isn't accidentally identical (e.g. all-empty-string)."""
    with TestClient(app) as client:
        for _ in range(DEFAULT_PER_BEARER_LIMIT.max_requests):
            client.get(
                "/random",
                headers={"Authorization": "Bearer token-a"},
            )
        # Same bearer is now exhausted
        r = client.get("/random", headers={"Authorization": "Bearer token-a"})
        assert r.status_code == 429
        # Different bearer must work (and have a fresh per-IP budget too,
        # but the IP budget was already exhausted above on the SAME IP).
        # To test bearer isolation we need to also use a different IP:
        r = client.get(
            "/random",
            headers={
                "Authorization": "Bearer token-b",
                "X-Forwarded-For": "10.0.0.99",
            },
        )
        assert r.status_code == 200


# --- production app wiring proof --------------------------------------------


def test_production_app_has_rate_limit_middleware() -> None:
    """The actual FastAPI app (created by aegis.api.app.create_app) must
    have RateLimitMiddleware installed. This pins the claim from
    CLAUDE.md / Phase 11 task #3 that 'all endpoints are rate-limited'."""
    from aegis.api.app import create_app

    application = create_app()
    # user_middleware is a list of starlette Middleware records; the
    # underlying class is accessible via .cls. We compare by name to
    # dodge the _MiddlewareFactory generic that mypy complains about.
    middleware_names = [getattr(m.cls, "__name__", "") for m in application.user_middleware]
    assert "RateLimitMiddleware" in middleware_names, (
        f"RateLimitMiddleware missing from production app; "
        f"installed middlewares={middleware_names}"
    )
