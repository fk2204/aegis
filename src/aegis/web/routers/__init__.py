"""Domain-organized sub-routers for the operator dashboard.

The parent ``aegis.web.router`` aggregator wires the ``/ui`` prefix and
``tags=["dashboard"]`` once. Each sub-router declares ``router = APIRouter()``
with NO prefix so route paths declared inside (e.g. ``@router.get("/merchants")``)
land at ``/ui/merchants`` after aggregation. See router.py header for
the full migration map.
"""
