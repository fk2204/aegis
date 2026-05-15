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
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from aegis.api.auth import warn_if_bearer_unconfigured
from aegis.api.deps import get_merchant_repository, get_repository
from aegis.api.routes import ALL_ROUTERS
from aegis.compliance.states import validate_states_table
from aegis.config import get_settings
from aegis.logger import configure_logging, get_logger
from aegis.merchants.repository import MerchantRepository
from aegis.storage import DocumentRepository

_log = get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging()
    validate_states_table()  # boot-time fail-closed compliance check
    warn_if_bearer_unconfigured()  # once-per-process operator visibility

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

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        """Send a fresh visitor to the dashboard.

        Workers signing in via Cloudflare Access land here first. Without
        this redirect FastAPI's default 404 surfaces as
        ``{"detail":"Not Found"}`` which is operator-hostile.
        """
        return RedirectResponse(url="/ui/", status_code=302)

    @app.get("/applicants", include_in_schema=False)
    async def applicants_lookup(
        email: str,
        merchants: Annotated[
            MerchantRepository, Depends(get_merchant_repository)
        ],
        documents: Annotated[DocumentRepository, Depends(get_repository)],
    ) -> RedirectResponse:
        # Entry point for the Zoho CRM "View in Aegis" Lead button
        # (id 7365508000001462009). Routes the operator to the right
        # surface: merchant detail if statements exist, dashboard
        # otherwise (including unknown email).
        merchant = merchants.find_by_email(email)
        if merchant is None:
            return RedirectResponse(url="/ui/", status_code=302)
        has_docs = bool(documents.list_documents(merchant_id=merchant.id, limit=1))
        if has_docs:
            return RedirectResponse(
                url=f"/ui/merchants/{merchant.id}", status_code=302
            )
        return RedirectResponse(url="/ui/", status_code=302)

    for r in ALL_ROUTERS:
        app.include_router(r)

    # v2 design assets — CSS, fonts, future static images. Mounted under
    # /ui/static so paths in base.html.j2 are stable. No auth needed; the
    # files are bundled with the app and contain no secrets.
    _static_dir = Path(__file__).resolve().parent.parent / "web" / "static"
    if _static_dir.is_dir():
        app.mount(
            "/ui/static",
            StaticFiles(directory=str(_static_dir)),
            name="static",
        )

    return app


app = create_app()
