# Cloudflare Access — worker onboarding

The AEGIS dashboard sits behind Cloudflare Access (SSO gate). To let a
new worker in, you add their email to the policy. **This is the only
step that grants dashboard access.** No file edits on the box, no code
push, no DNS change.

---

## One-time setup (already done for the operator)

Operator already has the tunnel at `aegis.commerafunding.com` →
`5.161.51.105:5555` and at least one Access policy gating the root path
`/*`. The same tunnel also fronts `aegis-ssh.commerafunding.com` → SSH
to `localhost:22` for operator deploys + ops admin (see
`deploy/RUNBOOK.md`). Both hostnames belong to the **same** Access
application ("AEGIS Dashboard"), so adding a worker email below covers
both surfaces in one step. If neither hostname is configured yet,
follow `deploy/RUNBOOK.md` first.

---

## Adding a worker (5 minutes per worker)

1. **Cloudflare dashboard** → pick the `commerafunding.com` zone.
2. Left sidebar: **Zero Trust** → **Access** → **Applications**.
3. Find the AEGIS application (the one with the `aegis.commerafunding.com`
   hostname). Click it.
4. **Policies** tab → click the **Allow** policy.
5. In the **Configure rules** section, add a new "Include" rule:
   - Selector: **Emails**
   - Value: `worker1@example.com` (the worker's email)
   - Repeat for each worker.
6. **Save**.
7. The worker visits https://aegis.commerafunding.com — gets a Cloudflare
   email-OTP prompt — pastes the PIN — they're in. No other setup needed.

### What the worker sees on first login

- Cloudflare Access "Sign in" page with the email options the policy
  allows.
- If they're new to Cloudflare: a confirmation email with a 6-digit PIN.
- After PIN entry: they land on `/ui/` (AEGIS index).
- Session duration is whatever you set in the Access app (default 24h).

### Revoking a worker

Same panel — delete the email from the Include rule. They lose access on
their next page load (session is signed by Cloudflare, not by AEGIS).

---

## Suggested policy shape

For Commera Capital's current size (operator + 1-3 workers):

| Application | Path | Policy | Action |
|---|---|---|---|
| AEGIS Dashboard | `aegis.commerafunding.com/*` | Allow | Emails in `[operator, worker1, worker2, ...]` |
| AEGIS Dashboard | `aegis-ssh.commerafunding.com/*` | Allow | Same email list (single self_hosted_domains entry, single policy) |
| AEGIS Dashboard | (no other allow rules) | — | — |

Anything not matching the allow rule gets denied. No need for an explicit
deny.

---

## Audit log

Cloudflare Access logs every authentication event to its own audit log
(Zero Trust → **Logs**). The AEGIS bearer-token audit (`audit_log`
table in Supabase) is a **separate** log for the bearer API path and
worker actions inside the dashboard. Together they cover:

- Who logged in when (Cloudflare)
- What they did inside the app (Supabase `audit_log`)

When a worker leaves, take both logs into account before deciding what
needs further review.

---

## What this policy does NOT cover

- **The bearer API (`/upload`, `/merchants`, `/funders`, etc.)** — those
  are protected by a different token in `/etc/aegis/aegis.env`
  (`API_BEARER_TOKEN`). Workers don't need that token; they go through
  the dashboard.
- **SSH to the box** — that's the operator's SSH key
  (`~/.ssh/aegis_ed25519`). Not delegated to workers in this round.
- **Supabase dashboard access** — that's a separate Supabase login. Only
  operator needs it.

Per CLAUDE.md, the box's auth model is:
**Cloudflare Access SSO (dashboard) + bearer token (programmatic API) + SSH key (ops only).**

Three layers. Don't merge them.
