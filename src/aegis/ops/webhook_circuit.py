"""Close webhook circuit breaker.

Per-lead consecutive-failure counter backed by Redis. Opens the breaker
after a threshold of consecutive failures so Close stops getting
"please process this" retries against a webhook payload that has
proven to repeatedly fail — surfacing the bad payload to the operator
via the ``GET /ui/webhooks/circuits`` page instead of letting it
poison the queue.

Design contract:

* Failure increments ``webhook:circuit:{lead_id}`` (an integer count).
* Success resets the key to 0 (deletes it — TTL handles same effect).
* The key TTL is 1 hour from the most recent failure, so a lead that
  goes quiet eventually auto-resets without operator intervention.
* The breaker is OPEN iff the counter is ``>= OPEN_THRESHOLD``. While
  open, the webhook handler short-circuits with 204 (acknowledged so
  Close stops retrying) and writes a ``close.webhook.circuit_open``
  audit row PER reception.
* Operator can force-reset a single circuit from ``/ui/webhooks/
  circuits`` — that deletes the key + writes ``close.webhook.
  circuit_reset``.

Why a Protocol-based backend
----------------------------
We deliberately don't take ``redis.Redis`` as the only backend. The
production wiring is Redis; tests inject ``InMemoryCircuitBackend`` so
the unit-test suite doesn't need ``fakeredis`` as a dependency.
"""

from __future__ import annotations

from typing import Protocol

from redis import Redis, RedisError

from aegis.config import get_settings

#: Consecutive failures required to open the circuit. 5 is enough to
#: filter out transient flakes (one bad redelivery, a single Bedrock
#: timeout cascade) while still tripping fast on a truly broken payload.
OPEN_THRESHOLD: int = 5

#: TTL (seconds) applied to the counter key on every failure. One hour
#: lets a quiet lead auto-recover after the last failure without
#: operator intervention; the operator's force-reset bypasses this.
KEY_TTL_SECONDS: int = 60 * 60

#: Redis key prefix. The full key is ``webhook:circuit:{lead_id}``.
_KEY_PREFIX: str = "webhook:circuit:"


def _key(close_lead_id: str) -> str:
    return _KEY_PREFIX + close_lead_id


class CircuitBackend(Protocol):
    """Minimal Redis-shaped surface the breaker needs.

    The production implementation is ``RedisCircuitBackend`` (wraps a
    real ``redis.Redis`` client). The test implementation is
    ``InMemoryCircuitBackend``.
    """

    def incr(self, key: str) -> int: ...

    def expire(self, key: str, seconds: int) -> None: ...

    def get_int(self, key: str) -> int: ...

    def delete(self, key: str) -> None: ...

    def scan_open_keys(self, *, threshold: int) -> list[tuple[str, int]]:
        """Return every ``(lead_id, count)`` pair whose counter ``>= threshold``.

        Powers the operator's UI. Returns lead_id only (not the full
        Redis key) so the route layer doesn't have to know the prefix.
        """


class InMemoryCircuitBackend:
    """Dict-backed test double. Same surface as ``RedisCircuitBackend``."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}

    def incr(self, key: str) -> int:
        self._counters[key] = self._counters.get(key, 0) + 1
        return self._counters[key]

    def expire(self, key: str, seconds: int) -> None:
        # TTL tracking isn't needed for tests — we test the failure /
        # success / reset semantics directly. Production behavior is
        # exercised by the Redis backend's integration tests on the box.
        _ = (key, seconds)  # explicit unused — keep the surface match
        return None

    def get_int(self, key: str) -> int:
        return self._counters.get(key, 0)

    def delete(self, key: str) -> None:
        self._counters.pop(key, None)

    def scan_open_keys(self, *, threshold: int) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        for key, count in self._counters.items():
            if count < threshold:
                continue
            if not key.startswith(_KEY_PREFIX):
                continue
            out.append((key[len(_KEY_PREFIX) :], count))
        return out


class RedisCircuitBackend:
    """Production backend — wraps a real ``redis.Redis`` connection.

    A ``RedisError`` during ``incr`` / ``expire`` / ``delete`` is logged
    upstream by the caller; this module raises so the caller decides
    whether to fail-open or fail-closed. The webhook handler fails OPEN
    (don't trip on Redis being down — we'd rather process a few
    extra retries than reject legit traffic), and the UI page surfaces
    the Redis error directly so the operator notices.
    """

    def __init__(self, client: Redis[bytes]) -> None:
        self._client = client

    def incr(self, key: str) -> int:
        return int(self._client.incr(key))

    def expire(self, key: str, seconds: int) -> None:
        self._client.expire(key, seconds)

    def get_int(self, key: str) -> int:
        raw = self._client.get(key)
        if raw is None:
            return 0
        try:
            return int(raw)
        except (TypeError, ValueError):
            # Corrupt value — treat as zero so the breaker stays closed;
            # the next failure rewrites the key cleanly via incr.
            return 0

    def delete(self, key: str) -> None:
        self._client.delete(key)

    def scan_open_keys(self, *, threshold: int) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        cursor = 0
        match_pattern = _KEY_PREFIX + "*"
        while True:
            cursor, batch = self._client.scan(cursor=cursor, match=match_pattern, count=100)
            for raw_key in batch:
                key = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else str(raw_key)
                raw_val = self._client.get(key)
                if raw_val is None:
                    continue
                try:
                    count = int(raw_val)
                except (TypeError, ValueError):
                    continue
                if count >= threshold:
                    out.append((key[len(_KEY_PREFIX) :], count))
            if cursor == 0:
                break
        return out


class WebhookCircuit:
    """Per-lead consecutive-failure breaker.

    The handler calls ``is_open`` before processing and ``record_*``
    after. Backend swap is via the constructor — production wires a
    ``RedisCircuitBackend``, tests wire ``InMemoryCircuitBackend``.
    """

    def __init__(
        self,
        backend: CircuitBackend,
        *,
        threshold: int = OPEN_THRESHOLD,
        ttl_seconds: int = KEY_TTL_SECONDS,
    ) -> None:
        self._backend = backend
        self._threshold = threshold
        self._ttl = ttl_seconds

    def record_failure(self, close_lead_id: str) -> int:
        """Increment the failure counter; return the post-increment count.

        Refreshes the TTL on every failure so the open state survives
        repeated retries — the TTL only counts down once failures STOP.
        """
        key = _key(close_lead_id)
        count = self._backend.incr(key)
        self._backend.expire(key, self._ttl)
        return count

    def record_success(self, close_lead_id: str) -> None:
        """Clear the counter — one success resets the consecutive streak."""
        self._backend.delete(_key(close_lead_id))

    def is_open(self, close_lead_id: str) -> bool:
        """True when the breaker has tripped open for this lead."""
        return self._backend.get_int(_key(close_lead_id)) >= self._threshold

    def reset(self, close_lead_id: str) -> None:
        """Force-close the breaker for one lead (operator action)."""
        self._backend.delete(_key(close_lead_id))

    def list_open_circuits(self) -> list[tuple[str, int]]:
        """Return every (lead_id, count) pair whose counter is at or above
        the open threshold. Powers the ``/ui/webhooks/circuits`` page."""
        return self._backend.scan_open_keys(threshold=self._threshold)


def build_default_circuit() -> WebhookCircuit:
    """Construct a ``WebhookCircuit`` backed by the configured Redis.

    Pulls the URL from ``Settings.redis_url`` — same source as the
    ``arq`` worker so the circuit shares the box's existing Redis.
    Raises ``RedisError`` only when constructing the client fails;
    actual command failures bubble up from the backend methods.
    """
    settings = get_settings()
    client: Redis[bytes] = Redis.from_url(settings.redis_url)
    return WebhookCircuit(RedisCircuitBackend(client))


__all__ = [
    "KEY_TTL_SECONDS",
    "OPEN_THRESHOLD",
    "CircuitBackend",
    "InMemoryCircuitBackend",
    "RedisCircuitBackend",
    "RedisError",
    "WebhookCircuit",
    "build_default_circuit",
]
