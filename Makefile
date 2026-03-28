# =============================================================================
# Slate Health — Development Makefile
# =============================================================================

.PHONY: help db-init db-migrate dev test test-unit test-integration clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

db-init: ## Bootstrap local PostgreSQL: create role + database for development
	@echo "Creating PostgreSQL role 'slate' and database 'slate_health'..."
	@echo "  (Requires a running PostgreSQL instance and 'psql' on PATH)"
	@psql -h localhost -U postgres -c "SELECT 1 FROM pg_roles WHERE rolname='slate'" | grep -q 1 || \
		psql -h localhost -U postgres -c "CREATE ROLE slate WITH LOGIN PASSWORD 'slate';"
	@psql -h localhost -U postgres -c "SELECT 1 FROM pg_catalog.pg_database WHERE datname='slate_health'" | grep -q 1 || \
		psql -h localhost -U postgres -c "CREATE DATABASE slate_health OWNER slate;"
	@echo "Done. Run 'make db-migrate' to apply migrations."

db-init-docker: ## Bootstrap DB via Docker Compose (recommended)
	docker compose up -d postgres
	@echo "Waiting for PostgreSQL to be ready..."
	@sleep 2
	docker compose run --rm migrate
	@echo "Database ready."

db-migrate: ## Run Alembic migrations against local database
	cd backend && python -m alembic upgrade head

# ---------------------------------------------------------------------------
# Development
# ---------------------------------------------------------------------------

dev: ## Start all services via Docker Compose
	docker compose up --build

dev-backend: ## Run backend locally (requires local PostgreSQL)
	cd backend && uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------

test: ## Run all tests
	cd backend && python -m pytest tests/ -q

test-unit: ## Run unit tests only
	cd backend && python -m pytest tests/unit/ -q

test-integration: ## Run integration tests only
	cd backend && python -m pytest tests/integration/ -q

test-cov: ## Run tests with coverage report
	cd backend && python -m pytest tests/ --cov=app --cov-report=term-missing -q

# ---------------------------------------------------------------------------
# Release Gate (Docker E2E — mandatory for sprint signoff)
# ---------------------------------------------------------------------------

test-release: ## Run FULL release verification (requires Docker)
	@echo "═══════════════════════════════════════════════════════════════"
	@echo "  Slate Health — Release Gate Verification"
	@echo "═══════════════════════════════════════════════════════════════"
	@echo ""
	@echo "Step 1/4: Starting Docker Compose stack..."
	docker compose up -d --build --wait
	@echo ""
	@echo "Step 2/4: Running unit + integration tests with coverage..."
	cd backend && CI=true DOCKER_AVAILABLE=1 python -m pytest tests/unit/ tests/integration/ \
		--cov=app --cov-report=term-missing -q
	@echo ""
	@echo "Step 3/4: Running Docker E2E tests (full lifecycle for all 6 agents)..."
	cd backend && DOCKER_E2E=1 CI=true python -m pytest tests/e2e/test_docker_e2e.py -v
	@echo ""
	@echo "Step 4/4: Running PostgreSQL migration integration tests..."
	cd backend && DOCKER_AVAILABLE=1 CI=true python -m pytest tests/integration/test_postgres_migrations.py -v
	@echo ""
	@echo "═══════════════════════════════════════════════════════════════"
	@echo "  ✓ Release gate verification PASSED"
	@echo "═══════════════════════════════════════════════════════════════"

test-docker-e2e: ## Run Docker E2E tests only (requires running stack)
	cd backend && DOCKER_E2E=1 python -m pytest tests/e2e/test_docker_e2e.py -v

test-postgres: ## Run PostgreSQL migration tests (requires Docker)
	cd backend && DOCKER_AVAILABLE=1 python -m pytest tests/integration/test_postgres_migrations.py -v

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

clean: ## Stop Docker Compose and remove volumes
	docker compose down -v
