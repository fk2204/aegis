"""Rate-limiting middleware (mp Phase 11 task #3).

Two buckets per request:

  * **Per-IP** — keyed by ``X-Forwarded-For`` (Cloudflare Tunnel sets
    this) falling back to the socket address.
  * **Per-bearer** — keyed by a sha256 hash of the bearer token so
    multi-operator growth doesn't share a global per-token bucket.

Both buckets are sliding-window-count: the middleware records the
timestamp of each request and counts how many fall within the
configured window. Limits are pinned in code (this module) so a
review of "what's the rate limit policy?" is a single grep.

Default limits target the operator-zero-touch, ~100 deals/month
profile from CLAUDE.md. They're generous on read-heavy paths (the UI
issues a flurry of GETs while a dossier loads) and tighter on the
state-changing surfaces (POST /deals/score, POST /upload).

Storage backend
---------------
This module ships an in-process ``InMemoryRateStore`` because the
production deployment is a single Hetzner box. When Phase 11 task #5
(horizontal scale) activates and the app spans more than one host, a
``RedisRateStore`` implementation needs to land — the protocol below
locks the interface so the swap is local. Don't add a global limiter
service yet; the in-process store is the right tool for the current
operator profile.

Exempt paths
------------
``/healthz`` is the ONLY path exempt from the limiter. It must always
respond to Cloudflare Tunnel + monitoring without consuming bucket
budget; the operator's runbook depends on this.
"""

from __future__ import annotations

import hashlib
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Final, Protocol

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from aegis.logger import get_logger

_log = get_logger(__name__)


# --- limit table (per CLAUDE.md ~100 deals/month operator profile) -------


@dataclass(frozen=True)
class RateLimit:
    """One request-bucket rule."""

    #: Window in seconds.
    window_seconds: int
    #: Max requests allowed in the window.
    max_requests: int


#: Default per-IP limit for any authenticated route. Tuned to leave the
#: dashboard's read traffic well under the cap while making it expensive
#: for a misconfigured Zoho automation to runaway-loop the API.
DEFAULT_PER_IP_LIMIT: Final[RateLimit] = RateLimit(window_seconds=60, max_requests=120)

#: Default per-bearer (per-operator) limit. Aggregates across IPs so a
#: single operator's load is bounded even when they're on a mobile +
#: desktop simultaneously.
DEFAULT_PER_BEARER_LIMIT: Final[RateLimit] = RateLimit(
    window_seconds=60, max_requests=300
)

#: Heavy-path overrides. Path-prefix → limit. Order matters — the
#: longest matching prefix wins (we sort by length descending at lookup
#: time). Heavy paths get tighter caps because they cost real money
#: (Bedrock, Zoho writes) or PII-sensitive disk I/O.
PATH_LIMITS_PER_IP: Final[dict[str, RateLimit]] = {
    # Upload + multi-statement intake — bound the rate of new PDFs to
    # what the parser can chew through without queue backup.
    "/upload": RateLimit(window_seconds=60, max_requests=12),
    # Score endpoints touch Bedrock indirectly + OFAC; tighter cap.
    "/deals/score": RateLimit(window_seconds=60, max_requests=30),
    "/deals/score-with-matches": RateLimit(window_seconds=60, max_requests=30),
    # Zoho push is operator-triggered; tighter cap protects against a
    # double-click loop.
    "/deals": RateLimit(window_seconds=60, max_requests=60),
    # Webhook surface is HMAC-verified upstream of this; cap protects
    # against a flood from a misconfigured Zoho workflow.
    "/webhooks/zoho": RateLimit(window_seconds=60, max_requests=30),
}

#: Path that must NEVER be rate-limited. Cloudflare Tunnel uses this
#: for tunnel-side liveness, and the operator's heartbeat unit hits
#: it from the box itself.
EXEMPT_PATHS: Final[frozenset[str]] = frozenset({"/healthz"})


# --- storage backend ------------------------------------------------------


class RateLimitStore(Protocol):
    """Protocol for the bucket store. Swap to Redis when Phase 11 task #5
    activates (multi-box)."""

    def increment_and_count(
        self, key: str, *, window_seconds: int, now: float
    ) -> int:
        """Record ``now`` against ``key`` and return the count of timestamps
        in the trailing window."""


class InMemoryRateStore:
    """In-process sliding-window store. Thread-safe under the GIL.

    Uses a per-key deque so the trim cost on the hot path is O(K)
    where K is the number of evicted timestamps — typically 0-1 per
    request at steady state.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, deque[float]] = defaultdict(deque)

    def increment_and_count(
        self, key: str, *, window_seconds: int, now: float
    ) -> int:
        bucket = self._buckets[key]
        cutoff = now - window_seconds
        # Evict timestamps that fell out of the window.
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        bucket.append(now)
        return len(bucket)

    def reset(self) -> None:
        """Test-only — clear all buckets."""
        self._buckets.clear()


# --- middleware ------------------------------------------------------------


def _resolve_per_ip_limit(path: str) -> RateLimit:
    """Find the longest matching path prefix from PATH_LIMITS_PER_IP."""
    matches = sorted(
        (p for p in PATH_LIMITS_PER_IP if path.startswith(p)),
        key=len,
        reverse=True,
    )
    if matches:
        return PATH_LIMITS_PER_IP[matches[0]]
    return DEFAULT_PER_IP_LIMIT


def _client_ip(request: Request) -> str:
    """Resolve the requesting client IP.

    Cloudflare Tunnel forwards the originating IP via ``X-Forwarded-For``;
    the first comma-separated entry is the client. When the header is
    absent (direct local calls during dev/tests), fall back to the
    Starlette client tuple.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",", 1)[0].strip()
    if request.client is not None:
        return request.client.host
    return "unknown"


def _bearer_key(request: Request) -> str | None:
    """Return a stable per-bearer bucket key, or None if no bearer.

    The key is a sha256 prefix so a stolen log line / audit row never
    leaks the bearer itself.
    """
    authz = request.headers.get("authorization")
    if not authz or not authz.lower().startswith("bearer "):
        return None
    token = authz[len("bearer ") :].strip()
    if not token:
        return None
    return "bearer:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Starlette/FastAPI middleware applying per-IP + per-bearer limits.

    Builds two bucket keys per request:

      * ``ip:<client_ip>:<path-bucket-or-default>`` — per-IP, per
        path-bucket. Path-bucket lets the limiter charge a heavy path
        against a tighter budget without touching the default cap.
      * ``bearer:<sha256-prefix>`` — per-bearer global; aggregates
        across paths so a single operator's total throughput is bounded.

    Returns 429 with a small JSON body when EITHER bucket trips. The
    response carries ``Retry-After`` set to the window so the client's
    backoff strategy can pick a sane interval.
    """

    def __init__(
        self,
        app: FastAPI,
        *,
        store: RateLimitStore | None = None,
    ) -> None:
        super().__init__(app)
        self._store = store if store is not None else InMemoryRateStore()

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> JSONResponse:
        path = request.url.path
        if path in EXEMPT_PATHS:
            return await call_next(request)  # type: ignore[return-value]

        now = time.monotonic()
        client_ip = _client_ip(request)
        per_ip_limit = _resolve_per_ip_limit(path)

        # Per-IP bucket: include the matched path-bucket so heavy paths
        # don't drain the default budget for read-only GETs.
        path_bucket = _matched_path_bucket(path)
        ip_key = f"ip:{client_ip}:{path_bucket}"
        ip_count = self._store.increment_and_count(
            ip_key,
            window_seconds=per_ip_limit.window_seconds,
            now=now,
        )
        if ip_count > per_ip_limit.max_requests:
            _log.warning(
                "ops.rate_limit.tripped scope=per_ip ip=%s path=%s "
                "count=%d limit=%d window=%ds",
                client_ip,
                path,
                ip_count,
                per_ip_limit.max_requests,
                per_ip_limit.window_seconds,
            )
            return _too_many(per_ip_limit, scope="per_ip")

        # Per-bearer bucket runs globally across paths. Routes without
        # a bearer (the few unauth surfaces) skip this layer; auth
        # middleware downstream rejects them on its own terms.
        bearer_key = _bearer_key(request)
        if bearer_key is not None:
            bearer_count = self._store.increment_and_count(
                bearer_key,
                window_seconds=DEFAULT_PER_BEARER_LIMIT.window_seconds,
                now=now,
            )
            if bearer_count > DEFAULT_PER_BEARER_LIMIT.max_requests:
                _log.warning(
                    "ops.rate_limit.tripped scope=per_bearer path=%s "
                    "count=%d limit=%d window=%ds",
                    path,
                    bearer_count,
                    DEFAULT_PER_BEARER_LIMIT.max_requests,
                    DEFAULT_PER_BEARER_LIMIT.window_seconds,
                )
                return _too_many(DEFAULT_PER_BEARER_LIMIT, scope="per_bearer")

        return await call_next(request)  # type: ignore[return-value]


def _matched_path_bucket(path: str) -> str:
    """Return the path bucket label (or 'default') the limiter charges."""
    matches = sorted(
        (p for p in PATH_LIMITS_PER_IP if path.startswith(p)),
        key=len,
        reverse=True,
    )
    if matches:
        return matches[0]
    return "default"


def _too_many(limit: RateLimit, *, scope: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={
            "detail": "rate_limit_exceeded",
            "scope": scope,
            "limit": limit.max_requests,
            "window_seconds": limit.window_seconds,
        },
        headers={"Retry-After": str(limit.window_seconds)},
    )


__all__ = [
    "DEFAULT_PER_BEARER_LIMIT",
    "DEFAULT_PER_IP_LIMIT",
    "EXEMPT_PATHS",
    "PATH_LIMITS_PER_IP",
    "InMemoryRateStore",
    "RateLimit",
    "RateLimitMiddleware",
    "RateLimitStore",
]
