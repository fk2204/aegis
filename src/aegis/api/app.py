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

from aegis.api.auth import (
    warn_if_bearer_unconfigured,
    warn_if_close_callback_token_unconfigured,
)
from aegis.api.boot_guards import assert_uvicorn_loopback_bind
from aegis.api.deps import get_merchant_repository, get_repository
from aegis.api.routes import ALL_ROUTERS
from aegis.compliance.anti_drift import run_boot_checks
from aegis.compliance.state_matrix import load_matrix
from aegis.compliance.states import validate_states_table
from aegis.config import get_settings
from aegis.logger import configure_logging, get_logger
from aegis.merchants.repository import MerchantRepository
from aegis.ops.rate_limit import RateLimitMiddleware
from aegis.storage import DocumentRepository

_log = get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging()
    # PDF retention chunk A — boot guards. Run BEFORE anything else
    # touches the network or schema so a misconfigured deployment
    # refuses to start instead of leaking PDFs or accepting traffic
    # on an interface it shouldn't be on.
    #
    # 1. uvicorn must be bound to loopback (regression guard against a
    #    future systemd unit edit that flips --host to 0.0.0.0 and
    #    silently reopens the PDF-exfil hole guarded by the CF tunnel).
    # 2. Supabase document bucket must exist and be private (the
    #    encrypted-PDF persistence layer assumes service_role-only
    #    access).
    assert_uvicorn_loopback_bind()
    # Bucket assertion. Three "cannot determine" outcomes (unreachable,
    # absent, auth) WARN + audit + proceed per the operator-required
    # chunk-A refinement — a Supabase outage during a routine restart
    # must not brick the web tier. Verified-public is the only
    # refuse-boot branch.
    #
    # Lazy import: storage_objects only pulls supabase-py when the
    # supabase backend is selected.
    from aegis.api.deps import get_audit
    from aegis.storage_objects import assert_bucket_private_at_startup

    assert_bucket_private_at_startup(audit=get_audit())
    validate_states_table()  # legacy boot-time fail-closed compliance check
    # mp Phase 1: load + validate states.yaml; fail closed on drift.
    app.state.state_matrix = load_matrix()
    _log.info(
        "api.lifespan.state_matrix_loaded",
        extra={
            "matrix_version": app.state.state_matrix.version,
            "state_count": len(app.state.state_matrix.states),
        },
    )
    # mp Phase 3: anti-drift (template SHA256, overdue reviews). Runs after
    # load_matrix() so the matrix is available for the template-SHA scan.
    run_boot_checks()
    warn_if_bearer_unconfigured()  # once-per-process operator visibility
    warn_if_close_callback_token_unconfigured()  # /api/close-callback/* fail-closed

    # Detect lingering ZOHO_* env vars after the Close cutover (step 9).
    # Non-fatal: AEGIS still boots, but the operator gets a structured
    # WARN + audit row so /etc/aegis/aegis.env on Hetzner can be cleaned
    # up to match the codebase state.
    from aegis.api.deps import get_audit
    from aegis.config import warn_if_zoho_env_lingers

    warn_if_zoho_env_lingers(audit=get_audit())

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

    # Per-IP + per-bearer rate limiting (mp Phase 11 task #3). Applied
    # BEFORE routes so 429s are returned without hitting auth or route
    # handlers. /healthz is exempt; see aegis.ops.rate_limit.
    # The starlette stub for add_middleware doesn't model **kwargs
    # ergonomically; cast to Any to keep the runtime contract clear
    # without a long Protocol shim.
    from typing import Any, cast

    app.add_middleware(cast("Any", RateLimitMiddleware))

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
        # Entry point for the CRM "View in Aegis" Lead button. The
        # button was originally configured in Zoho and is being
        # reconfigured in Close (same email-based lookup pattern).
        # Routes the operator to the right surface: merchant detail
        # if statements exist, dashboard otherwise (including unknown
        # email).
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
