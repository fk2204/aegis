# Close → AEGIS automation spec

**Status:** build plan, Option A locked.
**Last updated:** 2026-06-05

---

## Goal

Fully automatic Close-to-scored-deal pipeline FROM the moment the operator
flips a Close Opportunity into "Docs In — Pre-UW". No manual scripts, no
operator button-mashing on the normal path. Scales to 20-30 new applications
per day.

The rescan button stays in the UI as a **recovery** surface for failed
ingests — not the normal path.

The "Score unavailable — no analyzed statement on file yet" silent failure
we hit on A&R KM LLC (`a522a8fb-…`) is the failure mode this spec rules out:
at 30 deals/day, no human can hunt for which one didn't go through. Failed
ingests have to be **visible** and **retryable from the same surface**.

---

## Trigger contract — Option A LOCKED

The webhook trigger is the operator flipping the Close Opportunity to status
**"Docs In — Pre-UW"** (`stat_1YZuVqdPWC8…`).

Everything *after* that flip runs automatically: pull → parse → score → queue
state surface.

**Option B (subscribe to `lead.note.created` / `lead.email.created` so AEGIS
ingests attachments as they arrive) is REJECTED.** Reason: it would ingest
incomplete document packages and start work the operator hasn't signalled
"ready to underwrite". The status-flip is the operator's intentional ingest
gesture and stays the contract.

Consequences for the build:
- No Close-side subscription changes required (the existing
  `opportunity.updated` subscription stays as-is).
- No new event-type branches in `webhooks_close.py:_matches_trigger`.
- Late-arrival statements (operator emails statements in after the Opp is
  already in Pre-UW) require an operator gesture — bump the Opp status off
  and back to Pre-UW, OR hit the rescan button on the dossier. The rescan
  button must work correctly for this to be acceptable (Step 1 fixes it).

---

## Verified state of the pipeline (read-only trace, 2026-06-05)

| Stage | File:line | State today |
|---|---|---|
| Webhook | `src/aegis/api/routes/webhooks_close.py:71` | `POST /webhooks/close` — HMAC-SHA256 verified, 5-min freshness window |
| Trigger filter | `webhooks_close.py:252-266` | `opportunity.updated` AND `status_id == CLOSE_DOCS_IN_PRE_UW_STATUS_ID` ✓ |
| Merchant upsert | `webhooks_close.py:306-375` | `close.merchant.created` / `close.merchant.updated` audit ✓ |
| Orchestration enqueue | `webhooks_close.py:157` → `close/orchestration.py:62` | `close.orchestration.enqueued` audit ✓ |
| Worker entry | `src/aegis/workers.py:1556` | `process_close_attachments` ✓ |
| Attachment listing | `src/aegis/close/client.py:330-380` | **BROKEN** — calls `/api/v1/files/?lead_id=…`, Close has no such endpoint, 404 |
| Attachment download | `client.py:390-…` | Broken in concert — `app.close.com` URL refuses Basic auth |
| Parse enqueue | (post-attachment-persist) | Never reached today |
| Failure visibility | per-merchant dossier only | "Score unavailable" — silent at volume |

The wiring upstream of `list_lead_attachments` is in place and correct. The
chain breaks at one specific call site. Fixing that one call site (Step 1)
unblocks the normal path.

---

## 4-step build plan

### Step 1 — Close client fix (Track 2)

Patch `CloseClient.list_lead_attachments` (`src/aegis/close/client.py:330-380`)
to enumerate `note` and `email` activities for the lead and walk each
activity's `attachments[]`, filtering PDFs. (Pattern originally proven
in a manual back-fill script, removed 2026-06-10 once acceptance #4
landed; structural shape is now locked by
`tests/close/fixtures/acti_note_with_pdf.json` + tests in
`tests/close/test_client_attachments.py`.)

Patch `CloseClient.download_attachment` to:
- rewrite `app.close.com` → `api.close.com` on the attachment URL,
- follow redirects to the S3 signed URL,
- return `(bytes, filename)` as today.

Tests against fixtures of real `acti_note` / `acti_email` payload shapes
(the activity envelope, not synthetic dicts).

`make check`, show diff before deploy.

### Step 2 — Staged verification

After Step 1 deploys:

1. POST `/ui/merchants/a522a8fb…/close-rescan` (A&R KM LLC).
2. Confirm the FULL chain end-to-end:
   - Statements pulled from Close (≥1 document persisted).
   - Worker picks them up (parse audit rows fire).
   - `parse_status` reaches a terminal value (`proceed` / `review` / `manual_review`).
   - Score appears on dossier (no more "Score unavailable").
3. **If statements pull but DON'T parse, STOP and report — that's a second break.**

(The previously-listed second backfill — Top Tier Authentics
``b6d37e19…`` — is removed from the acceptance set on operator
direction 2026-06-10. A&R KM is the proof case.)

### Step 3 — `/ui/close-queue` (the load-bearing piece for 30/day scale)

New dashboard route + template. Aggregates Close-sourced merchants
(`close_lead_id IS NOT NULL`) by pipeline stage, derived from
`audit_log` signals:

| Stage | Derivation |
|---|---|
| `merchant_created` | `close.merchant.created` present, no later audit |
| `attachments_pulled:N` | `close.orchestration.complete` with attachment count N |
| `parsing:N/M` | `parse_started` × N, `parse_succeeded` × M < N |
| `analyzed:N` | `parse_succeeded` × N |
| `scored` | merchant has a non-null `fraud_score` / analyzed row |
| `FAILED:<reason>` | any `*.failed` audit row not superseded by a later success |

Each row shows: merchant name, Close lead id, created_at, current stage,
last audit timestamp + action. `FAILED` rows get a retry button — POSTs
to the existing `merchant_close_rescan` handler (`router.py:2279`). The
normal path needs no operator interaction.

### Step 4 — Stale-row detection (polish)

Page-render-time check OR cron, flag rows stuck:
- `close.merchant.created` present but no `attachments_pulled` after 6h.
- A document with `parse_status == 'pending'` for > 1h.

Surface visually on `/ui/close-queue` so a silently-stuck merchant doesn't
fall through the cracks even when no `*.failed` audit fires.

---

## Acceptance

1. New Close lead, operator drops statement PDF on it, flips Opp to
   "Docs In — Pre-UW": merchant row created → attachments pulled →
   parsed → scored, with zero operator clicks after the status flip,
   within 5 minutes of the webhook.
2. Statement emailed in 12h after status flip: operator either bumps
   status or clicks rescan on the dossier; pipeline carries through.
3. Simulated failure (corrupted PDF): row appears on `/ui/close-queue`
   with `FAILED:parse_failed` and a working retry button. Operator
   click re-runs.
4. A&R KM LLC comes back into the queue and reaches `scored` without
   the operator running any script. (Top Tier Authentics was removed
   from the backfill set 2026-06-10 by operator direction.)

---

## Cross-references

- Track 2 investigation report: in-conversation, 2026-06-05.
- Webhook trace: in-conversation, 2026-06-05.
- Manual back-fill scripts (`scripts/manual_close_pull.py`,
  `scripts/manual_close_pull_note_files.py`,
  `scripts/manual_persist_local_pdfs.py`): removed 2026-06-10 after
  acceptance #4 landed via A&R KM. Recoverable from git history if
  ever needed.

---

## Parked — deliberate decisions for later

These were deferred during the 2026-06-05 session. Each is its own
discussion, not part of this spec's build:

- **fraud_score 65/70 threshold (A.2) + EOF gate (A.1)** — decline-
  boundary policy, see `docs/audit-confirmed-bugs.md`.
- **NY broker-compensation disclosure guard** — wire as NY hard-fail
  vs delete the dormant module; product decision. The new
  `/ui/funders/new` form already exposes `aegis_compensation_disclosure_text`
  so Shor's text can be pasted through the UI without code.
- **Scoring redesign** (3-track: integrity / business-risk band /
  context) — Q1 decided, Q2/Q4 open. Multi-week build.
- **Tampering rule** — stays SHADOW until live audit rows reviewed.
- **Industry casing mismatch** — seed/new-form Title Case vs prompt's
  lowercase-hyphenated. Matcher is case-insensitive; cosmetic backfill.
- **VU 7722 reconciliation** — 3 of 4 months failed by $5/$11/$55;
  fraud-review question (running-balance drift signature), not a
  parser-salvage task.
