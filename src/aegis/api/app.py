"""FastAPI app entry point.

Importing this module triggers ``get_settings()`` so the data-residency
boot guard runs before the server accepts traffic. ``/healthz`` is the
only auth-free route; all other routes inherit ``require_bearer`` via
their router-level dependency.

App lifespan owns the arq Redis pool: it's created on startup and closed
on shutdown. The upload route reads ``app.state.arq_pool`` to enqueue.
When ``aegis_storage_backend == "memory"`` (tests) the pool is left
unset and the upload route falls back to in-process pending-job state.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from aegis.api.routes import ALL_ROUTERS
from aegis.compliance.states import validate_states_table
from aegis.config import get_settings
from aegis.logger import configure_logging, get_logger

_log = get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging()
    validate_states_table()  # boot-time fail-closed compliance check

    app.state.arq_pool = None
    if settings.aegis_storage_backend == "supabase":
        try:
            from arq import create_pool

            from aegis.workers import build_redis_settings

            app.state.arq_pool = await create_pool(build_redis_settings())
            _log.info("api.lifespan.arq_pool_ready")
        except Exception:
            _log.exception("api.lifespan.arq_pool_failed")

    try:
        yield
    finally:
        pool = getattr(app.state, "arq_pool", None)
        if pool is not None:
            await pool.close()


def create_app() -> FastAPI:
    get_settings()  # boot guard (raises DataResidencyError on misconfig)

    app = FastAPI(
        title="AEGIS",
        version="2.0.0",
        description="MCA underwriting brain for Commera Capital.",
        lifespan=_lifespan,
    )

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    for r in ALL_ROUTERS:
        app.include_router(r)

    return app


app = create_app()
