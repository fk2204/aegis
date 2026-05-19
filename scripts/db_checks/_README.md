# `scripts/db_checks/` — verification SQL

Drop a `.sql` file in here and `scripts/db_verify.py` picks it up automatically.

## Conventions

- **One SELECT per file.** Multi-statement files are rejected.
- **No writes.** The runner refuses any file containing `insert`, `update`, `delete`, `truncate`, `drop`, `create`, `alter`, `grant`, `revoke`, or `copy` at statement-start. The connection is also opened read-only.
- **Optional headers** (comment lines at the top):
  - `-- DESCRIPTION: one-line summary` — shown in `--list` output.
  - `-- EXPECT_ROWS: <n>` — assert exact row count; FAIL otherwise.
  - `-- EXPECT_ROWS_MIN: <n>` — assert lower bound; FAIL if fewer rows returned.

If neither expectation is declared the runner reports the row count without pass/fail semantics — useful for human-interpreted checks.

## Run

```
make verify-db CHECK=block-4-triggers-exist TARGET=prod
make verify-db CHECK=all TARGET=prod
uv run python scripts/db_verify.py --list
```

## Naming

`block-N-<what>.sql` for protocol blocks (block-4 = decisions immutability). Generic checks use plain kebab-case (`triggers-immutable.sql`, `audit-log-recent.sql`, etc.).
