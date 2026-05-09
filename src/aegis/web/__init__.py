"""HTMX-based operator dashboard.

Mounted at ``/ui`` by ``aegis.api.app``. Server-rendered Jinja2 +
HTMX partials — no React, no build step. The dashboard's merchant-detail
page provides the audit drill-down required by CLAUDE.md: clicking an
aggregate shows the contributing transactions with page/line refs.
"""

from __future__ import annotations

from aegis.web.router import router

__all__ = ["router"]
