# Track A latent code audit — 2026-06-12

**Scope:** `src/aegis/scoring_v2/track_a/` + adjacent (`aggregation.py`,
`shadow_disagreements.py`, tampering feed-in, dossier-panel wiring,
`score_deal_inputs.py`, `track_a_historical_lookback.py`). Read-only
walk; no code changes. Punch-list for operator triage.

**Status legend:** hard fix needed = HARD / worth fixing = WORTH /
informational = INFO.

---

## Summary

11 findings: **2 HARD**, **6 WORTH**, **3 INFO**. The track_a module
is small, deliberate, and well-documented; the headline branch logic
matches the Q2 spec verbatim and the two real-merchant fixtures
(A&R KM, VU 7722) pin the load-bearing distinguisher (editor flag
presence → drift_plus_editor vs drift_alone) byte-for-byte against
the persisted prod payloads.

Biggest latent risk: the verdict path silently downgrades to
``None`` on any exception (in
``compute_score_deal_track_inputs`` and ``_track_inputs_for_deal``),
which makes a Pydantic ``ValidationError`` in the `rationale`
``max_length=320`` field (F1) invisible — the deal would just run on
legacy fraud_score instead of failing loud. Together with two test
boundary gaps (F3) and one evidence-completeness gap (F2), these are
the items worth landing before the Step 2 cutover gates close.

---

## Findings

### [F1] `rationale` max_length=320 can be exceeded by long editor strings → silent verdict downgrade
**Status:** HARD
**Files:** `src/aegis/scoring_v2/track_a/models.py:172-180`,
`src/aegis/scoring_v2/track_a/framing.py:19-30`,
`src/aegis/scoring_v2/score_deal_inputs.py:112-129`

**What's wrong:** `IntegrityVerdict.rationale` is constrained to
`max_length=320` (Pydantic Field). `frame_drift_plus_editor` produces
a static ~264-char body PLUS `short_editor` (the editor flag tail,
stripped of the `editor_detected: ` prefix). The editor flag string
is not length-bounded upstream — `metadata.flags` is a free-form list
from `analyze_metadata`. A flag like `editor_detected: Some Long
Vendor Tool Name with Version 11.2.3.42 (Build 8a7c) by Producer
GmbH` (~80 chars) pushes the rationale past 320 → Pydantic raises
`ValidationError` inside `compute_integrity_verdict` →
`compute_score_deal_track_inputs` (line 118) catches generic
`Exception`, logs a warning, returns `(None, None)` → `score_deal`
falls back to the legacy engine for that deal.

**Failure mode:** A real merchant with a verbose editor signature
silently bypasses Track A's `fail` gate even after Step 2 cutover.
The "Track A is the gate" guarantee leaks: no audit row, no UI
indicator, just a quietly missed catch. Exactly the pattern CLAUDE.md
"audit-write failures fail the operation, never silently log-and-
continue" was written to prevent — but the exception swallowing is
intentional (don't break scoring), so the discipline rule has to be
re-asserted at the rationale-builder boundary.

**Suggested fix:** either (a) truncate `short_editor` to 60 chars in
`framing.py` with an explicit ellipsis, or (b) lift the `rationale`
`max_length` to 480-512 to absorb foreseeable editor tails. (a) is
simpler and keeps the verdict serialised at a known max byte cost.
Independently, the catch-all `except Exception` in
`score_deal_inputs.py:118` should distinguish Pydantic
`ValidationError` from genuine input crashes and surface the former
as a critical log+audit (it's a code bug, not a data oddity).

**Test gap:** yes. No test exercises rationale length boundary on
any branch; add a property test that fuzzes `editor_detected: …`
tails up to 240 chars (the EvidenceItem.detail max) and asserts
`compute_integrity_verdict` returns without raising.

---

### [F2] Strong-metadata fail (branch 1) drops drift evidence
**Status:** HARD
**Files:** `src/aegis/scoring_v2/track_a/compute.py:73-100`

**What's wrong:** When `metadata_score >= 50` AND reconciliation drift
is also present, branch 1 fires and emits evidence: `metadata_score`
row + `editor_detected` (if any) + per-flag rows from
`other_meta`. The `drift_failures` list — already computed at line
69 — is NEVER added to the evidence. The fail verdict is correct,
but the underwriter looking at the dossier sees only "metadata score
72" without the corroborating math failure that would tell them this
isn't just a Foxit-export false positive.

**Failure mode:** Underwriter overrides what they perceive as a
metadata-only fail ("just Foxit, probably innocuous, override to
review") without seeing the reconciliation_failed_period that
genuinely corroborates. This is exactly the H10 / VU shape the
discipline rule warned about — but in reverse: hiding a corroborating
signal lets a human soften a fail it shouldn't soften. Discipline
rule "no track-tuning to pass a specific merchant" applies equally
to UI-level evidence hiding.

**Suggested fix:** add the drift_failures loop to branch 1's evidence
build (mirror branch 2's pattern, lines 108-114). Update the strong-
metadata acceptance test to assert that when drift is present
alongside `metadata_score >= 50`, drift evidence surfaces.

**Test gap:** yes. `test_strong_metadata_branch_fires_at_score_
threshold` uses `validation_failures=()` — never tests the both-
present case.

---

### [F3] Boundary cases not exercised: `metadata_score = 49`, `= 50`, `= 25`, `= 24`, `= 0`
**Status:** WORTH
**Files:** `tests/scoring_v2/track_a/test_integrity_verdict.py:35-181`

**What's wrong:** The acceptance tests use scattered scores (8, 72,
12, 37, 38, 10, 22) but never test the exact branch-transition
boundaries. Parser's `test_tampering.py` exercises 49 and 50; Track A
does not. The same `_STRONG_METADATA_FLOOR=50` and
`_MEDIUM_METADATA_FLOOR=25` / `_CEIL=49` thresholds in
`compute.py:57-59` are duplicated (intentionally — see the design
note) but the duplication's discipline ("they MUST stay in sync") is
only verified at the value level
(`test_track_a_thresholds_match_parser_tampering`), not at the
behavior level (does `score=50` actually fire branch 1 in Track A?).

**Failure mode:** A future refactor that nudges one threshold by 1
(off-by-one mistake during a guard test edit) ships a behavioral
regression caught only by manual triage of the historical lookback.
With Step 2 cutover gated on lookback exit-0, this could turn into a
real false-clean → false-clear in production.

**Suggested fix:** add explicit boundary tests:
- `metadata_score=50` + nothing else → fail / `strong_metadata`
- `metadata_score=49` + drift → review / `medium_corroborated`
- `metadata_score=49` + editor + drift → fail / `drift_plus_editor`
- `metadata_score=25` + drift → review / `medium_corroborated`
- `metadata_score=24` + drift → review / `drift_alone`
- `metadata_score=0` + drift → review / `drift_alone`
- `metadata_score=100` + nothing → fail / `strong_metadata`

**Test gap:** yes (the entire boundary band).

---

### [F4] Editor flag dual-location: present in both `metadata_flags` AND `[META] all_flags` but only one path is checked
**Status:** WORTH
**Files:** `src/aegis/scoring_v2/track_a/signals.py:77-95`,
`src/aegis/scoring_v2/dossier_panel.py:111-131`

**What's wrong:** `_collect_flags` in `parser/pipeline.py:602` writes
every metadata flag into `all_flags` with `[META]` prefix —
including `editor_detected: …`. Track A's
`extract_editor_metadata_flag` reads only `metadata_flags` (the raw
column, no prefix), never inspects the `validation_failures`
parameter (which `dossier_panel._signals_for_document` populates from
`all_flags`). If a persistence path ever writes `all_flags` but
leaves `metadata_flags` empty (re-parse race, partial-update bug,
historical row from a legacy migration), the editor flag is invisible
to Track A → drift_plus_editor → drift_alone downgrade → fail →
review.

**Failure mode:** A document persisted by an older pipeline version
(or a partial re-parse) silently downgrades fail → review. The
operator sees "drift_alone, needs OCR review" instead of "drift +
editor, hard fail." Probability is low (current parser writes both
columns atomically in `storage.py:371-372`), but the asymmetry is a
latent footgun for any future code that round-trips through `all_flags`
only.

**Suggested fix:** make `extract_editor_metadata_flag` also scan
`validation_failures` for `[META] editor_detected: …` (after prefix
strip). This is a one-line change in `signals.py` (add a fallback
scan when `metadata_flags` yields nothing) and matches the existing
"tolerate both raw + persisted formats" discipline the helpers
already follow for the drift family.

**Test gap:** yes. Add a fixture where `metadata_flags=()` and
`validation_failures` contains `[META] editor_detected: iText 2.1.7`
+ drift; assert drift_plus_editor fires.

---

### [F5] Stale comment: `compute_bundle_verdict` referenced but never implemented
**Status:** WORTH
**Files:** `src/aegis/scoring_v2/track_a/models.py:78-85`

**What's wrong:** The `DocumentIntegritySignals.document_id` docstring
says "a bundle-level rollup is computed separately by
`compute_bundle_verdict`." Grep confirms `compute_bundle_verdict` is
defined nowhere in the repo. The bundle-level rollup is in fact
performed by `dossier_panel._summarise_verdicts` and
`score_deal_inputs._worst_integrity_verdict`. Two unrelated functions
do roughly the same job under different names, neither matches the
docstring.

**Failure mode:** A reader trusts the docstring, greps for
`compute_bundle_verdict`, finds nothing, wastes 10 minutes confirming
the function isn't elsewhere. Not load-bearing but discipline rule
"stale comments are worse than no comments" applies.

**Suggested fix:** update the docstring to either name
`_worst_integrity_verdict` and `_summarise_verdicts` explicitly, or
extract the rollup into `track_a/compute.py::compute_bundle_verdict`
to satisfy the existing API contract and pull both call sites onto
it. Operator judgment: the latter consolidates two near-duplicate
"worst-of" implementations; the former is the cheap doc-only fix.

**Test gap:** no (function doesn't exist; can't be tested).

---

### [F6] Stale comment: `future_dated_period:` doesn't exist as a real flag
**Status:** WORTH
**Files:** `src/aegis/scoring_v2/track_a/models.py:108-112`

**What's wrong:** `validation_failures` field docstring lists
`'future_dated_period: …'` as an example, and
`signals.py:5` lists `future_dated_*:` prefixes. Real flag from
`parser/validate.py:170` is `future_dated: period_end=…` (no `_period`
suffix). Lookup still matches because `DRIFT_FAILURE_PREFIXES =
("reconciliation_failed", "future_dated")` is a prefix check —
correct — but the example in the docstring is fictional.

**Failure mode:** Documentation-only; the prefix match still works.
A future author copy-pasting the docstring example into a fixture
ships a fictional flag that wouldn't pass canary review.

**Suggested fix:** correct the docstring example to
`'future_dated: period_end=2026-07-01 today=2026-06-12'`.

**Test gap:** no (behavior is correct).

---

### [F7] Dead defensive code: `getattr(d, "uploaded_at", None) or ""`
**Status:** INFO
**Files:** `src/aegis/scoring_v2/dossier_panel.py:193-197`

**What's wrong:** `DocumentRow.uploaded_at` is typed as `datetime`
(required, non-optional — `storage.py:99`). The
`getattr(…, None) or ""` fallback can never trigger on a real
`DocumentRow`; if it ever did, sorting a list of datetimes alongside
an empty string would raise `TypeError`. The test-side
`_document_row_stub` passes `uploaded_at="2026-06-01T00:00:00Z"` (a
string) — string-string comparison works, masking the type
inconsistency.

**Failure mode:** none currently. The defensive fallback hides the
test/prod type drift; a real `None` would crash sorting rather than
silently mis-ordering.

**Suggested fix:** drop the `or ""` and let `getattr` return the
`datetime`; let mypy catch any caller that passes a string. Or, if
the stub pattern is too useful to give up, type the stub explicitly
as `Any` with a comment.

**Test gap:** no.

---

### [F8] Branch 1 emits `metadata_flag` evidence for `other_meta` but doesn't deduplicate against `editor_detected`
**Status:** INFO
**Files:** `src/aegis/scoring_v2/track_a/compute.py:86-92`

**What's wrong:** `extract_other_metadata_flags` is correctly "non-
editor" only (filters by `_EDITOR_FLAG_PREFIX`), so the strong-
metadata branch's evidence won't accidentally duplicate the editor
flag as both `editor_detected` AND `metadata_flag`. Code is correct;
worth noting only because the `signal=` token differs between
branches (`metadata_flag` vs the more specific token branch 2 emits)
— evidence rows about the SAME flag look different depending on which
branch fired. A dossier-side filter "show me all editor_detected
evidence" works because of the dedup; a "show me all
metadata_flag evidence" filter wouldn't surface the editor flag from
branch 1 (which is correct, but counterintuitive).

**Failure mode:** UI signal-class filtering is mildly inconsistent
across branches. Not load-bearing.

**Suggested fix:** none required — flag for future operator UI work
that wants per-signal filtering across branches.

**Test gap:** no.

---

### [F9] VU 7722 fixture is single-month; the documented signature is "3 of 4 months"
**Status:** INFO
**Files:** `tests/scoring_v2/track_a/test_integrity_verdict.py:282-302`,
`docs/REMAINING_WORK.md:90-91`

**What's wrong:** `REMAINING_WORK.md` describes VU 7722 as "Running-
balance drift $5 / $11 / $55 across 3 of 4 months." The Track A
fixture asserts on one statement (Feb 2026, $55 drift) only. Track A
is per-document, so this is structurally correct — but the
multi-month pattern is what makes VU 7722 worth catching; a
single-month $55 drift is genuinely OCR-band and the `drift_alone`
review verdict matches it. The pattern-level "3 of 4 months drift"
detection is Track B / pattern-detector territory, NOT Track A.
Worth confirming the boundary explicitly because the test name
(`test_vu_7722_verdict_is_review_drift_alone`) reads as if Track A
owns the VU case end-to-end.

**Failure mode:** A future operator triaging the lookback may
conclude Track A "caught" VU 7722 because the fixture passes, then
be surprised when only 1 of the 4 docs surfaces a verdict at all (the
other 3 statements would clean-pass because Track A is per-doc and
the drift signal isn't structurally correlated across the bundle).
Not a bug; a clarity gap.

**Suggested fix:** add 3 more fixtures (Jan/Mar/Apr 2026) each with
their respective drift magnitudes, and assert each individually
produces `drift_alone` review. Or add a comment to the existing
fixture noting Track A is per-doc and the bundle-level "3 of 4"
pattern lives elsewhere.

**Test gap:** partial (no per-doc coverage of the other 3 months).

---

### [F10] `_drift_signal_token` returns the prefix before `:` — robust to whitespace edge cases?
**Status:** INFO
**Files:** `src/aegis/scoring_v2/track_a/compute.py:198-207`

**What's wrong:** `head = _strip_category_prefix(failure).split(":",
1)[0]`. If a flag arrives with leading whitespace
(`" reconciliation_failed_period: …"`), `_strip_category_prefix`
doesn't trim and the split returns `" reconciliation_failed_period"`
— the signal token has a leading space. Won't break the verdict
(token is informational), but dossier-side filtering keyed off the
token would miss the row. The pipeline writes flags clean
(`f"[MATH] {f}"` — no extra spaces), so probability is low. Worth a
`.strip()` for defense in depth.

**Failure mode:** Future drift in flag formatting (e.g. a
hand-written entry via an admin tool) leaks whitespace into the
dossier filter index.

**Suggested fix:** `.strip()` after `_strip_category_prefix` in both
`_drift_signal_token` and the `extract_*` helpers.

**Test gap:** no (current emitters always produce clean flags).

---

### [F11] Shadow-disagreement evidence-hash idempotency hashes `None` as `"null"` — collision with empty dict?
**Status:** INFO
**Files:** `src/aegis/scoring_v2/shadow_disagreements.py:233-247`

**What's wrong:** `_canonical_evidence_json(None)` returns `"null"`.
`_canonical_evidence_json({})` returns `"{}"`. Hashes are distinct.
Comment at line 240-241 explicitly documents this: "the hash
distinguishes 'no evidence captured' from 'empty dict'." Correct, no
issue — but worth surfacing in the audit because the same anchor is
used to dedupe Track A categorisations across nightly runs; if the
comparison script ever emits a categorisation without explicit
evidence (literal `None`) AND a different categorisation later emits
`{}` for the same merchant/day/category, both rows persist. Code is
correct; the surface is brittle to comparison-script changes.

**Failure mode:** A categorisation refactor that switches `None` →
`{}` (or vice versa) makes one calendar day's runs spawn duplicate
rows until the next day. Not load-bearing — operator sees both rows
in the triage queue.

**Suggested fix:** none required — flag for documentation in
`docs/REMAINING_WORK.md` under "shadow-disagreement nuances" if /
when the comparison script's evidence shape changes.

**Test gap:** no.

---

## What ISN'T a finding (worth recording)

- **Branch precedence is correct and well-tested.** The 5-branch
  decision tree in `compute.py` matches the Q2 spec verbatim, and
  every branch has at least one dedicated test
  (`test_integrity_verdict.py:35-181`).
- **No coupling violations.** Track A imports nothing from Track B,
  Track C, or `aggregation.py`. Track B/C import from
  `aggregation.py` but never from `track_a`. The discipline rule
  "three tracks stay separate forever" holds at the import graph.
- **No decline-field leakage.** `IntegrityVerdict.model_fields` is
  guarded by `test_verdict_has_no_decline_or_score_field`; the
  schema cannot grow a `decline`/`score`/`outcome` field without the
  test failing. Same guard applies to `UnifiedTracksView` in
  `test_dossier_panel.py`.
- **Threshold sync with parser tampering is pinned.**
  `test_track_a_thresholds_match_parser_tampering` reads the parser's
  constants directly; any drift fails at test time. (Behavior-level
  boundary coverage is the F3 gap, but the constants stay in sync.)
- **Track A is not wired into the live decline path.**
  `score.py:294` runs the legacy `fraud_score >= 65` rule under the
  default `engine == "legacy"`; Track A is consumed only under
  `engine == "track_abc"`. Step 2 cutover gates (lookback exit-0,
  zero un-reviewed `old-caught-something-new-misses`, deliberate flip
  via env var) remain in front of any production behavior change.
- **A&R KM vs VU 7722 ground-truth alignment.** Fixtures are
  byte-verbatim from the live prod payloads
  (`test_integrity_verdict.py:262-280, 293-302` — pulled
  2026-06-05). The A&R KM `metadata_score=0` quirk
  (editor flag fires but persisted score still 0) is explicitly
  asserted as the load-bearing point that Track A's drift_plus_editor
  branch reads the flag PRESENCE, not the score. Branch + verdict
  match the documented per-merchant outcomes.

## Outside scope but flagged

- **`scripts/track_a_historical_lookback.py:95-108`'s
  `_extract_math_failures` only strips `[MATH] ` prefix** — so a
  flag like `[META] future_dated: …` would not be forwarded to Track
  A's input even though Track A's
  `extract_drift_failures` recognises `future_dated`. The persisted
  pipeline writes `future_dated` as a `[MATH]` (from
  `validation.failures`) not `[META]`, so this is currently aligned
  by coincidence. If anything ever moves `future_dated` into
  `metadata.flags`, the lookback silently drops it. Worth a comment
  in the lookback script asserting the source-of-truth boundary.

- **`compute_score_deal_track_inputs` catches generic `Exception`**
  (`score_deal_inputs.py:118`). Documented as intentional safety
  guard. Combined with F1, this swallows what could be a code-bug
  signal (Pydantic ValidationError on rationale length). The fallback
  shape is correct; the lack of differentiation between data-shape
  bugs and runtime data oddities is worth one structured log line at
  WARN+1 severity (separate signal types) — feature-flag-able if the
  operator wants the alarm threshold tuned.
