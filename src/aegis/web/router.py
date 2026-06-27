"""Operator dashboard aggregator — wires every ``/ui`` sub-router.

R4.1 status (COMPLETE)
----------------------
All twelve domain sub-routers live under ``aegis.web.routers.*``:

  * ``close_queue.py``        — ``GET /ui/close-queue``
  * ``compliance.py``         — ``POST /ui/decisions/{id}/override``,
                                ``GET  /ui/compliance/obligations``
  * ``dashboard.py``          — ``GET /ui/``, ``GET /ui/review``,
                                ``GET /ui/deals``
  * ``disclosure_events.py``  — ``GET /ui/disclosure-events`` (+ detail)
  * ``documents.py``          — ``GET /ui/documents/{id}`` (+ aggregate partial)
  * ``funders.py``            — ``/ui/funders`` (list / new / import / detail /
                                submit-modal / reextract-modal / reextract /
                                operator-notes)
  * ``intake.py``             — ``GET /ui/intake``, ``POST /ui/intake``
  * ``merchants.py``          — ``GET /ui/merchants``, ``/ui/merchants/new``,
                                ``/ui/merchants/{id}`` (detail / edit / dossier /
                                match / submit / funder-response / close-rescan /
                                findings.csv)
  * ``portfolio.py``          — ``GET /ui/portfolio``
  * ``renewals.py``           — ``GET /ui/renewals``, ``POST /ui/renewals/{id}/attest``
  * ``triage.py``             — ``GET /ui/triage``, ``GET /ui/shadow-signals``
  * ``upload.py``             — ``GET /ui/upload``, ``POST /ui/upload``

Templates singleton lives in ``aegis.web._templates``. Shared module-level
constants/helpers live in ``aegis.web._router_helpers``. Tests + other
code import private helpers (``_classify_close_pipeline_state``,
``templates``, ``_state_tier``, ``_compute_merchant_tier``,
``_ofac_ribbon_status``, ``_match_card``, ``_build_attention_groups``,
``_build_review_queue_cards``, ``_dossier_pattern_analysis``,
``_collect_analyzed_for_merchant``, ``_bundle_keys_for_merchant``,
``_select_default_bundle``, ``_score_input_from_dashboard``) from
``aegis.web.router`` — those re-exports MUST stay (see ``__all__``
at end of file).

Auth note
---------
The dashboard intentionally does NOT require the bearer token: in
production it sits behind Cloudflare Access (SSO + JWT). The bearer
token guards programmatic API endpoints, not the operator UI. In a
local dev box without Cloudflare in front, the dashboard is reachable
on localhost only.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from aegis.web._role_gate import current_operator

# R4.1 — bundling / score-input helpers + form helpers + aggregate
# metadata constants live in ``aegis.web._router_helpers``. Re-exported
# below for the tests + ``aegis.api.routes.findings`` integration that
# pull these from ``aegis.web.router``.
from aegis.web._router_helpers import (
    _bundle_keys_for_merchant,
    _collect_analyzed_for_merchant,
    _score_input_from_dashboard,
    _select_default_bundle,
)

# Templates singleton + Jinja filter registrations moved to
# ``aegis.web._templates`` during R4.1 so sub-routers can share without
# importing this aggregator. ``templates`` is re-exported below so
# existing imports (``from aegis.web.router import templates``) still work.
from aegis.web._templates import templates

# R4.1 — domain sub-routers. Each module declares its own ``router =
# APIRouter()`` (no prefix; the aggregator below carries ``/ui``) and is
# wired in via ``include_router`` below.
from aegis.web.routers import admin as _admin_routes
from aegis.web.routers import bank_layouts as _bank_layouts_routes
from aegis.web.routers import calibration as _calibration_routes
from aegis.web.routers import close_queue as _close_queue_routes
from aegis.web.routers import compliance as _compliance_routes
from aegis.web.routers import dashboard as _dashboard_routes
from aegis.web.routers import disclosure_events as _disclosure_events_routes
from aegis.web.routers import documents as _documents_routes
from aegis.web.routers import funder_replies as _funder_replies_routes
from aegis.web.routers import funders as _funders_routes
from aegis.web.routers import intake as _intake_routes
from aegis.web.routers import merchants as _merchants_routes
from aegis.web.routers import portfolio as _portfolio_routes
from aegis.web.routers import renewals as _renewals_routes
from aegis.web.routers import shadow_review as _shadow_review_routes
from aegis.web.routers import submissions as _submissions_routes
from aegis.web.routers import triage as _triage_routes
from aegis.web.routers import upload as _upload_routes

# Re-export so tests/test_close_queue.py keeps its
# ``from aegis.web.router import _classify_close_pipeline_state`` import path.
from aegis.web.routers.close_queue import _classify_close_pipeline_state

# Re-export so tests still pulling these from ``aegis.web.router`` keep
# their import paths working after the R4.1 dashboard split:
#   * tests/web/test_attention_groups.py
#   * tests/web/test_review_queue_cards.py
#   * tests/test_merchant_status_guards.py
from aegis.web.routers.dashboard import (
    _build_attention_groups,
    _build_review_queue_cards,
    _compute_merchant_tier,
)

# Re-export so tests still pulling these from ``aegis.web.router`` keep
# their import paths working after the R4.1 merchants split:
#   * tests/test_merchant_status_guards.py — _state_tier, _ofac_ribbon_status
#   * tests/web/test_match_card_color_rule.py — _match_card
#   * tests/web/test_dossier_pattern_analysis_source.py — _dossier_pattern_analysis
from aegis.web.routers.merchants import (
    _dossier_pattern_analysis,
    _match_card,
    _ofac_ribbon_status,
    _state_tier,
)

# Router-level dependency: every ``/ui/...`` request resolves the
# current operator before any route runs. Side-effect populates
# ``request.state.operator`` so the shared topstrip can render the
# operator name + role chip without each route plumbing the value
# through the template context.
router = APIRouter(
    prefix="/ui",
    tags=["dashboard"],
    dependencies=[Depends(current_operator)],
)

router.include_router(_admin_routes.router)
router.include_router(_bank_layouts_routes.router)
router.include_router(_calibration_routes.router)
router.include_router(_close_queue_routes.router)
router.include_router(_compliance_routes.router)
router.include_router(_dashboard_routes.router)
router.include_router(_documents_routes.router)
router.include_router(_funder_replies_routes.router)
router.include_router(_funders_routes.router)
router.include_router(_intake_routes.router)
router.include_router(_merchants_routes.router)
router.include_router(_renewals_routes.router)
router.include_router(_portfolio_routes.router)
router.include_router(_shadow_review_routes.router)
router.include_router(_submissions_routes.router)
router.include_router(_disclosure_events_routes.router)
router.include_router(_triage_routes.router)
router.include_router(_upload_routes.router)


# R4.1 — re-exports required for `from aegis.web.router import X` callers
# in tests + other modules. Keep aligned with the imports at the top of
# the file.
__all__ = [
    "_build_attention_groups",
    "_build_review_queue_cards",
    "_bundle_keys_for_merchant",
    "_classify_close_pipeline_state",
    "_collect_analyzed_for_merchant",
    "_compute_merchant_tier",
    "_dossier_pattern_analysis",
    "_match_card",
    "_ofac_ribbon_status",
    "_score_input_from_dashboard",
    "_select_default_bundle",
    "_state_tier",
    "router",
    "templates",
]
