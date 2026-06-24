# Step 2 cutover — regression-review template

Plan 4.3. This file is the operator's triage workspace for the
miss / disagreement rows surfaced by Wave 4's pre-cutover diagnostics.
Every row in either source script's output that this document does
not categorise blocks the Step-2 flip. Zero un-reviewed rows is the
bar — not "zero misses." See `docs/REMAINING_WORK.md` Wave 4 gating
conditions.

---

## How to use this document

Two read-only scripts produce rows that land here:

1. **`scripts/track_a_historical_lookback.py`** — miss = legacy
   `fraud_score >= HARD_DECLINE_THRESHOLD` BUT Track A returned a
   non-`fail` verdict. The deal would have declined under the legacy
   rule and would NOT decline under Track A.

2. **`scripts/shadow_comparison_a_b_c_vs_fraud_score.py`** — five
   categories per the script's own taxonomy. The category that
   blocks Step 2 is `old-caught-something-new-misses` (REGRESSION).
   The others (`agreement`, `new-is-better`, `genuinely-ambiguous`,
   `insufficient-new-data`) are informational here.

Run both scripts on the box, append the rows to the table below, then
fill in the per-row triage:

| Field | Value space |
|---|---|
| **source** | `lookback` (Track A historical) or `shadow-comparison` |
| **run_date** | UTC date the script was run |
| **merchant_id** | The merchant UUID. Prefix-trim acceptable (`5cf4479d…`) |
| **document_id** | The document UUID (`lookback` only). Empty for shadow-comparison rows |
| **legacy_verdict** | `decline` / `tier-A` / `tier-B` / `tier-C` / `tier-D` / `tier-F` |
| **new_verdict** | Track A `clean`/`review`/`fail` (lookback); Track A+B+C tuple (shadow-comparison) |
| **category** | `genuine-regression` / `detector-gap` / `corpus-shape-artifact` (see below) |
| **rationale** | One sentence explaining the call |
| **reviewer** | Operator name (always `filip` for now) |
| **reviewed_on** | UTC date of the triage call |

A row is **complete** when `category`, `rationale`, and `reviewer` are
populated. Empty rows are blocking — Step 2 doesn't flip while any
row is un-reviewed.

---

## Category definitions

The three honest categories. Pick the one the evidence supports, not
the one most convenient.

### `genuine-regression`

Track A (or A/B/C) really doesn't catch a deal the legacy rule would
have correctly declined. This is the failure mode the gate exists to
catch. Action: **block the cutover** until either (a) the detector
is strengthened to cover the pattern, or (b) the operator explicitly
accepts the residual risk in writing.

Examples that would fit:
- The legacy rule declined for an `ofac_sanctions_match` and Track A
  is silent (Track A covers integrity, not OFAC — `ofac` lives in
  the score-time path regardless of engine, so this would be a
  shim-the-OFAC-check-into-track-abc question, not a Track A patch).
- The legacy rule declined for a `bank_statement_tampering_confirmed`
  that Track A inexplicably misses despite the evidence being on file.

### `detector-gap`

Track A doesn't catch the pattern AND the pattern is a known
limitation Track A was never designed to handle. The legacy rule
fired correctly via a different mechanism (e.g. pattern signals,
debt-to-revenue, NSF, days-negative, monthly-revenue floor) that
isn't part of Track A's scope.

Step 2 only retires the **fraud_score** path. The non-fraud
hard-decline rules in `score.py` still fire under `track_abc`
(verified in `tests/scoring/test_scoring_engine_config.py`). So a
detector-gap row is informational — the deal still declines via the
non-fraud path, just for a different reason. **Does not block** the
cutover.

Examples that would fit:
- `debt_to_revenue_exceeds_40pct: 55%` declined the deal in legacy;
  Track A is clean (integrity is fine). The `track_abc` engine still
  declines via the unchanged debt-to-revenue rule.
- `days_negative` = 16 and Track A is clean. The unchanged
  `DAYS_NEGATIVE_HARD_DECLINE = 15` rule still fires.

### `corpus-shape-artifact`

The disagreement is a property of the corpus itself, not of the
detectors. Common patterns:
- The merchant was a test seed from a prior dev session (verify via
  `audit_log` for `seed_*` actions). These pre-date the no-prod-seed
  rule and are not representative.
- The merchant was scored before a parser fix landed and the
  persisted `metadata_score` reflects the pre-fix output.
  Re-running parse on the same PDF would produce a different
  `metadata_score`, and the disagreement disappears.
- The merchant has only one document and the lookback's
  reconstruction of integrity signals is incomplete because one of
  the inputs (`metadata_flags`) was empty on the row.

**Does not block** the cutover. Worth fixing the data shape (delete
test seeds, re-parse pre-fix documents) but doesn't gate Step 2.

---

## Triage row template

Copy-paste this block into the table below for each new row. Fill
in-line; leave `category` / `rationale` / `reviewer` blank only if the
row is un-reviewed (and therefore blocking).

```
| lookback | 2026-MM-DD | <merchant_uuid_prefix>… | <doc_uuid_prefix>… | decline | review | genuine-regression / detector-gap / corpus-shape-artifact | one sentence | filip | 2026-MM-DD |
```

---

## Reviewed rows

| source | run_date | merchant_id | document_id | legacy_verdict | new_verdict | category | rationale | reviewer | reviewed_on |
|---|---|---|---|---|---|---|---|---|---|
| _no rows yet — populate after the first prod run of the lookback / shadow-comparison_ | | | | | | | | | |

---

## Open rows (un-reviewed — these block the cutover)

| source | run_date | merchant_id | document_id | legacy_verdict | new_verdict |
|---|---|---|---|---|---|
| _none_ | | | | | |

---

## Cutover authorisation log

Append a one-line entry every time the operator confirms intent to
move toward the flip — even a "not yet, blocked by row X" qualifies.

| Date (UTC) | Operator | Decision | Open-row count | Notes |
|---|---|---|---|---|
| 2026-06-10 | filip | scaffold this template | 0 (lookback not yet run against prod) | Plan 4.3 — doc skeleton landed; first run pending corpus growth past N=1. |
| 2026-06-23 | filip | FLIPPED — track_abc is live | 0 | Track A lookback EXIT 0, 42 scanned, 0 misses. AEGIS_SCORING_ENGINE=track_abc set in /etc/aegis/aegis.env. Commit c5c11fc. |

The actual env-var flip
(`AEGIS_SCORING_ENGINE=track_abc` in `/etc/aegis/aegis.env`) is plan
4.4 and requires this log to show a deliberate decision row with
Open-row count = 0.
