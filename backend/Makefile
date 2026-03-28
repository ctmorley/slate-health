# =============================================================================
# Slate Health Backend — Development Makefile
# =============================================================================
# Run from the backend/ directory, or use the root Makefile for project-wide tasks.

.PHONY: help test test-unit test-integration test-cov migrate serve lint

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

db-init: ## Bootstrap local PostgreSQL: create role + database
	@echo "Creating PostgreSQL role 'slate' and database 'slate_health'..."
	@echo "  (Requires a running PostgreSQL instance and 'psql' on PATH)"
	@psql -h localhost -U postgres -c "SELECT 1 FROM pg_roles WHERE rolname='slate'" | grep -q 1 || \
		psql -h localhost -U postgres -c "CREATE ROLE slate WITH LOGIN PASSWORD 'slate';"
	@psql -h localhost -U postgres -c "SELECT 1 FROM pg_catalog.pg_database WHERE datname='slate_health'" | grep -q 1 || \
		psql -h localhost -U postgres -c "CREATE DATABASE slate_health OWNER slate;"
	@echo "Done. Run 'make migrate' to apply migrations."

migrate: ## Run Alembic migrations against local database
	python -m alembic upgrade head

# ---------------------------------------------------------------------------
# Development
# ---------------------------------------------------------------------------

serve: ## Run backend locally with hot reload
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------

test: ## Run all tests
	python -m pytest tests/ -q

test-unit: ## Run unit tests only
	python -m pytest tests/unit/ -q

test-integration: ## Run integration tests only
	python -m pytest tests/integration/ -q

test-cov: ## Run tests with coverage report
	python -m pytest tests/ --cov=app --cov-report=term-missing -q

test-ci: ## Run tests in CI mode (fails if release-gate tests skip when Docker available)
	CI=true python -m pytest tests/ -q

# ---------------------------------------------------------------------------
# Code Quality
# ---------------------------------------------------------------------------

lint: ## Run type checking and linting (if configured)
	python -m mypy app/ --ignore-missing-imports || true
