.PHONY: install install-hooks dev worker test test-fast typecheck lint format check migrate migrate-dry

install:
	uv sync

# Install the pre-commit hook that enforces compliance-review annotations
# on commits touching docs/compliance/states/**. The narrow no-CI
# exception documented in README.md. Re-runnable; idempotent.
install-hooks:
	git config core.hooksPath .githooks
	chmod +x .githooks/pre-commit
	@echo "[install-hooks] core.hooksPath -> .githooks; pre-commit executable."

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
