#!/usr/bin/env bash
#
# Deterministic E2E test harness for Slate Health frontend.
#
# This script:
#   1. Verifies Docker/Compose are available (fails fast with clear message if not)
#   2. Checks port availability (fails fast if another process occupies the port)
#   3. Starts the Docker Compose stack (builds images if needed)
#   4. Waits for frontend + backend health checks to pass
#   5. Runs the Vitest E2E suite (e2e-docker tests)
#   6. Runs Playwright browser redirect tests (mandatory by default)
#   7. Tears down the stack on exit (success or failure)
#
# Usage:
#   ./scripts/run-e2e.sh                 # Full E2E: Vitest + Playwright (recommended)
#   ./scripts/run-e2e.sh --no-playwright # Vitest E2E only (skip browser tests)
#
# Environment variables:
#   FRONTEND_PORT  - Port for frontend (default: 3000)
#   FRONTEND_HOST  - Hostname for frontend (default: localhost)
#   COMPOSE_FILE   - Path to docker-compose file (default: auto-detected)
#   SKIP_BUILD     - Set to "1" to skip Docker image rebuild
#   KEEP_STACK     - Set to "1" to skip teardown (useful for debugging)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
FRONTEND_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

FRONTEND_HOST="${FRONTEND_HOST:-localhost}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
FRONTEND_URL="http://${FRONTEND_HOST}:${FRONTEND_PORT}"
COMPOSE_PROJECT="slate-e2e"
RUN_PLAYWRIGHT=true
EXIT_CODE=0

for arg in "$@"; do
  case "$arg" in
    --no-playwright) RUN_PLAYWRIGHT=false ;;
    --help|-h)
      echo "Usage: $0 [--no-playwright]"
      echo ""
      echo "Runs full E2E tests including Vitest and Playwright browser redirect tests."
      echo ""
      echo "  --no-playwright  Skip Playwright browser tests (not recommended)"
      exit 0
      ;;
  esac
done

# ── Preflight checks ──────────────────────────────────────────────────────────

check_command() {
  if ! command -v "$1" &>/dev/null; then
    echo "ERROR: '$1' is not installed or not in PATH."
    echo ""
    echo "This E2E test suite requires Docker and Docker Compose."
    echo "Install Docker Desktop: https://docs.docker.com/get-docker/"
    echo ""
    echo "If Docker is installed but not in PATH, ensure the Docker CLI"
    echo "is available (e.g., open Docker Desktop, or add to PATH)."
    exit 1
  fi
}

check_command docker
# docker compose (v2 plugin) — fall back to docker-compose (v1) if needed
if docker compose version &>/dev/null; then
  COMPOSE_CMD="docker compose"
elif command -v docker-compose &>/dev/null; then
  COMPOSE_CMD="docker-compose"
else
  echo "ERROR: Neither 'docker compose' (v2) nor 'docker-compose' (v1) found."
  echo "Install Docker Compose: https://docs.docker.com/compose/install/"
  exit 1
fi

# ── Port occupancy check ─────────────────────────────────────────────────────
# Fail fast if the frontend port is already in use by another process.
# This prevents false positives/negatives from an unrelated service.

check_port_available() {
  local port="$1"
  local pid=""
  local process_info=""

  # Try lsof first (macOS + Linux)
  if command -v lsof &>/dev/null; then
    pid=$(lsof -ti :"$port" 2>/dev/null | head -1 || true)
    if [ -n "$pid" ]; then
      process_info=$(ps -p "$pid" -o pid=,comm=,args= 2>/dev/null || echo "PID $pid (details unavailable)")
    fi
  # Fall back to ss (Linux)
  elif command -v ss &>/dev/null; then
    if ss -tlnp "sport = :$port" 2>/dev/null | grep -q ":$port"; then
      process_info=$(ss -tlnp "sport = :$port" 2>/dev/null | tail -1)
    fi
  fi

  if [ -n "$process_info" ]; then
    echo "ERROR: Port $port is already in use by another process."
    echo ""
    echo "  Process: $process_info"
    echo ""
    echo "The E2E tests need port $port for the Slate Health frontend."
    echo "Either:"
    echo "  1. Stop the process using port $port"
    echo "  2. Use a different port: FRONTEND_PORT=3210 $0"
    echo ""
    exit 1
  fi
}

check_port_available "$FRONTEND_PORT"

# Locate compose file
if [ -n "${COMPOSE_FILE:-}" ]; then
  COMPOSE_ARGS="-f $COMPOSE_FILE"
elif [ -f "$PROJECT_ROOT/docker-compose.yml" ]; then
  COMPOSE_ARGS="-f $PROJECT_ROOT/docker-compose.yml"
elif [ -f "$PROJECT_ROOT/compose.yml" ]; then
  COMPOSE_ARGS="-f $PROJECT_ROOT/compose.yml"
else
  echo "ERROR: No docker-compose.yml or compose.yml found in $PROJECT_ROOT"
  exit 1
fi

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║         Slate Health — E2E Test Harness                     ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  Project root : $PROJECT_ROOT"
echo "  Frontend URL : $FRONTEND_URL"
echo "  Compose cmd  : $COMPOSE_CMD $COMPOSE_ARGS -p $COMPOSE_PROJECT"
echo "  Playwright   : $RUN_PLAYWRIGHT"
echo ""

# ── Teardown handler ──────────────────────────────────────────────────────────

cleanup() {
  if [ "${KEEP_STACK:-}" = "1" ]; then
    echo ""
    echo "KEEP_STACK=1 — leaving compose stack running."
    echo "Tear down manually: $COMPOSE_CMD $COMPOSE_ARGS -p $COMPOSE_PROJECT down -v"
  else
    echo ""
    echo "Tearing down compose stack..."
    $COMPOSE_CMD $COMPOSE_ARGS -p "$COMPOSE_PROJECT" down -v --remove-orphans 2>/dev/null || true
  fi
  exit "$EXIT_CODE"
}
trap cleanup EXIT

# ── Start stack ───────────────────────────────────────────────────────────────

echo "Starting Docker Compose stack..."
BUILD_FLAG=""
if [ "${SKIP_BUILD:-}" != "1" ]; then
  BUILD_FLAG="--build"
fi

$COMPOSE_CMD $COMPOSE_ARGS -p "$COMPOSE_PROJECT" up -d $BUILD_FLAG

# ── Wait for services ─────────────────────────────────────────────────────────

wait_for_url() {
  local url="$1"
  local label="$2"
  local max_attempts="${3:-30}"
  local attempt=1

  echo -n "  Waiting for $label ($url) "
  while [ $attempt -le $max_attempts ]; do
    if curl -sf -o /dev/null --max-time 2 "$url" 2>/dev/null; then
      echo " ready (${attempt}s)"
      return 0
    fi
    echo -n "."
    sleep 1
    attempt=$((attempt + 1))
  done
  echo " TIMEOUT after ${max_attempts}s"
  echo "ERROR: $label did not become available at $url"
  echo ""
  echo "Docker logs:"
  $COMPOSE_CMD $COMPOSE_ARGS -p "$COMPOSE_PROJECT" logs --tail=50
  EXIT_CODE=1
  exit 1
}

echo ""
echo "Waiting for services to become healthy..."
wait_for_url "$FRONTEND_URL" "Frontend" 60
wait_for_url "$FRONTEND_URL/health" "Backend (via proxy)" 30

echo ""
echo "All services are healthy."
echo ""

# ── Run Vitest E2E ────────────────────────────────────────────────────────────

echo "Running Vitest E2E tests..."
echo ""
cd "$FRONTEND_DIR"

DOCKER_E2E=1 \
FRONTEND_HOST="$FRONTEND_HOST" \
FRONTEND_PORT="$FRONTEND_PORT" \
FRONTEND_URL="$FRONTEND_URL" \
  npx vitest run --config vitest.e2e.config.ts || EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
  echo ""
  echo "Vitest E2E tests FAILED (exit code: $EXIT_CODE)"
fi

# ── Run Playwright (mandatory by default) ─────────────────────────────────────

if [ "$RUN_PLAYWRIGHT" = true ] && [ $EXIT_CODE -eq 0 ]; then
  echo ""
  echo "Running Playwright browser redirect tests..."
  echo ""

  # Ensure Playwright browsers are installed
  npx playwright install --with-deps chromium 2>/dev/null || true

  FRONTEND_URL="$FRONTEND_URL" npx playwright test || EXIT_CODE=$?

  if [ $EXIT_CODE -ne 0 ]; then
    echo ""
    echo "Playwright tests FAILED (exit code: $EXIT_CODE)"
  fi
elif [ "$RUN_PLAYWRIGHT" = false ]; then
  echo ""
  echo "⚠  Playwright browser tests SKIPPED (--no-playwright flag)."
  echo "   Browser redirect verification was not performed."
  echo "   For full acceptance validation, run without --no-playwright."
fi

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
if [ $EXIT_CODE -eq 0 ]; then
  if [ "$RUN_PLAYWRIGHT" = true ]; then
    echo "All E2E tests PASSED (Vitest + Playwright)."
  else
    echo "Vitest E2E tests PASSED (Playwright skipped)."
  fi
else
  echo "E2E tests FAILED (exit code: $EXIT_CODE)"
fi
