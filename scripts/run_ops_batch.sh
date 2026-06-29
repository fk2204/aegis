#!/bin/bash
# Server-side ops batch — runs on the prod Hetzner box.
#
# Sources /etc/aegis/aegis.env (no credential transit) and walks the
# standard backfill chain:
#
#   1. OFAC list refresh
#   2. Close lead re-sync (--apply)
#   3. Manual-review reparse via recover_legacy_docs.py
#   4. Background-checks pre-warm for every merchant (parallel)
#   5. Narrator-summary enqueue for proceed docs missing summaries
#
# Invoke from the workstation:
#
#   ssh -i ~/.ssh/aegis_ci_deploy root@5.161.51.105 \
#     "nohup bash /opt/aegis/scripts/run_ops_batch.sh > /tmp/ops_batch.txt 2>&1 &"
#
# The script logs to stdout — capture via the SSH-side redirect.

set -e
cd /opt/aegis
set -a
# shellcheck disable=SC1091
source /etc/aegis/aegis.env
set +a

echo "=== $(date) Starting ops batch ==="

echo "--- OFAC refresh ---"
.venv/bin/python scripts/update_ofac_list.py

echo "--- Close re-sync ---"
.venv/bin/python scripts/resync_close_leads.py --apply

echo "--- Background checks (parallel) ---"
.venv/bin/python scripts/recover_legacy_docs.py --run-background-checks-all --apply &
BG_PID=$!

echo "--- Reparse sealed manual_review ---"
.venv/bin/python scripts/recover_legacy_docs.py \
    --reparse-sealed-manual-review --all-merchants --apply

echo "--- Narrator backfill ---"
.venv/bin/python - <<'PYEOF'
import asyncio
from arq import create_pool
from arq.connections import RedisSettings
from aegis.config import get_settings
from aegis.db import get_supabase


async def main() -> None:
    settings = get_settings()
    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    sb = get_supabase()
    docs = (
        sb.table("documents")
        .select("id,merchant_id")
        .eq("parse_status", "proceed")
        .execute()
    )
    enqueued = 0
    for d in docs.data:
        a = (
            sb.table("analyses")
            .select("id,narrator_summary")
            .eq("document_id", d["id"])
            .limit(1)
            .execute()
        )
        if a.data and not a.data[0].get("narrator_summary"):
            await pool.enqueue_job(
                "generate_narrator_summary", str(d["id"]), str(d["merchant_id"])
            )
            enqueued += 1
    print(f"Narrator jobs enqueued: {enqueued}")
    await pool.close()


asyncio.run(main())
PYEOF

echo "--- Document status snapshot ---"
.venv/bin/python - <<'PYEOF'
from aegis.db import get_supabase

sb = get_supabase()
for s in ["manual_review", "proceed", "error", "pending"]:
    r = (
        sb.table("documents").select("id", count="exact").eq("parse_status", s).execute()
    )
    print(f"{s:20s}  {r.count}")
PYEOF

echo "--- Waiting on background-checks job ---"
wait $BG_PID
echo "Background checks complete"

echo "=== $(date) Ops batch complete ==="
