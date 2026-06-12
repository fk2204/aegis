.PHONY: install install-hooks dev worker test test-fast typecheck lint format check migrate migrate-dry verify-bedrock verify-db verify-db-list

install:
	uv sync

# Install the pre-commit framework with all three hooks chained: ruff,
# mypy --strict (src/aegis only), and the compliance-review annotation
# check on docs/compliance/states/**. The narrow no-CI exception
# documented in README.md. Re-runnable; idempotent.
#
# Migrates clones that ran the previous `core.hooksPath=.githooks` form
# by unsetting it first; pre-commit then takes over .git/hooks/pre-commit.
install-hooks:
	@git config --unset-all core.hooksPath 2>/dev/null || true
	@command -v pre-commit >/dev/null 2>&1 || uv tool install pre-commit
	pre-commit install
	@chmod +x .githooks/pre-commit
	@echo "[install-hooks] pre-commit installed; ruff + mypy + compliance-review chained."

dev:
	uv run uvicorn aegis.api.app:app --reload --port 5555

worker:
	uv run arq aegis.workers.WorkerSettings

# Fast iteration: skips slow/corpus tests. Use during development.
test-fast:
	uv run pytest -v

# Full test run including the Phase 5.5 corpus suite.
test:
	CORPUS=1 uv run pytest -v

typecheck:
	uv run mypy src tests scripts

lint:
	uv run ruff check src tests scripts

format:
	uv run ruff format src tests scripts

# `make check` is the pre-commit / pre-deploy gate.
# CORPUS=1 is set inside `test` so the operator can never accidentally
# ship without corpus validation. The opt-out is `make test-fast`.
check: typecheck lint test

# 3C-extra migration runner. The operator never SSHes or opens the
# Supabase SQL editor for migrations.
#
# Usage:
#   make migrate TARGET=dev
#   make migrate TARGET=prod DRY_RUN=1
#   make migrate TARGET=prod
#
# Reads scripts/db_checks-style .env.local for MIGRATIONS_DB_URL_<TARGET>.
# See deploy/RUNBOOK.md "Database migrations" for the audit-log query.
migrate:
	@if [ -z "$(TARGET)" ]; then \
		echo "usage: make migrate TARGET=<dev|staging|prod> [DRY_RUN=1]"; \
		exit 2; \
	fi
	uv run python scripts/apply_migrations.py --target $(TARGET) $(if $(DRY_RUN),--dry-run,)

migrate-dry:
	@if [ -z "$(TARGET)" ]; then \
		echo "usage: make migrate-dry TARGET=<dev|staging|prod>"; \
		exit 2; \
	fi
	uv run python scripts/apply_migrations.py --target $(TARGET) --dry-run

# Operator-zero-touch verification harnesses.
# See scripts/verify_bedrock.py and scripts/db_verify.py.
# The operator never SSHes into Hetzner or opens the Supabase SQL editor.
verify-bedrock:
	uv run python scripts/verify_bedrock.py $(ARGS)

# Usage: make verify-db CHECK=block-4-triggers-exist TARGET=prod
#        make verify-db CHECK=all TARGET=prod
verify-db:
	@if [ -z "$(CHECK)" ] || [ -z "$(TARGET)" ]; then \
		echo "usage: make verify-db CHECK=<name|all> TARGET=<dev|staging|prod>"; \
		exit 2; \
	fi
	uv run python scripts/db_verify.py --check $(CHECK) --target $(TARGET)

verify-db-list:
	uv run python scripts/db_verify.py --list
