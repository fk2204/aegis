"""One-shot: persist N local PDFs to a specific merchant + enqueue parse.

Operator-supplied PDFs that didn't come through Close (e.g. a second
bank account whose statements were emailed/dropped directly). Uses the
same ``persist_pdf_upload`` path the dashboard upload route uses, so
SHA256 dedup + audit + parse-enqueue all flow through the shared
helper. No new write codepath.

Usage on prod box (env loaded from /etc/aegis/aegis.env):

    uv run python scripts/manual_persist_local_pdfs.py \\
        <merchant_id> <actor_email> <pdf_path>...
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import UUID

from aegis.api.deps import get_audit, get_repository
from aegis.api.routes.upload import persist_pdf_upload
from aegis.workers import build_redis_settings


async def main(merchant_id_str: str, actor_email: str, paths: list[str]) -> int:
    merchant_id = UUID(merchant_id_str)
    repository = get_repository()
    audit = get_audit()

    from arq import create_pool

    pool = await create_pool(build_redis_settings())

    async def _enqueue(document_id: UUID, file_hash: str) -> None:
        await pool.enqueue_job("parse_document", str(document_id), file_hash)

    print(f"Persisting {len(paths)} PDF(s) to merchant {merchant_id} ...")
    print()
    try:
        for i, raw_path in enumerate(paths, 1):
            p = Path(raw_path)
            if not p.exists():
                print(f"  [{i}/{len(paths)}] MISSING {p}")
                continue
            body = p.read_bytes()
            print(f"  [{i}/{len(paths)}] {p.name}  ({len(body)} bytes)")
            resp = await persist_pdf_upload(
                enqueue_parse=_enqueue,
                body=body,
                original_filename=p.name,
                repository=repository,
                audit=audit,
                actor="manual_persist_local_pdfs",
                actor_email=actor_email,
                merchant_id=merchant_id,
            )
            tag = "DUP" if resp.duplicate_of_existing else "NEW"
            print(
                f"      {tag} document_id={resp.document_id}  "
                f"parse_status={resp.parse_status}"
            )
    finally:
        await pool.close(close_connection_pool=True)

    print()
    print("Done — worker will pick up the parse jobs.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(
            "usage: manual_persist_local_pdfs.py "
            "<merchant_id> <actor_email> <pdf_path>...",
            file=sys.stderr,
        )
        sys.exit(2)
    sys.exit(
        asyncio.run(
            main(
                merchant_id_str=sys.argv[1],
                actor_email=sys.argv[2],
                paths=sys.argv[3:],
            )
        )
    )
