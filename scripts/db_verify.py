"""Operator-zero-touch DB verification harness.

Runs named SQL checks against a Supabase Postgres instance so the operator
never has to open the Supabase SQL editor for routine verification.

Each check lives as a `.sql` file under `scripts/db_checks/<name>.sql`. The
file body is a single SELECT. Optional headers at the top of the file
configure pass/fail semantics:

    -- EXPECT_ROWS: 2          (exact row count required)
    -- EXPECT_ROWS_MIN: 1      (lower bound; for "at least one" assertions)
    -- DESCRIPTION: one-line summary printed in the report

Without an EXPECT_ROWS* header the script reports the row count but does
not assert on it — useful for checks that need human interpretation.

DSN selection is per-environment, matching the locked 3C-extra migration
runner spec. Resolved URLs that match the prod project ref
(`tprpbomqcucuxnszeafo`) require `--target prod` explicitly; anything else
is rejected before a connection is opened.

Usage:
    uv run python scripts/db_verify.py --target prod --check block-4-triggers-exist
    uv run python scripts/db_verify.py --target prod --check all
    uv run python scripts/db_verify.py --list

The script is read-only by design: it opens a connection in read-only mode
and refuses any check whose SQL contains a top-level INSERT/UPDATE/DELETE
verb. Verification must never be able to mutate prod.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKS_DIR = REPO_ROOT / "scripts" / "db_checks"

PROD_PROJECT_REF = "tprpbomqcucuxnszeafo"

_DSN_ENV_BY_TARGET = {
    "dev": "MIGRATIONS_DB_URL_DEV",
    "staging": "MIGRATIONS_DB_URL_STAGING",
    "prod": "MIGRATIONS_DB_URL_PROD",
}

_WRITE_KEYWORDS = re.compile(
    r"^\s*(insert|update|delete|truncate|drop|create|alter|grant|revoke|copy)\b",
    re.IGNORECASE | re.MULTILINE,
)


class VerifyError(RuntimeError):
    """Raised when a check cannot be run (config, missing file, etc.)."""


@dataclass(frozen=True)
class CheckSpec:
    name: str
    description: str
    expect_rows: int | None
    expect_rows_min: int | None
    sql: str


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    row_count: int
    rows: list[dict[str, Any]]
    detail: str


def _load_dotenv_local() -> None:
    """Load .env.local on top of .env without touching pydantic's settings cache.

    .env.local is the operator-only file with prod DSN strings. Variables
    already present in os.environ are not overwritten (so CI / shell exports
    still win).
    """
    for path in (REPO_ROOT / ".env", REPO_ROOT / ".env.local"):
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def _parse_headers(sql_body: str) -> tuple[str, int | None, int | None]:
    description = ""
    expect_rows: int | None = None
    expect_rows_min: int | None = None
    for raw in sql_body.splitlines():
        line = raw.strip()
        if not line.startswith("--"):
            break
        body = line.lstrip("-").strip()
        if body.upper().startswith("EXPECT_ROWS:"):
            expect_rows = int(body.split(":", 1)[1].strip())
        elif body.upper().startswith("EXPECT_ROWS_MIN:"):
            expect_rows_min = int(body.split(":", 1)[1].strip())
        elif body.upper().startswith("DESCRIPTION:"):
            description = body.split(":", 1)[1].strip()
    return description, expect_rows, expect_rows_min


def _load_check(name: str) -> CheckSpec:
    path = CHECKS_DIR / f"{name}.sql"
    if not path.exists():
        raise VerifyError(
            f"check {name!r} not found at {path}. Run --list to see available checks."
        )
    sql = path.read_text(encoding="utf-8")
    if _WRITE_KEYWORDS.search(sql):
        raise VerifyError(
            f"check {name!r} contains a write keyword (insert/update/delete/etc.); "
            "verification SQL must be read-only"
        )
    description, expect_rows, expect_rows_min = _parse_headers(sql)
    return CheckSpec(
        name=name,
        description=description or "(no description)",
        expect_rows=expect_rows,
        expect_rows_min=expect_rows_min,
        sql=sql,
    )


def _list_checks() -> list[str]:
    if not CHECKS_DIR.exists():
        return []
    return sorted(p.stem for p in CHECKS_DIR.glob("*.sql"))


def _resolve_dsn(target: str) -> str:
    env_var = _DSN_ENV_BY_TARGET.get(target)
    if env_var is None:
        raise VerifyError(f"unknown target {target!r}; expected dev|staging|prod")
    dsn = os.environ.get(env_var, "").strip()
    if not dsn:
        raise VerifyError(
            f"{env_var} is not set. Add it to .env.local. "
            "Get the URI from Supabase dashboard → Settings → Database → Connection string (URI)."
        )
    points_at_prod = PROD_PROJECT_REF in dsn
    if points_at_prod and target != "prod":
        raise VerifyError(
            f"refusing to connect: --target={target} but DSN points at prod project "
            f"{PROD_PROJECT_REF}. Use --target prod or fix the DSN in .env.local."
        )
    return dsn


def _run_check(dsn: str, spec: CheckSpec) -> CheckResult:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, autocommit=False) as conn:
        conn.read_only = True
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(spec.sql)
            rows: list[dict[str, Any]] = list(cur.fetchall())
    row_count = len(rows)

    passed = True
    detail_bits: list[str] = []
    if spec.expect_rows is not None:
        ok = row_count == spec.expect_rows
        passed = passed and ok
        detail_bits.append(
            f"expect_rows={spec.expect_rows} actual={row_count} "
            f"{'OK' if ok else 'MISMATCH'}"
        )
    if spec.expect_rows_min is not None:
        ok = row_count >= spec.expect_rows_min
        passed = passed and ok
        detail_bits.append(
            f"expect_rows_min={spec.expect_rows_min} actual={row_count} "
            f"{'OK' if ok else 'UNDER'}"
        )
    if not detail_bits:
        detail_bits.append(f"row_count={row_count} (no expectation declared)")
    return CheckResult(
        name=spec.name,
        passed=passed,
        row_count=row_count,
        rows=rows,
        detail="; ".join(detail_bits),
    )


def _print_result(result: CheckResult, spec: CheckSpec) -> None:
    mark = "PASS" if result.passed else "FAIL"
    print(f"[{mark}] check={result.name}  rows={result.row_count}  ({result.detail})")
    if spec.description and spec.description != "(no description)":
        print(f"       desc: {spec.description}")
    if result.rows:
        print("       rows:")
        # Convert any non-JSON-serializable types (UUID, datetime) via default=str.
        for row in result.rows[:10]:
            print(f"         {json.dumps(row, default=str, sort_keys=True)}")
        if len(result.rows) > 10:
            print(f"         ... ({len(result.rows) - 10} more)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        choices=("dev", "staging", "prod"),
        help="Which environment to verify. Required unless --list.",
    )
    parser.add_argument(
        "--check",
        help="Check name (file stem under scripts/db_checks/) or 'all'.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available checks and exit.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Optional JSON output path with full results.",
    )
    args = parser.parse_args()

    _load_dotenv_local()

    if args.list:
        names = _list_checks()
        if not names:
            print(f"No checks found in {CHECKS_DIR}", file=sys.stderr)
            return 2
        print("Available checks:")
        for name in names:
            try:
                spec = _load_check(name)
                print(f"  {name:<32}  {spec.description}")
            except VerifyError as exc:
                print(f"  {name:<32}  (load error: {exc})")
        return 0

    if not args.target or not args.check:
        parser.error("--target and --check are required (or use --list)")

    try:
        dsn = _resolve_dsn(args.target)
    except VerifyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.check == "all":
        names = _list_checks()
        if not names:
            print(f"No checks found in {CHECKS_DIR}", file=sys.stderr)
            return 2
    else:
        names = [args.check]

    specs: list[CheckSpec] = []
    try:
        specs = [_load_check(n) for n in names]
    except VerifyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    results: list[CheckResult] = []
    any_failed = False
    for spec in specs:
        try:
            result = _run_check(dsn, spec)
        except Exception as exc:
            print(f"[FAIL] check={spec.name}  exception={type(exc).__name__}: {exc}")
            any_failed = True
            continue
        results.append(result)
        _print_result(result, spec)
        if not result.passed:
            any_failed = True

    if args.out:
        payload = {
            "target": args.target,
            "results": [
                {
                    "name": r.name,
                    "passed": r.passed,
                    "row_count": r.row_count,
                    "detail": r.detail,
                    "rows": r.rows,
                }
                for r in results
            ],
        }
        args.out.write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )
        print(f"Wrote {args.out}")

    print()
    if any_failed:
        print("OVERALL: FAIL")
        return 1
    print("OVERALL: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
