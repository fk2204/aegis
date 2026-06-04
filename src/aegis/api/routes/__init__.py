"""API route packages.

Routes are mounted on ``aegis.api.app.create_app`` via ``include_router``.
Add new route modules here so the wiring stays in one place.
"""

from __future__ import annotations

from aegis.api.routes.audit import router as audit_router
from aegis.api.routes.close_callback import router as close_callback_router
from aegis.api.routes.deals import router as deals_router
from aegis.api.routes.disclosures import router as disclosures_router
from aegis.api.routes.documents import router as documents_router
from aegis.api.routes.findings import router as findings_router
from aegis.api.routes.funder_replies import router as funder_replies_router
from aegis.api.routes.funders import router as funders_router
from aegis.api.routes.merchants import router as merchants_router
from aegis.api.routes.transactions import router as transactions_router
from aegis.api.routes.upload import router as upload_router
from aegis.api.routes.upload import uploads_router
from aegis.api.routes.webhooks_close import router as webhooks_close_router

# Direct submodule attribute access — ``from aegis.web import router``
# binds to whatever ``aegis.web.router`` resolves to at import time,
# which races the submodule registration (Python registers
# ``aegis.web.router`` as an attribute of ``aegis.web`` when the
# submodule is loaded, potentially overwriting the APIRouter object
# re-exported from ``aegis/web/__init__.py``). The race surfaced as
# 7 collection errors in ``tests/web/`` whenever pytest's import
# order touched the submodule before the package re-export. Direct
# attribute access on the submodule eliminates the ambiguity.
from aegis.web.router import router as web_router

ALL_ROUTERS = (
    upload_router,
    uploads_router,
    merchants_router,
    transactions_router,
    funders_router,
    funder_replies_router,
    disclosures_router,
    deals_router,
    documents_router,
    findings_router,
    audit_router,
    webhooks_close_router,
    close_callback_router,
    web_router,
)


__all__ = [
    "ALL_ROUTERS",
    "audit_router",
    "close_callback_router",
    "deals_router",
    "disclosures_router",
    "documents_router",
    "findings_router",
    "funder_replies_router",
    "funders_router",
    "merchants_router",
    "transactions_router",
    "upload_router",
    "uploads_router",
    "web_router",
    "webhooks_close_router",
]
