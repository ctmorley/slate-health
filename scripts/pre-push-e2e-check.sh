#!/usr/bin/env bash
#
# Git pre-push hook for Slate Health.
#
# Warns developers that Docker E2E tests are mandatory for merges to main.
# If Docker is available and the compose stack is running, it will offer to
# run the E2E suite. Otherwise, it prints a reminder.
#
# Installation:
#   ln -sf ../../scripts/pre-push-e2e-check.sh .git/hooks/pre-push
#
# To bypass in an emergency:
#   git push --no-verify
#
set -euo pipefail

FRONTEND_DIR="$(cd "$(dirname "$0")/../../frontend" 2>/dev/null && pwd || echo "")"

# Only trigger on pushes that include main-targeting branches
PUSHING_TO_MAIN=false
while read local_ref local_sha remote_ref remote_sha; do
  if echo "$remote_ref" | grep -qE "refs/heads/main$"; then
    PUSHING_TO_MAIN=true
  fi
done

if [ "$PUSHING_TO_MAIN" = false ]; then
  exit 0
fi

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Slate Health — Pre-Push E2E Reminder                       ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  You are pushing to main. The following tests are MANDATORY"
echo "  for merge and are enforced by the CI release-gate job:"
echo ""
echo "    1. Unit tests:     cd frontend && npm test"
echo "    2. Docker E2E:     cd frontend && npm run test:e2e:full"
echo "       (starts compose stack, runs Vitest E2E + Playwright)"
echo "    3. Backend tests:  cd backend && pytest tests/ -v"
echo ""
echo "  If you have not run the Docker E2E suite locally, the CI"
echo "  pipeline will catch failures, but local verification is"
echo "  strongly recommended to avoid failed merges."
echo ""

# Check if Docker is available
if command -v docker &>/dev/null; then
  # Check if compose stack is running
  if docker compose ps 2>/dev/null | grep -q "slate"; then
    echo "  Docker compose stack detected. To run E2E now:"
    echo "    cd frontend && npm run test:e2e:full"
    echo ""
  else
    echo "  Docker is available but compose stack is not running."
    echo "  To run full E2E validation:"
    echo "    cd frontend && npm run test:e2e:full"
    echo ""
  fi
else
  echo "  ⚠  Docker not found. E2E tests will only run in CI."
  echo "     Install Docker Desktop to run locally."
  echo ""
fi

# Allow the push to proceed (this is advisory, not blocking)
exit 0
