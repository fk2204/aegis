"""Read-only digest of `bedrock.usage` audit rows (Phase 11 #2).

Renders the WeeklyDigest shape against a live audit_log so the operator
can see per-day spend + top-N most expensive parses without opening the
Supabase SQL editor.

Usage
-----
    python scripts/show_weekly_digest.py --target prod
    python scripts/show_weekly_digest.py --target prod --since "1 hour ago"
    python scripts/show_weekly_digest.py --target prod --since 2026-05-20
    python scripts/show_weekly_digest.py --target dev --since "30 days ago"

DSN env var convention matches scripts/apply_migrations.py:
``MIGRATIONS_DB_URL_<TARGET>`` (loaded from .env / .env.local on startup).

This script writes NOTHING. The query is a single SELECT against
audit_log; no transaction, no INSERT.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

# Operator-side tooling — bypass the prod-boot data-residency guard the
# app modules pull in via `aegis.config`. This script never invokes
# Bedrock; it only reads audit_log rows that the running app already
# wrote under the live US-only routing.
os.environ.setdefault("AEGIS_DATA_RESIDENCY_CONFIRMED", "true")

from aegis.ops.cost_tracking import build_weekly_digest  # noqa: E402

if TYPE_CHECKING:
    import psycopg

_DSN_ENV_BY_TARGET = {
    "dev": "MIGRATIONS_DB_URL_DEV",
    "staging": "MIGRATIONS_DB_URL_STAGING",
    "prod": "MIGRATIONS_DB_URL_PROD",
}

_REL_RE = re.compile(
    r"^(\d+)\s+(second|minute|hour|day|week)s?\s+ago$",
    re.IGNORECASE,
)

_UNIT_TO_SECONDS = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 604800,
}


def _load_dotenv_local() -> None:
    """Same as scripts/apply_migrations.py — load .env + .env.local."""
    for path in (REPO_ROOT / ".env", REPO_ROOT / ".env.local"):
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(
                key.strip(), value.strip().strip('"').strip("'")
            )


def _resolve_dsn(target: str) -> str:
    env_var = _DSN_ENV_BY_TARGET[target]
    dsn = os.environ.get(env_var, "").strip()
    if not dsn:
        raise SystemExit(
            f"ERROR: {env_var} is not set. Add it to .env.local "
            "(same convention as apply_migrations.py)."
        )
    return dsn


def _parse_since(value: str) -> datetime:
    """Accept ISO date / datetime, or a relative phrase like '1 day ago'."""
    rel = _REL_RE.match(value.strip())
    if rel:
        n = int(rel.group(1))
        unit = rel.group(2).lower()
        return datetime.now(UTC) - timedelta(seconds=n * _UNIT_TO_SECONDS[unit])
    try:
        # Bare date ('2026-05-20') becomes midnight UTC.
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(
            f"ERROR: --since {value!r} is neither ISO ('YYYY-MM-DD' or full "
            "ISO datetime) nor a relative phrase ('N units ago'). "
            f"Units: {sorted(_UNIT_TO_SECONDS)}."
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _fetch_usage_rows(conn: psycopg.Connection, since: datetime) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT subject_type, subject_id, details, created_at
            FROM audit_log
            WHERE action = 'bedrock.usage' AND created_at >= %s
            ORDER BY created_at
            """,
            (since,),
        )
        rows = cur.fetchall()
    return [
        {
            "subject_type": r[0],
            "subject_id": str(r[1]) if r[1] else None,
            "details": r[2] or {},
            "created_at": r[3],
            "action": "bedrock.usage",
        }
        for r in rows
    ]


def _per_day_rollup(
    rows: list[dict[str, Any]],
) -> list[tuple[str, int, int, int, Decimal]]:
    """One tuple per UTC date: (date_iso, calls, in_tokens, out_tokens, total_cost)."""
    by_day: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "in": 0, "out": 0, "cost": Decimal("0")}
    )
    for r in rows:
        created = r["created_at"]
        if not isinstance(created, datetime):
            continue
        day = created.astimezone(UTC).date().isoformat()
        details = r["details"]
        by_day[day]["calls"] += 1
        by_day[day]["in"] += int(details.get("input_tokens", 0))
        by_day[day]["out"] += int(details.get("output_tokens", 0))
        by_day[day]["cost"] += Decimal(str(details.get("total_cost_usd", "0")))
    return sorted(
        (
            (day, b["calls"], b["in"], b["out"], b["cost"])
            for day, b in by_day.items()
        ),
        key=lambda t: t[0],
    )


def _fmt_money(value: Decimal) -> str:
    return f"${value.quantize(Decimal('0.000001'))}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        choices=("dev", "staging", "prod"),
        required=True,
        help="Which DB to query — picks MIGRATIONS_DB_URL_<TARGET>.",
    )
    parser.add_argument(
        "--since",
        default="7 days ago",
        help=(
            "Window start. Accepts ISO ('2026-05-20', '2026-05-20T12:00:00Z') "
            "or a relative phrase ('1 hour ago', '7 days ago')."
        ),
    )
    parser.add_argument(
        "--top",
        type=int,
        default=5,
        help="Show the top-N most expensive documents (default 5).",
    )
    args = parser.parse_args()

    _load_dotenv_local()
    dsn = _resolve_dsn(args.target)
    since = _parse_since(args.since)
    window_end = datetime.now(UTC)

    import psycopg

    with psycopg.connect(dsn) as conn:
        rows = _fetch_usage_rows(conn, since)

    if not rows:
        print(f"No bedrock.usage rows in audit_log since {since.isoformat()}.")
        print("(Nothing yet parsed under the CostTrackingBedrockClient wrap?)")
        return 0

    digest = build_weekly_digest(
        rows,
        window_start=since.isoformat(),
        window_end=window_end.isoformat(),
    )
    per_day = _per_day_rollup(rows)

    print(f"Target:        {args.target}")
    print(f"Window:        {since.isoformat()}  -> {window_end.isoformat()}")
    print(f"Total calls:   {digest.total_calls}")
    print(f"Total tokens:  in={digest.total_input_tokens:,}  "
          f"out={digest.total_output_tokens:,}")
    print(f"Total spend:   {_fmt_money(digest.total_cost_usd)}")
    print(f"Avg per deal:  {_fmt_money(digest.avg_cost_per_deal)}")
    if digest.avg_cost_per_funded_deal is not None:
        print(f"Avg per funded: {_fmt_money(digest.avg_cost_per_funded_deal)}")
    print()

    print("Per-day rollup (UTC dates)")
    print("  date          calls    in_tokens    out_tokens    total_cost")
    for day, calls, ti, to, cost in per_day:
        print(
            f"  {day}    {calls:>4}     {ti:>8,}     {to:>9,}     {_fmt_money(cost)}"
        )
    print()

    deals_with_subject = [d for d in digest.deals if d.document_id is not None]
    print(f"Top {min(args.top, len(deals_with_subject))} documents by cost")
    print(
        "  rank  document_id                              calls    "
        "total_cost     in / out tokens"
    )
    for i, deal in enumerate(deals_with_subject[: args.top], start=1):
        print(
            f"  {i:>3}.  {deal.document_id!s:<40} {deal.call_count:>4}    "
            f"{_fmt_money(deal.total_cost_usd):>12}     "
            f"{deal.input_tokens:,} / {deal.output_tokens:,}"
        )

    no_subject = sum(1 for d in digest.deals if d.document_id is None)
    if no_subject:
        print()
        print(
            f"({no_subject} bedrock.usage row(s) without a document subject — "
            "ad-hoc calls outside parse_document.)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
