# Repo hygiene scan — 2026-06-12

**Purpose:** systematic check of "should be caught" maintenance items.
Triggered by two regression-class bugs slipping through the per-commit
gate in the 2026-06-10 session (`224f413` line-length E501 and
`3f215bd` close_queue mypy errors). Both would have been caught by
`.pre-commit-config.yaml`'s ruff + mypy hooks, but those hooks aren't
installed by default in this repo.

**Method:** read-only scan; orchestrator decides what to act on.

**Closure status (afternoon 2026-06-12 update):**
- **H1 act-now ✅ CLOSED** in commit `bcf209a` — install-hooks unifies
  ruff + mypy + compliance-review under pre-commit framework.
- **H3 worth-fixing ✅ CLOSED** in commit `dcc65c1` — box-side ops
  gotchas landed in `.claude/rules/deploy.md` + funder-seeding sub-rule
  in operating-principles.md.
- **H6 ✅ ACTED ON** prior to this update (gitignore additions for
  editor/AI tooling backups).
- **H2 / H4 / H5 / H7-H10 still open** (H2 needs operator decision; the
  others are informational or operator-review-pass candidates).

## Summary

10 findings. **2 act now**, **5 worth fixing**, **3 informational**.
Biggest risk: the two pre-commit gates (`.githooks/pre-commit` for
compliance-review and `.pre-commit-config.yaml` for ruff+mypy) are
**mutually exclusive by design** — `make install-hooks` sets
`core.hooksPath=.githooks` which bypasses `.git/hooks/pre-commit` where
`pre-commit install` writes. README §"Developer setup" calls this out
but leaves the actual chaining as homework. The 2026-06-10 regressions
are direct evidence the chain is unsolved.

## Findings

### [H1] Pre-commit hooks not installed — both gates inert
**Status:** 🔴 act now
**What I checked:** `git config core.hooksPath` returns `.git/hooks`
(default). `.githooks/pre-commit` exists (2749 bytes, executable),
enforces compliance-review annotations only on
`docs/compliance/states/**`. `.pre-commit-config.yaml` (3078 bytes,
2026-06-10) defines ruff + mypy on every commit. Neither is active.
**Finding:** Two parallel pre-commit systems exist with a documented
conflict (README §Developer setup): `make install-hooks` and
`pre-commit install` clobber each other. The 2026-06-10 regressions
slipped because the ruff/mypy gate isn't installed and chaining wasn't
implemented.
**Recommendation:** Pick ONE primary mechanism. Cleanest option: drop
`core.hooksPath=.githooks` from `make install-hooks`; instead, install
`pre-commit` and add the compliance-review check as a third hook in
`.pre-commit-config.yaml` (local hook, language: system, runs the same
bash script). Then `pre-commit install` covers both. Update README §32-50.
**Effort to fix:** ~30 min (script port + README update + one-time test commit).

### [H2] design/ is untracked but not gitignored — Schrödinger directory
**Status:** 🟡 worth fixing
**What I checked:** `git status` shows `?? design/`. `git
check-ignore` does NOT match it (the earlier "line 93" report was
git's quirk reporting on a blank line). `git log -- design/` returns
nothing — never tracked. Contents:
- `design/box-artifacts/_audit_seed.py` + `_audit_seed_funders.py` —
  one-shot data-fix scripts from the 2026-05-10 audit (docstring says
  "Not for routine use"). They import `from aegis.db import
  get_supabase` so they ARE runnable from this repo.
- `design/logos/` — 6 SVGs (3 lockups, 3 marks) for AEGIS branding.
- `design/logo-preview.html` — 11.7 KB Jinja-free preview page with
  inline SVG references to the logos.
**Finding:** Two distinct concerns mixed in one untracked dir: (a)
operator-only data-fix scripts that arguably belong in `scripts/audit/`
once vetted, and (b) brand assets that could be tracked or moved to
`docs/brand/`. Today it's invisible to anyone cloning the repo.
**Recommendation:** Pick one:
- (a) Commit `design/logos/` + `logo-preview.html` to `docs/brand/`
  (small, no PII, useful for future operators).
- (b) Move `_audit_seed*.py` to `scripts/audit/` after a security
  review confirms they have the same accidental-production-write
  guards as `scripts/audit/*` (per operating-principle #4).
- (c) If the operator wants these kept local, add `design/` to
  `.gitignore` and document the convention in CLAUDE.md.
**Effort to fix:** ~20 min once direction is chosen; requires operator input on intent.

### [H3] CLAUDE.md drift — five non-negotiable rules since "Last updated 2026-06-05"
**Status:** 🟡 worth fixing
**What I checked:** CLAUDE.md footer = 2026-06-05. Last commit
touching CLAUDE.md: `ae86343` (2026-06-05). 28 commits have landed
since. Notable behavioral rules learned post-2026-06-05 that CLAUDE.md
doesn't reflect:
1. **AEGIS sudo NOPASSWD pattern** (2026-06-10, see global memory) — only
   `sudo -n /usr/bin/systemctl restart aegis-web aegis-worker` works
   from non-TTY shells. Cost ~30 min thrashing.
2. **Funder seeding discipline** (2026-06-10) — never seed placeholder
   funder data; ask operator for actual criteria sheets. 4 wasted
   migrations on 2026-06-10.
3. **systemctl status leaks tokens** (2026-06-10) — use `is-active` +
   targeted `journalctl --output=cat`. Cost a tunnel-token rotation.
4. **Read AEGIS rules first** (2026-06-10) — before deploy/parser/
   compliance/scoring work, read matching `.claude/rules/*.md`.
5. **Decision-boundary changes shadow-first** — already in CLAUDE.md
   but no recent corpus evidence pointer.
**Finding:** Items 1-4 are deploy/ops gotchas that future-Claude will
re-discover at cost. They live in `~/.claude/projects/.../memory/`
files which are Filip-machine-local only — clones won't have them.
**Recommendation:** Add a "Deploy & operations gotchas" section to
CLAUDE.md (or `.claude/rules/deploy.md`) covering items 1-3. Item 4
is already implicit in the "Where to find the rest" footer but could
be promoted. Don't expand CLAUDE.md beyond what saves real time.
**Effort to fix:** ~20 min for the additions.

### [H4] CORPUS_FINDINGS.md gap — 2026-06-09 R4.7 entry shipped, but no entries since
**Status:** 🟢 informational
**What I checked:** CORPUS_FINDINGS.md last entry = 2026-06-09 R4.7
regional-bank layouts (matches `git log -1 -- CORPUS_FINDINGS.md` =
2026-06-09 17:25). No corpus-touching commits since (last 20 show
parser/scoring work but no corpus regen or new bank-layout finding).
**Finding:** No gap. CORPUS_FINDINGS.md is current as of the most
recent corpus expansion. The doc has good discipline ("convention for
future entries" at the foot).
**Recommendation:** None. Surface this to the operator only if the
2026-06-12 work involves corpus changes — in that case, append a new
entry.
**Effort to fix:** N/A.

### [H5] audit-confirmed-bugs.md — all three items closed, file lingers as scar tissue
**Status:** 🟢 informational
**What I checked:** Header says A.1 ✅ CLOSED 2026-06-10, A.2 ✅
CLOSED 2026-06-10 (`4c1c743`), A.3 ✅ CLOSED pre-session. Body
(160+ lines) preserves the original "CONFIRMED-LIVE" diagnoses as
historical record.
**Finding:** The closure header at lines 1-9 is clear enough. The
body is useful as a forensic trail (it shows how the bugs were
diagnosed and the rationale for the fix order), which has training
value for future-Claude reading why constants live in
`aegis.thresholds` or why the router-vs-module shadowing pattern is
banned.
**Recommendation:** Keep as-is. Optional: move to `docs/archive/` if
the operator wants `docs/` to only show currently-actionable items.
**Effort to fix:** ~2 min if archiving.

### [H6] .gitignore gap — operator scratch artifacts not covered
**Status:** 🟡 worth fixing
**What I checked:** `.tmp/` is correctly ignored (line 92). But:
- `.tmp/aegis_tunnel_token.txt` exists. Confirmed ignored.
- `install-cloudflare-tunnel.ps1`, `upload-statements*.ps1`,
  `cf-token*`, `CF_TOKEN*` patterns are present — good.
- Missing: `.aider*`, `.cursor/`, `.continue/` (other AI tool
  caches that could appear if someone uses a different IDE).
- Missing: `*.bak`, `*.swp`, `*~` (editor backup patterns).
- `docs/design/` (the file at `docs/design/close-integration-design.md`)
  is fine — that's the tracked file under `docs/design/`, separate
  from the top-level untracked `design/` in [H2].
**Finding:** Coverage is good for the AEGIS-specific risk surface
(credentials, tunnel tokens). Editor-backup and AI-tool patterns are
hygiene-level, not security-level.
**Recommendation:** Add a 4-line "Editor/AI tooling backups" block:
```
*.bak
*.swp
*~
.aider*
```
**Effort to fix:** ~5 min.

### [H7] docs/horizontal_scale_readiness.md — last touched 2026-05-19, content may be stale
**Status:** 🟡 worth fixing
**What I checked:** File last modified 2026-05-19 (`ea0f142`). The
since-then scaling-relevant commits (R4.5 cohort backtest scaffold,
R4.6 EOF env-var, R4.7 corpus expansion, B2 Step 2 scoring engine
cutover) likely touch invariants or thresholds named in the doc but
the doc hasn't been updated. Without reading the file deeply (not in
scope), can't confirm specific drift.
**Finding:** Candidate for review-and-update or archival.
**Recommendation:** Skim during the next horizontal-scale work; if
items have shipped that the doc claims are pending, update or move to
`docs/archive/horizontal_scale_readiness_2026-05-19.md` and start a
fresh version.
**Effort to fix:** ~30 min skim + selective edit.

### [H8] docs/archive/ has only REWRITE_PLAN.md — pattern is established but underused
**Status:** 🟢 informational
**What I checked:** `docs/archive/REWRITE_PLAN.md` (May 13). Other
candidates that COULD be archived if they're truly historical:
- `docs/AUDIT_2026_05_10.md` (filename has the date)
- `docs/real_corpus_expansion_spec.md` (spec for an item that shipped)
- `docs/SCORING_REDESIGN_CONTINUATION.md` (continuation doc, may be obsolete after B2 Step 2 cutover)
**Finding:** Archival discipline exists but isn't routinely applied.
Not actionable on its own — depends on whether the items have truly
shipped end-to-end.
**Recommendation:** Pass on this in isolation; revisit during a docs
sweep alongside [H7].
**Effort to fix:** ~15 min after a per-doc shipped-yes/no check.

### [H9] Commit message convention — 15-commit sample is clean
**Status:** 🟢 informational
**What I checked:** Last 15 commit subjects.
- 13 follow `type(scope): description` exactly.
- 1 bare `docs:` (no scope) on `ffa453a docs: worktree-isolation
  diagnostic for Windows Claude Code Agent` — minor.
- 1 `revert:` (`20f60ec revert: drop CF_API_TOKEN...`) — `revert` is
  not in the type list from `~/.claude/rules/git.md`, but it's a
  standard convention.
**Finding:** No real drift. The `docs:` without scope is the only
nit and it's borderline.
**Recommendation:** None. Optional: add `revert` to the allowed
types in `~/.claude/rules/git.md`.
**Effort to fix:** N/A.

### [H10] `scripts/_*.py` — six leading-underscore "private" scripts hint at unrecognised one-shots
**Status:** 🟡 worth fixing
**What I checked:** Top-level scripts named `_debug_doc.py`,
`_reparse_one.py`, `_reparse_wipe.py`, `_status_probe.py`, +
`_verify_leg_runner.sh`. The underscore-prefix convention typically
means "internal" or "do not import." Here they live alongside committed
public scripts (`add_funder.py`, `apply_migrations.py`, etc.).
**Finding:** These look like the AEGIS equivalent of `design/box-artifacts/_audit_seed*.py` —
operator-only one-shots that got committed because they touch the
DB. If they're still relevant, they need a docstring header
explaining when to run them. If they're done, they should move to
`docs/archive/` or be deleted (git history preserves them).
**Recommendation:** Operator review pass. For each `_*.py`, decide:
keep (add docstring + idempotency guard), archive, or delete.
**Effort to fix:** ~30 min walkthrough with operator.

## Out-of-scope observations

- **Empty `__init__.py`** under most `tests/<area>/` directories — this
  is fine and expected (pytest discovery), not a finding.
- **`.tmp/aegis_tunnel_token.txt` exists and is gitignored.** Per the
  global rule "never print credentials in tool output," I did not
  read its contents. Operator should still verify rotation cadence.
- **No `.github/workflows/test.yml` parity check** was performed
  against `.pre-commit-config.yaml`. They claim to mirror each other
  (per the YAML comment) but neither has been audited side-by-side.
- **The two `design/box-artifacts/_audit_seed*.py` scripts** import
  from `aegis.db` directly — if anyone runs them by mistake they hit
  production Supabase. Operating-principle #4 makes this risky.
  Worth surfacing during the [H2] decision.
