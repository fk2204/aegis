"""Invariants that protect the horizontal-scale readiness claim (mp #5).

Phase 11 task #5 promises the worker codebase is stateless enough to
run on multiple boxes without changes. These tests pin the invariants
that, if violated, would break that claim silently — by walking the
source tree and asserting properties that automation can verify.

The tests deliberately use module-attribute introspection rather than
``grep`` because a syntax-aware check survives refactors that move
code around.
"""

from __future__ import annotations

import importlib
import inspect


def test_settings_caching_is_per_process_only() -> None:
    """get_settings uses lru_cache, which is per-process by design.

    A scaling regression would be replacing the lru_cache with a
    process-shared cache (e.g. file-based memoization) — that would
    break determinism in a multi-box deploy.
    """
    config = importlib.import_module("aegis.config")
    fn = config.get_settings
    # lru_cache decorates the function; the wrapper exposes
    # ``cache_clear`` and ``cache_info`` callables.
    assert hasattr(fn, "cache_clear"), (
        "aegis.config.get_settings lost its lru_cache decorator — "
        "settings would be recomputed on every call, which is OK but "
        "the contract is per-process caching, not no caching."
    )
    assert hasattr(fn, "cache_info"), (
        "aegis.config.get_settings lost cache_info — see above."
    )


def test_worker_module_has_no_module_level_mutable_state() -> None:
    """``aegis.workers`` MUST NOT define module-level mutable containers
    (lists, dicts, sets) used as caches between parse_document calls.

    The horizontal-scale guarantee is that a worker process can be
    killed mid-job and restarted on a different box without losing
    correctness. Module-level mutable caches break that.
    """
    workers = importlib.import_module("aegis.workers")
    # Walk module-level names and flag any that are mutable
    # containers. WorkerSettings is a class — fine. _log is a
    # logger — fine. We allow the typed protocol/dataclass surface
    # but disallow raw dicts/lists/sets at module level.
    offenders: list[tuple[str, type[object]]] = []
    for name, value in vars(workers).items():
        if name.startswith("_"):
            continue
        if inspect.ismodule(value) or inspect.isclass(value):
            continue
        if inspect.isfunction(value) or inspect.iscoroutinefunction(value):
            continue
        if isinstance(value, (dict, list, set)):
            offenders.append((name, type(value)))
    assert not offenders, (
        f"aegis.workers introduced module-level mutable state: {offenders}. "
        "These break horizontal-scale guarantees because two worker "
        "processes would not share the state but might assume they do. "
        "Move state into the repository (Supabase) or arq context."
    )


def test_rate_limit_store_protocol_is_present() -> None:
    """The Protocol that lets us swap InMemoryRateStore for a Redis
    implementation MUST stay in place — that's the documented growth
    path from Stage 2 → Stage 3 in docs/horizontal_scale_readiness.md.
    """
    rate_limit = importlib.import_module("aegis.ops.rate_limit")
    assert hasattr(rate_limit, "RateLimitStore")
    assert hasattr(rate_limit, "InMemoryRateStore")
    # The Protocol class must declare the increment_and_count method
    # — the contract that any swap-in implementation honors.
    proto = rate_limit.RateLimitStore
    assert "increment_and_count" in dir(proto), (
        "RateLimitStore lost its increment_and_count method. A Redis "
        "swap-in needs this signature; removing it breaks the swap."
    )
