# Scoring Redesign — Continuation Note

**Saved:** 2026-06-04 (end of session)
**Status:** paused mid-design. No build in flight. Continue when ready.

This note exists so the next session can pick up cold without re-deriving the state. It is the bridge between the session that produced the redesign brief and the session that ships the first build step.

---

## The design doc (the authoritative artifact)

`AEGIS_scoring_redesign.md` — the three-track redesign. Lives in the operator's drafts (not committed to the repo). Key thesis: the current engine conflates *how a legitimate modern business moves money* with *evidence of fraud or risk*, and scores both on one undifferentiated `fraud_score`. The fix is three independent tracks that never blend back into a single number.

- **Track A — Document Integrity.** A near-binary gate. Is the statement real? Metadata forensics, math reconciliation (incl. running-balance drift as the canonical "competent fabrication" signal), font/structural anomalies, cross-document consistency. Output: `integrity_verdict ∈ {clean, review, fail}`.
- **Track B — Business Risk.** An explainable 4-band score. Can the business support repayment? True revenue (net of transfers/own-account sweeps), ADB, lowest balance, negative days, NSFs, trend & volatility (seasonality is not risk), stacking/positions, holdback capacity. Output: `risk_band ∈ {low, moderate, elevated, high}`.
- **Track C — Context / Concentration.** Informational only, never auto-penalizes. Counterparty type (processor / own-account / international client / end-customer / card-paydown / unknown), stress-case reasoning. Informs Track B's band at most; never independently fires a decline.

Counterparty classification (Section 4 of the brief) is the foundation B and C both depend on.

Industry-research validation cited in the brief is current as of 2026-06-04. Re-validate if more than a quarter has passed before shipping.

---

## What's DECIDED

### Q1 — Band → operator-action mapping (this is what Track B's shadow mode measures)

| Band | Action |
|---|---|
| **Low** | Auto-forward to funder match. The ONLY band that moves without a human. |
| **Moderate** | Human review, neutral. |
| **Elevated** | Human review, neutral. |
| **High** | Human review. **Decline is the default** — worker must actively override to fund. |

**Principle:** only clean deals auto-move. Nothing auto-declines on business-risk alone without a human seeing it. (Document-integrity `fail` from Track A is the only thing that auto-blocks at all, and it's narrower than a band.)

### Q3 — Counterparty classification: parse time + cache + re-classify CLI

Runs at parse time (not at every dossier render). Results are cached per-merchant or per-statement. A re-classify CLI lets the operator force a refresh when the dictionary gets meaningful updates (new processor added, new MCA funder, etc.).

- **Dictionary** for the obvious cases: Shopify / Amazon / Stripe / PayPal / Square / known MCA funders / payment processors. Cheap, deterministic.
- **LLM-assisted** only for unknowns. Most of the long tail is end-customers and international clients; the dictionary will not grow forever.

---

## What's STILL TO CONFIRM (quick, next session)

### Q2 — Integrity verdict states

Proposed mapping from the existing tampering composition:
- `fail` = `strong_metadata` (auto-decline / hard block)
- `review` = `medium_corroborated` (operator confirm-or-override)
- `clean` = `none`

**PLUS** pin the running-balance-drift signal explicitly:
- **drift alone** = `review`
- **drift + consumer-editor metadata** (Foxit / Smallpdf / iLovePDF / Word / Canva / etc.) = `fail`

Operator confirms.

### Q4 — Own-account classification when the other side is missing

Three options were on the table. Operator's call: **option 1 — `own_account_unconfirmed`.** Surface the gap explicitly ("merchant references account we don't have statements for"). Do NOT infer-own from descriptions alone; the cost of a false-confirm is too high.

This is also what closes the VU-discovery problem cleanly: 7722 references CHK 9940 and CRD 0993 → dossier shows "Referenced accounts (statements not on file): CHK 9940, CRD 0993" instead of the operator finding out from a script.

---

## Build order (agent-corrected sequence)

```
        ┌──── Step 1: counterparty classification (foundation for B + C)
1 + 3 ──┤
        └──── Step 3: Track A forensics (running-balance drift, text-layer integrity,
                       wire the dead tampering control)

  ↓ (Step 1 + Step 3 both complete)

Step 4: Track C reframing
        - concentration / processor / international / own-account as CONTEXT
        - cross-bundle own-account netting (kills the VU false-positive class cleanly)

  ↓

Step 2: REPLACE fraud_score with the 3 new outputs (integrity_verdict, risk_band, context_panel)
        - breaking change for dossier + downstream readers
        - SHADOW-FIRST, validated against the corpus before any field flip
        - goes LAST when everything else is producing the new shape

  ↓

Step 5: Track B banding
        - port existing cash-flow signals into the explainable 4-band
        - seasonality / volatility reasoning
```

Steps 1 and 3 are file-disjoint by design (Step 1 lives in `aegis/parser/patterns.py` + new classifier module; Step 3 lives in `aegis/parser/metadata.py` + `aegis/parser/validate.py` + `aegis/parser/tampering.py`). **Confirm file sets before fanning out** — worktree isolation is unreliable; file-disjointness is the only real protection. (Lesson from this session: tracks repeatedly fell back to main repo cwd; the only thing that kept work safe was different target file paths.)

Step 2 is the only step that's a breaking change for the dossier and downstream readers. It benefits from going LAST when everything else is producing the new shape.

Steps 1, 3 (text-layer), 4 are largely **mechanical/additive** (presentation + detection, no decline-boundary change) and can ship incrementally without shadow gates. Step 2 and any change to *what auto-declines* are **decision-policy** — shadow mode + corpus validation before live, never blended back into one tunable number.

---

## DONE this session (zero-risk, before any redesign build)

Two guardrails promoted into `CLAUDE.md` under a new `### Scoring discipline` subsection of Non-Negotiable Rules:

1. **Document integrity and business risk stay separate forever.** Never re-blend into one score. The moment they share a score, "tune the severity to clear a specific deal" becomes the path of least resistance.
2. **No track-tuning to pass a specific merchant.** Changes validated on a corpus, not reverse-engineered from one deal. Shadow mode + corpus validation before any decision-boundary edit; same discipline as the tampering rule.

Both would have prevented H10. They bind future scoring work even if someone forgets the redesign rationale.

---

## Also still parked (unrelated to the redesign)

These are pre-existing items the redesign does NOT touch. They live in their own audit doc and stay parked until operator schedules them.

- **A.2 — fraud_score 65/70 threshold mismatch.** Deliberate decline-boundary call. `docs/audit-confirmed-bugs.md`.
- **A.1 — EOF gate inconsistency.** Pipeline relaxed `eof_markers > 2`; score-side still `> 1`. Deliberate decline-boundary call. Same doc.
- **Tampering rule stays SHADOW** until operator reviews live shadow audit rows. Flag at `aegis_tampering_decline_mode = "shadow"`. The `tampering_would_decline` audit-log action is the signal to read.
- **VU 7722 — 3 of 4 months failed reconciliation by $5 / $11 / $55.** Sit with this as a fraud-review question. **The redesign's running-balance-drift signal (Track A) is exactly this pattern.** It is NOT a parser-salvage task ("re-extract until it passes"); the integrity gate is doing the right thing.

---

## Hard-won discipline carried forward

- **Decision-changes are deliberate + shadow-first.** Anything that moves a decline boundary runs in shadow mode, validated against false positives AND true positives, before live. Tonight's tampering ship is the established pattern.
- **No batch ships on decision changes.** Each fix gets its own commit, smoke-check, and operator review window. H1 / H2 / A.3 shipped one at a time tonight — that's the cadence.
- **File-disjoint, not worktree isolation, is what protects parallel agents.** Two of three worktrees fell back to main repo cwd this session. The only thing that prevented collision was that the tracks targeted different files.
- **Show diff before write on every prod-state mutation.** Re-aggregation / re-pattern / rescore scripts all hard-pause for explicit `YES` before any DB write.

---

## When picking this up

1. Re-read this note and the design brief.
2. Confirm Q2 and Q4 (the remaining two open questions) with operator.
3. Confirm file sets for Step 1 and Step 3 before fanning out to parallel agents.
4. Pick which of Step 1 or Step 3 ships first (or both in parallel) and scope the first piece.
5. Same workflow as this session: design → operator review → agent scoping → diff → operator review → ship → smoke → next.
