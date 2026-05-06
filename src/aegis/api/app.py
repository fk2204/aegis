"""FastAPI app entry point.

Importing this module triggers `get_settings()` so the data-residency boot
guard runs before the server accepts traffic. /healthz is auth-free; all
future routes will depend on `require_bearer`.
"""

from __future__ import annotations

from fastapi import FastAPI

from aegis.config import get_settings


def create_app() -> FastAPI:
    get_settings()  # boot guard

    app = FastAPI(
        title="AEGIS",
        version="2.0.0",
        description="MCA underwriting brain for Commera Capital.",
    )

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    return app


app = create_app()
