"""API route packages.

Routes are mounted on ``aegis.api.app.create_app`` via ``include_router``.
Add new route modules here so the wiring stays in one place.
"""

from __future__ import annotations

from aegis.api.routes.deals import router as deals_router
from aegis.api.routes.disclosures import router as disclosures_router
from aegis.api.routes.findings import router as findings_router
from aegis.api.routes.funders import router as funders_router
from aegis.api.routes.merchants import router as merchants_router
from aegis.api.routes.transactions import router as transactions_router
from aegis.api.routes.upload import router as upload_router
from aegis.api.routes.webhooks_zoho import router as webhooks_zoho_router
from aegis.web import router as web_router

ALL_ROUTERS = (
    upload_router,
    merchants_router,
    transactions_router,
    funders_router,
    disclosures_router,
    deals_router,
    findings_router,
    webhooks_zoho_router,
    web_router,
)


__all__ = [
    "ALL_ROUTERS",
    "deals_router",
    "disclosures_router",
    "findings_router",
    "funders_router",
    "merchants_router",
    "transactions_router",
    "upload_router",
    "web_router",
    "webhooks_zoho_router",
]
