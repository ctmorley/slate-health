#!/usr/bin/env bash
# Sprint 1 Acceptance Verification Script
# Run this in a Docker-enabled environment to execute the full acceptance checklist.
#
# Usage:
#   cd <repo-root>
#   bash scripts/verify-sprint1.sh
#
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass() { echo -e "${GREEN}PASS${NC}: $1"; }
fail() { echo -e "${RED}FAIL${NC}: $1"; exit 1; }
info() { echo -e "${YELLOW}INFO${NC}: $1"; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

echo "============================================================"
echo " Sprint 1 — Acceptance Verification"
echo "============================================================"
echo ""

# ── Preflight: Detect Docker & Compose availability ──────────────
if ! command -v docker &>/dev/null; then
  echo -e "${RED}PREFLIGHT FAIL${NC}: 'docker' command not found."
  echo ""
  echo "  This verification script requires a running Docker daemon."
  echo "  Please install Docker Desktop (https://www.docker.com/products/docker-desktop)"
  echo "  or ensure the Docker CLI is on your PATH, then re-run this script."
  exit 1
fi

if ! docker info &>/dev/null; then
  echo -e "${RED}PREFLIGHT FAIL${NC}: Docker daemon is not running."
  echo ""
  echo "  The 'docker' CLI is installed but cannot connect to the daemon."
  echo "  Please start Docker Desktop or the Docker service, then re-run this script."
  exit 1
fi

# Resolve compose command: prefer 'docker compose' plugin, fall back to 'docker-compose'
if docker compose version &>/dev/null; then
  COMPOSE_CMD="docker compose"
elif command -v docker-compose &>/dev/null; then
  COMPOSE_CMD="docker-compose"
  info "Using legacy 'docker-compose' command (plugin 'docker compose' not available)."
else
  echo -e "${RED}PREFLIGHT FAIL${NC}: Neither 'docker compose' plugin nor 'docker-compose' found."
  echo ""
  echo "  Please install Docker Compose:"
  echo "    - Plugin (recommended): https://docs.docker.com/compose/install/"
  echo "    - Standalone: pip install docker-compose"
  exit 1
fi

info "Using compose command: $COMPOSE_CMD"
echo ""

# ── 1. Docker Compose ────────────────────────────────────────────
info "Step 1: Starting services with docker compose..."
$COMPOSE_CMD down -v 2>/dev/null || true
$COMPOSE_CMD up -d --build --wait
pass "docker compose up — services started"

echo ""

# ── 2. Verify services healthy ──────────────────────────────────
info "Step 2: Checking service health..."
$COMPOSE_CMD ps
BACKEND_STATUS=$($COMPOSE_CMD ps backend --format '{{.Health}}' 2>/dev/null || echo "unknown")
if [[ "$BACKEND_STATUS" == *"healthy"* ]]; then
  pass "Backend service is healthy"
else
  # Give it a few more seconds if still starting
  info "Waiting 15s for backend to become healthy..."
  sleep 15
  BACKEND_STATUS=$($COMPOSE_CMD ps backend --format '{{.Health}}' 2>/dev/null || echo "unknown")
  [[ "$BACKEND_STATUS" == *"healthy"* ]] || fail "Backend not healthy: $BACKEND_STATUS"
  pass "Backend service is healthy (after wait)"
fi

echo ""

# ── 3. Health endpoint ──────────────────────────────────────────
info "Step 3: GET /health..."
HTTP_CODE=$(curl -s -o /tmp/health.json -w "%{http_code}" http://localhost:8000/health)
BODY=$(cat /tmp/health.json)
echo "  HTTP $HTTP_CODE — $BODY"
[[ "$HTTP_CODE" == "200" ]] || fail "/health returned $HTTP_CODE"
echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['status']=='healthy'" \
  || fail "/health response missing status=healthy"
pass "GET /health → 200 {\"status\": \"healthy\"}"

echo ""

# ── 4. Alembic migrations & table check ────────────────────────
info "Step 4: Verifying tables in PostgreSQL..."
TABLES=$($COMPOSE_CMD exec -T postgres psql -U slate -d slate_health -t -c \
  "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' AND table_name != 'alembic_version' ORDER BY table_name;")

EXPECTED_TABLES=(
  users organizations patients encounters
  agent_tasks workflow_executions hitl_reviews
  audit_logs phi_access_log
  payers payer_rules clearinghouse_configs
  eligibility_checks claims claim_denials
  prior_auth_requests prior_auth_appeals
  scheduling_requests credentialing_applications compliance_reports
)

MISSING=()
for T in "${EXPECTED_TABLES[@]}"; do
  if echo "$TABLES" | grep -qw "$T"; then
    echo "  OK: $T"
  else
    echo "  MISSING: $T"
    MISSING+=("$T")
  fi
done

[[ ${#MISSING[@]} -eq 0 ]] || fail "Missing tables: ${MISSING[*]}"
pass "All ${#EXPECTED_TABLES[@]} expected tables exist in PostgreSQL"

echo ""

# ── 5. Alembic round-trip ──────────────────────────────────────
info "Step 5: Alembic downgrade → upgrade round-trip..."
$COMPOSE_CMD exec -T backend python -m alembic downgrade base
$COMPOSE_CMD exec -T backend python -m alembic upgrade head
AFTER_COUNT=$($COMPOSE_CMD exec -T postgres psql -U slate -d slate_health -t -c \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' AND table_name != 'alembic_version';" | tr -d ' ')
[[ "$AFTER_COUNT" -ge 18 ]] || fail "Round-trip produced $AFTER_COUNT tables (expected >= 18)"
pass "Alembic round-trip OK ($AFTER_COUNT tables)"

echo ""

# ── 6. Unit tests ──────────────────────────────────────────────
info "Step 6: Running pytest (CI=true — skipped PG tests = failure)..."
cd "$REPO_ROOT/backend"
CI=true python -m pytest tests/ -v --tb=short 2>&1 | tee /tmp/pytest-output.txt
RESULT=${PIPESTATUS[0]}
cd "$REPO_ROOT"
[[ "$RESULT" -eq 0 ]] || fail "pytest exited with code $RESULT"

# Release gate: PostgreSQL integration tests must have *passed*, not skipped
if grep -q "SKIPPED.*test_postgres_migrations" /tmp/pytest-output.txt; then
  fail "PostgreSQL integration tests were SKIPPED — release gate requires them to PASS"
fi
PG_PASSED=$(grep -c "PASSED.*test_postgres_migrations" /tmp/pytest-output.txt || true)
[[ "$PG_PASSED" -ge 4 ]] || fail "Expected ≥4 PostgreSQL integration tests PASSED, got $PG_PASSED"
pass "pytest — all tests passed ($PG_PASSED PostgreSQL integration tests confirmed)"

echo ""

# ── 7. Cleanup ─────────────────────────────────────────────────
info "Step 7: Stopping services..."
$COMPOSE_CMD down -v
pass "Cleanup complete"

echo ""
echo "============================================================"
echo -e " ${GREEN}ALL SPRINT 1 ACCEPTANCE CRITERIA VERIFIED${NC}"
echo "============================================================"
