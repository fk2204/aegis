# Horizontal-scale readiness — current state + growth path

Phase 11 task #5. Verifies the application is already statefulness-
clean enough to scale horizontally when deal volume justifies it, and
documents the deployment path the operator follows to grow without
a rewrite.

The current single-box Hetzner CPX21 deploy handles the ~100
deals/month profile. This doc covers the path forward.

---

## Current state (verified 2026-05-19)

| Property | Status | Evidence |
|---|---|---|
| Worker code is stateless across calls | ✓ | `src/aegis/workers.py` `parse_document` reads everything it needs from args + repositories; no module-level mutable state between calls. |
| Session/queue state in Redis | ✓ | `arq` queue is Redis-backed via `aegis_storage_backend=supabase` path; `redis-cli ping` is the worker liveness pre-check. |
| File uploads land in object storage (or scratch + delete) | ✓ | PDFs land under `AEGIS_UPLOAD_DIR`, are processed, and deleted in a `finally` block in the worker. No long-lived on-disk state. |
| Settings cache is per-process (not shared) | ✓ | `@lru_cache(maxsize=1)` on `get_settings()` — same value computed identically across processes from the same env file. |
| Per-IP rate-limit store is in-memory | ⚠ | `aegis.ops.rate_limit.InMemoryRateStore`. Single-box OK; needs a `RedisRateStore` swap when multi-box (Protocol already pinned for the swap). |
| No SQLite / local DB | ✓ | Only Postgres (Supabase) and Redis. |
| Worker assumes a unique process | ⚠ | `_populate_worker_attributes()` reads env at import; multiple workers picking up the same Redis stream is supported by arq, but the cost-tracking + audit_log assumptions need a quick review when horizontal. |

The two warnings (⚠) are the only items that need attention to scale
beyond one box. Both are localized — neither is a structural blocker.

---

## Growth path

### Stage 1 — current: single box

* One Hetzner CPX21 hosting `aegis-web` + `aegis-worker` + Redis.
* Cloudflare Tunnel → uvicorn on `127.0.0.1:5555`.
* RPO: 24h (daily Supabase logical dump, off-Hetzner storage).
* Capacity ceiling: ~100 deals/month at the current
  `AEGIS_WORKER_MAX_CONCURRENT=4`. Empirically the parser takes 30-90s
  per statement; one worker easily handles steady-state burst.

### Stage 2 — multi-worker on one box

Triggered when daily deal volume regularly exceeds the worker's
throughput (queue depth alert fires repeatedly).

What changes:
1. Run multiple `arq` worker processes by adjusting
   `AEGIS_WORKER_MAX_CONCURRENT` upward AND/OR running multiple
   `aegis-worker.service` units (`aegis-worker@1`, `aegis-worker@2`,
   … via a templated unit file).
2. Web stays as a single uvicorn process — its load is light at this
   scale (the parsing work is in the worker).
3. Heartbeat units gain `aegis-heartbeat-worker@1` siblings.
4. Healthchecks.io picks up one check per worker process.

What doesn't change:
* Schema. arq's Redis-backed queue is multi-consumer-safe by design.
* Rate-limit store. The web process is still single; the in-memory
  store is still correct.
* Auth. Cloudflare Access SSO scales with operators, not processes.

Capacity ceiling: ~500 deals/month (assuming Bedrock cost stays
acceptable — see weekly digest from Phase 11 task #2).

### Stage 3 — multi-box behind a load balancer

Triggered when CPU on the single box becomes the bottleneck OR the
operator wants regional redundancy.

What changes:
1. **Rate-limit store → Redis.** Swap
   `aegis.ops.rate_limit.InMemoryRateStore` for a Redis-backed
   implementation of `RateLimitStore`. The Protocol is already in
   place; this is a ~50-line addition.
2. **Object storage for uploads.** Today PDFs land in
   `AEGIS_UPLOAD_DIR` on disk and are deleted in a `finally` block.
   At multi-box, the web process and the worker MAY run on different
   boxes; replace `AEGIS_UPLOAD_DIR` with S3-presigned upload that
   the worker consumes by `boto3.client('s3').get_object` then
   deletes the S3 object. Storage interface (`aegis.storage`) doesn't
   change shape; only the on-disk path becomes an S3 key.
3. **Load balancer.** Cloudflare Tunnel already supports multiple
   `cloudflared` instances pointing at the same hostname; add a
   second instance on each new box. No additional config needed on
   the Cloudflare side — Access ID stays the same.
4. **Redis → managed.** Move from box-local Redis to Upstash /
   ElastiCache so the workers across boxes all read the same queue.
5. **Logging → centralized.** Today each box's logs land in its own
   journalctl + `/var/log/aegis/app.log`. At multi-box, ship logs
   to a single collector (Grafana Loki, Better Stack, Datadog) so
   the operator has one log surface.

What doesn't change:
* Application code. Every change above is config or middleware swap.
* Schema. Supabase already handles multi-client connections; the
  advisory lock in `apply_migrations.py` serializes schema changes.
* Bedrock integration. AWS IAM credentials are already shared via
  `/etc/aegis/aegis.env`; each box reads its own copy.

Capacity ceiling: O(thousands of deals/month) per region. Beyond
that, regional sharding (multi-region S3 + per-region Supabase) is
the next step but well beyond the foreseeable operator profile.

---

## What's deliberately NOT in scope today

* **Active-active multi-region.** The data-residency boot guard
  pins all Bedrock + storage to US regions. Adding a second region
  is a counsel + Bedrock-region question, not an engineering one.
* **Read replicas for Supabase.** Today the web process reads
  via the same connection as writes; at 100 deals/month this is
  fine. Phase 11 task #5's growth path defers replica setup to
  Stage 3 where it pairs naturally with the multi-box plan.
* **GraphQL / gRPC.** REST + JSON is the surface today. No reason
  to change it at the scales this doc covers.

---

## Operator action items by stage

| Stage | Action | When |
|---|---|---|
| 1 → 2 | Bump `AEGIS_WORKER_MAX_CONCURRENT` to 8 | When parse jobs back up regularly (>5 minute average wait) |
| 1 → 2 | Add `aegis-worker@2.service` (templated) | When concurrency=8 is still bottleneck |
| 2 → 3 | Swap `RateLimitStore` to Redis-backed | When provisioning a second box |
| 2 → 3 | Migrate `AEGIS_UPLOAD_DIR` to S3 | Same |
| 2 → 3 | Provision managed Redis (Upstash) | Same |
| 2 → 3 | Add centralized log shipping | Same |

Last verified: 2026-05-19 (Phase 11 task #5).
