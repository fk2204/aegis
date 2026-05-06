.PHONY: install dev worker test typecheck lint format check

install:
	uv sync

dev:
	uv run uvicorn aegis.api.app:app --reload --port 5555

worker:
	uv run arq aegis.workers.WorkerSettings

test:
	uv run pytest -v

typecheck:
	uv run mypy src tests

lint:
	uv run ruff check src tests

format:
	uv run ruff format src tests

check: typecheck lint test
