"""One-shot: upsert merchant from a Close Lead + enqueue attachment parse.

Mirrors the webhook flow in ``aegis.api.routes.webhooks_close`` without
the HMAC layer. Used to recover when an inbound Close webhook didn't
fire (or the opportunity sub-status didn't match the trigger filter)
but the operator wants the attachments parsed *now*.

Idempotent: merchant upsert is read-before-write; attachment parse
short-circuits inside ``persist_pdf_upload`` on SHA256 dedup.

Usage on prod box (env loaded from /etc/aegis/aegis.env via systemd
or `set -a; source /etc/aegis/aegis.env; set +a`):

    uv run python scripts/manual_close_pull.py <close_lead_id> <actor_email>

Writes one audit row each:
    * close.manual_pull.merchant_upserted
    * close.orchestration.enqueued  (via shared helper logic)
"""

from __future__ import annotations

import asyncio
import sys
from uuid import uuid4

from aegis.api.deps import (
    get_audit,
    get_close_client,
    get_merchant_repository,
)
from aegis.api.routes.webhooks_close import _lead_to_merchant_fields
from aegis.merchants.models import MerchantRow
from aegis.workers import build_redis_settings


async def main(close_lead_id: str, actor_email: str) -> int:
    merchants = get_merchant_repository()
    audit = get_audit()
    close_client = get_close_client()

    print(f"[1/4] Fetching Close lead {close_lead_id} ...")
    lead = close_client.get_lead(close_lead_id)
    display = lead.get("display_name") or lead.get("name") or "?"
    print(f"      lead display_name={display!r}")

    print("[2/4] Building merchant fields from lead ...")
    new_fields = _lead_to_merchant_fields(lead, close_lead_id, audit)
    print(
        f"      business_name={new_fields['business_name']!r} "
        f"state={new_fields['state']!r} "
        f"requested={new_fields['requested_amount']}"
    )

    existing = merchants.find_by_close_lead_id(close_lead_id)
    if existing is None:
        new_merchant = MerchantRow(
            id=uuid4(),
            close_lead_id=close_lead_id,
            **new_fields,
        )
        merchants.upsert(new_merchant)
        merchant_id = new_merchant.id
        action = "created"
        print(f"[3/4] Merchant CREATED id={merchant_id}")
        audit.record(
            actor="manual_close_pull",
            actor_email=actor_email,
            action="close.manual_pull.merchant_upserted",
            subject_type="merchant",
            subject_id=merchant_id,
            details={
                "close_lead_id": close_lead_id,
                "result": "created",
                "business_name": new_merchant.business_name,
            },
        )
    else:
        diff = {
            key: val
            for key, val in new_fields.items()
            if getattr(existing, key, None) != val
        }
        if diff:
            updated = existing.model_copy(update=diff)
            merchants.upsert(updated)
            action = "updated"
            print(
                f"[3/4] Merchant UPDATED id={existing.id} "
                f"changed_keys={sorted(diff.keys())}"
            )
        else:
            action = "noop"
            print(f"[3/4] Merchant already up-to-date id={existing.id}")
        merchant_id = existing.id
        audit.record(
            actor="manual_close_pull",
            actor_email=actor_email,
            action="close.manual_pull.merchant_upserted",
            subject_type="merchant",
            subject_id=merchant_id,
            details={
                "close_lead_id": close_lead_id,
                "result": action,
                "changed_keys": sorted(diff.keys()) if action == "updated" else [],
            },
        )

    print("[4/4] Enqueueing process_close_attachments (trigger='rescan') ...")
    from arq import create_pool

    pool = await create_pool(build_redis_settings())
    try:
        job = await pool.enqueue_job(
            "process_close_attachments",
            close_lead_id,
            "rescan",
            actor_email=actor_email,
            override_cap=False,
        )
    finally:
        await pool.close()

    print(f"      enqueued job_id={job.job_id if job else 'NONE'}")
    audit.record(
        actor="manual_close_pull",
        actor_email=actor_email,
        action="close.orchestration.enqueued",
        subject_type="merchant",
        subject_id=merchant_id,
        details={
            "close_lead_id": close_lead_id,
            "trigger": "rescan",
            "override_cap": False,
            "source": "manual_close_pull",
        },
    )
    print("DONE — merchant ready, parse job enqueued, worker will pick it up.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(
            "usage: manual_close_pull.py <close_lead_id> <actor_email>",
            file=sys.stderr,
        )
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2])))
