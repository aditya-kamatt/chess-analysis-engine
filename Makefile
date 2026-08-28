.PHONY: sync lint format-check typecheck test-backend test-engine test-frontend build package ci

sync:
	uv sync --locked

lint:
	uv run ruff check .
	cd frontend && npm run lint

format-check:
	uv run ruff format --check .
	cd frontend && npm run format:check

typecheck:
	cd frontend && npm run typecheck

# Fast tests that do not require a Stockfish binary.
test-backend:
	uv run pytest tests/ --ignore=tests/test_stockfish.py --cov=chess_analysis --cov-report=term-missing --cov-fail-under=85

# The engine tests are kept separate because they download and execute Stockfish.
test-engine:
	./scripts/fetch-stockfish.sh
	uv run pytest tests/test_stockfish.py

test-frontend:
	cd frontend && npm ci && npm run test:coverage

build:
	cd frontend && npm run build

security:
	uv export --format requirements-txt --no-emit-project --output-file /tmp/chess-analysis-requirements.txt
	uv run pip-audit -r /tmp/chess-analysis-requirements.txt --strict --cache-dir /tmp/pip-audit-cache
	cd frontend && npm audit --audit-level=high

package:
	uv build
	uv venv /tmp/chess-analysis-engine-package-test
	uv pip install --python /tmp/chess-analysis-engine-package-test/bin/python dist/*.whl

# Complete non-browser local gate; CI runs these checks in parallel.
ci: sync lint format-check typecheck test-backend test-engine test-frontend build package
