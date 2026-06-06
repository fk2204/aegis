# Audit — Confirmed Bugs (read-only re-verification)

**Main HEAD audited:** `875d08a fix(tampering): backfill replays the EXACT parse-time rule, not the score-only proxy`
**A.1 status:** CONFIRMED-LIVE
**A.2 status:** CONFIRMED-LIVE
**A.3 status:** CONFIRMED-LIVE
**Recommended fix order:** A.3 → A.2 → A.1
**Rationale (one line):** A.3 blocks `tests/web/` collection (192 unrelated tests can't even import), so unblock the test suite first; A.2 is a single-constant edit with a clear policy answer (align pipeline → score, not the other way); A.1 needs a small reshape of the score-side gate and benefits from a working test suite while you touch it.

---

## A.1 — `incremental_pdf_saves` pipeline-vs-score contradiction

### Source paste (current)

`src/aegis/parser/pipeline.py:86` — the relaxed parser threshold:

```python
EOF_HARD_DECLINE: Final[int] = 2
```

`src/aegis/parser/pipeline.py:378-394` — the parse_status decision tree:

```python
if metadata.eof_markers > EOF_HARD_DECLINE:
    return "manual_review"
if metadata.fraud_score >= METADATA_HARD_DECLINE:
    return "manual_review"
if fraud_score >= HARD_DECLINE_THRESHOLD:
    return "manual_review"
if confidence_failures:
    return "manual_review"
if (
    fraud_score >= REVIEW_THRESHOLD
    or not validation.passed
    or metadata.eof_markers > 1
):
    return "review"
return "proceed"
```

`src/aegis/scoring/score.py:162-163` — the score-side hard-decline still on the old bar:

```python
if deal.eof_markers > 1:
    reasons.append(f"incremental_pdf_saves: {deal.eof_markers} EOF markers")
```

### Semantic outcome (eof_markers=2)

The parser pipeline grades `eof_markers=2` as a **`review`** flag (not `manual_review`) — see lines 378 vs 392: `>2` hard-fails, exactly `>1 && ≤2` (i.e. exactly 2) drops to "review". The narrative comment block at `pipeline.py:25-34` explicitly says 2 EOFs is normal for legit online-banking exports and was intentionally demoted.

But `score.py:162` still treats `>1` (i.e. ≥2) as a HARD decline reason. So a deal whose statement has `eof_markers=2`:

- Pipeline `parse_status` = `review` (proceed-with-review).
- Score-side `hard_decline_reasons` contains `incremental_pdf_saves: 2 EOF markers`.
- `score_deal()` short-circuits at line 88-89 and returns `tier="F"`, `recommendation="decline"`, `score=0`.

Result: an operator-facing deal that the parser deliberately allowed into "review" gets stamped tier-F and auto-declined by `score_deal`. The two layers disagree on the policy decision the comment block above `EOF_HARD_DECLINE` says was the intended one.

### Minimal mechanical fix (one paragraph)

Align the score-side bar with the pipeline. Change `score.py:162` from `> 1` to `> EOF_HARD_DECLINE` (i.e. `> 2`) and import `EOF_HARD_DECLINE` from `aegis.parser.pipeline` — or, cleaner, move `EOF_HARD_DECLINE` to a shared `aegis.thresholds` module since both layers need it. The reason-message format already reads "incremental_pdf_saves: N EOF markers" which stays readable at the new bar. Score-side has no equivalent of the pipeline's "review" tier (it goes 0/1), so the `eof_markers=2` case should not trigger a hard decline in either place; ratify the pipeline's policy (the narrative comment is the authoritative source).

### Status

**CONFIRMED-LIVE.** The constant `EOF_HARD_DECLINE=2` was introduced in the pipeline alongside its policy comment, but the score-side gate was not updated to match.

---

## A.2 — fraud_score 65/70 threshold mismatch

### Constants (current)

- `src/aegis/parser/pipeline.py:79`: `HARD_DECLINE_THRESHOLD: Final[int] = 65`
- `src/aegis/scoring/score.py:40`: `FRAUD_SCORE_HARD_DECLINE: Final[int] = 70`

### Trace (fraud_score=67)

Following `pipeline.py:_decide` (line 382): `if fraud_score >= HARD_DECLINE_THRESHOLD:` → `67 >= 65` → returns `"manual_review"`. The parser routes the document to manual review.

Then in scoring: at `score.py:159` the condition is `if deal.fraud_score >= FRAUD_SCORE_HARD_DECLINE:` → `67 >= 70` is **false**. The `fraud_score_critical` reason is NOT appended. The deal goes through soft scoring instead — it can theoretically receive a non-F tier and recommendation `approve` or `refer` based on its other signals, even though the parser-layer decision was `manual_review`.

So a `fraud_score=67` deal lands in the worst kind of split state: parser says manual_review (it can't proceed), score layer says "no hard decline, soft-score normally." Whatever code reads from each layer gets a different answer.

### Mechanical fix (one paragraph)

Align `FRAUD_SCORE_HARD_DECLINE` to `HARD_DECLINE_THRESHOLD` by importing the parser-side constant (`from aegis.parser.pipeline import HARD_DECLINE_THRESHOLD`) or move both to `aegis.thresholds`. Pick the pipeline value (65) — the pipeline's `_decide` is the gate that drives `parse_status` (which the worker uses for routing), and aligning the scorer down to 65 produces a strictly more conservative decline policy, which matches the documented intent at `pipeline.py:18-21` ("hard-decline triggers"). The alternative (raise pipeline to 70) is wrong because the pipeline's compound-signal escalation rules already reference `HARD_DECLINE_THRESHOLD` at three different boundaries (lines 350, 353, 356, 359, 382) and moving it up there silently weakens the escalation ladder.

### Tests that assert on either threshold (cite-only, do not update)

- `tests/scoring/test_score.py:107-112` — `test_fraud_score_critical_declines` uses `fraud_score=75` (passes today; will still pass after lowering to 65).
- `tests/parser/test_pipeline_thresholds.py:21,42,61,70,79,89` — boundary asserts on `HARD_DECLINE_THRESHOLD` (currently 65). These read the constant symbolically, so they auto-track any future value change.

### Status

**CONFIRMED-LIVE.** Both constants are defined as documented above; nothing has converged them.

---

## A.3 — router.py import shadowing (pytest collection flake)

### Exact import line

`src/aegis/api/routes/__init__.py:22`:

```python
from aegis.web import router as web_router
```

### `aegis/web/__init__.py` (whole file, 13 lines)

```python
"""HTMX-based operator dashboard.

Mounted at ``/ui`` by ``aegis.api.app``. Server-rendered Jinja2 +
HTMX partials — no React, no build step. The dashboard's merchant-detail
page provides the audit drill-down required by CLAUDE.md: clicking an
aggregate shows the contributing transactions with page/line refs.
"""

from __future__ import annotations

from aegis.web.router import router

__all__ = ["router"]
```

The intent is clear: `aegis.web.__init__` re-exports the `router` APIRouter object from the `aegis.web.router` submodule. But `aegis.web.router` is *also* the submodule's name. When Python imports `aegis.web` and resolves `aegis.web.router`, the submodule is registered on the parent package's namespace as the attribute `router` (this is standard Python package import behavior — submodules become attributes of the parent on import). Whether the re-exported APIRouter "wins" depends on import order: if `aegis.web.router` (submodule) is imported *after* `aegis.web.__init__` finishes executing line 11, the submodule object overwrites the APIRouter attribute. That is what is happening here.

### Pytest output (tail 40 lines)

```
E   AttributeError: module 'aegis.web.router' has no attribute 'routes'. Did you mean: 'router'?
_____________ ERROR collecting tests/web/test_obligations_page.py _____________
tests\web\test_obligations_page.py:12: in <module>
    from aegis.api.app import create_app
src\aegis\api\app.py:192: in <module>
    app = create_app()
          ^^^^^^^^^^^^
src\aegis\api\app.py:176: in create_app
    app.include_router(r)
.venv\Lib\site-packages\fastapi\applications.py:1550: in include_router
    self.router.include_router(
.venv\Lib\site-packages\fastapi\routing.py:1723: in include_router
    for r in router.routes:
             ^^^^^^^^^^^^^
E   AttributeError: module 'aegis.web.router' has no attribute 'routes'. Did you mean: 'router'?
=========================== short test summary info ===========================
ERROR tests/web/test_close_rescan.py - AttributeError: module 'aegis.web.rout...
ERROR tests/web/test_dossier_pdf.py - AttributeError: module 'aegis.web.route...
ERROR tests/web/test_funder_operator_notes.py - AttributeError: module 'aegis...
ERROR tests/web/test_funder_reextract.py - AttributeError: module 'aegis.web....
ERROR tests/web/test_funder_submit_modal.py - AttributeError: module 'aegis.w...
ERROR tests/web/test_funder_submit_modal.py - AttributeError: module 'aegis.w...
ERROR tests/web/test_obligations_page.py - AttributeError: module 'aegis.web....
ERROR tests/web/test_upload_auto_create.py - AttributeError: module 'aegis.we...
!!!!!!!!!!!!!!!!!!! Interrupted: 7 errors during collection !!!!!!!!!!!!!!!!!!!
=================== 192 tests collected, 7 errors in 4.80s ====================
```

The error reproduces consistently on every collect run, not flakily. FastAPI's `include_router` tries `router.routes` on the `web_router` value, which has resolved to the `aegis.web.router` *module* (no `.routes` attribute), not the APIRouter object. Calling it "flake" in the prior brief understated it: this is a deterministic failure right now.

### Mechanical fix (one paragraph)

Change `aegis/api/routes/__init__.py:22` from `from aegis.web import router as web_router` to `from aegis.web.router import router as web_router`. This binds `web_router` directly to the APIRouter object at the moment of import, bypassing the submodule-vs-attribute race in `aegis.web`'s namespace. It is the smaller diff (one line, one site), preserves the `aegis.web.router` filename (no stale imports anywhere else in the repo), and makes the dependency explicit. Renaming `aegis/web/router.py` → `aegis/web/_router.py` would also work but requires an audit of every importer and touches the dashboard layer for a fix whose root cause is in the API wiring layer.

### Status

**CONFIRMED-LIVE.** 7 collection errors out of 199 tests in `tests/web/`, deterministically reproduced via `pytest --collect-only tests/web/`.

---

## Recommended fix order (rationale)

**A.3, then A.2, then A.1.** A.3 is blocking 7 web-layer test files from even being collected, which means the parser-layer test suite is the only thing currently giving signal — fix A.3 first so the test gate is whole again before touching scoring/pipeline behavior. A.2 is a single-constant alignment with an unambiguous policy answer (the pipeline value already drives the parse_status gate; the scorer should follow), and it doesn't require any reshape of the gating logic — just an import or a thresholds module. A.1 last because it is the most semantically subtle (it requires picking the right authority between the pipeline's relaxed-to-2 narrative and the scorer's still-on-1 inherited bar), and being able to add a regression test for the eof_markers=2 case via the now-collectable test suite (post-A.3) plus the now-aligned thresholds (post-A.2) makes the change safer.
