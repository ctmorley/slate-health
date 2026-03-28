/**
 * E2E test: Docker Compose startup and login redirect verification.
 *
 * This test verifies that:
 * - The frontend is accessible at the configured URL
 * - Unauthenticated access serves the SPA shell (React Router handles redirect)
 * - The SPA shell includes required app markers
 * - The backend health/API endpoints are reachable via proxy
 *
 * CI Integration:
 *   These tests are enforced in CI. The CI pipeline MUST set DOCKER_E2E=1
 *   and start the compose stack before running this suite:
 *
 *     # .github/workflows/e2e.yml (dedicated job — always runs, never skipped)
 *     e2e:
 *       runs-on: ubuntu-latest
 *       steps:
 *         - uses: actions/checkout@v4
 *         - name: Start Docker Compose stack
 *           run: docker compose -p slate-e2e up -d --build --wait
 *         - name: Install frontend deps
 *           working-directory: frontend
 *           run: npm ci
 *         - name: Run E2E tests
 *           working-directory: frontend
 *           env:
 *             DOCKER_E2E: "1"
 *             FRONTEND_PORT: "3000"
 *           run: npx vitest run tests/e2e-docker.test.ts
 *         - name: Tear down
 *           if: always()
 *           run: docker compose -p slate-e2e down
 *
 * Browser redirect testing (Playwright):
 *   Playwright browser tests run by default in the E2E harness (run-e2e.sh)
 *   and in CI. They validate true client-side redirect behavior by hydrating
 *   the SPA and asserting actual browser URL transitions (e.g., /reviews ->
 *   /login when unauthenticated). See tests/e2e-playwright/ for those tests.
 *   To skip Playwright locally: ./scripts/run-e2e.sh --no-playwright
 *
 * Configuration:
 *   DOCKER_E2E=1         - Enable this test suite (skipped in local dev by default)
 *   FRONTEND_PORT=3000   - Port where the frontend is served (default: 3000)
 *   FRONTEND_HOST=localhost - Hostname for the frontend (default: localhost)
 *
 * Example (local):
 *   FRONTEND_PORT=3210 docker compose -p slate-e2e up -d --build
 *   DOCKER_E2E=1 FRONTEND_PORT=3210 npx vitest run tests/e2e-docker.test.ts
 *   docker compose -p slate-e2e down
 */
import { describe, it, expect, beforeAll } from "vitest";

const DOCKER_E2E = process.env.DOCKER_E2E === "1";
const CI = process.env.CI === "true" || process.env.CI === "1";
const FRONTEND_HOST = process.env.FRONTEND_HOST ?? "localhost";
const FRONTEND_PORT = process.env.FRONTEND_PORT ?? "3000";
const FRONTEND_URL =
  process.env.FRONTEND_URL ?? `http://${FRONTEND_HOST}:${FRONTEND_PORT}`;

// NOTE: This file is excluded from the default vitest config (see vite.config.ts
// exclude list). It is run ONLY by the dedicated `docker-e2e` CI job which sets
// DOCKER_E2E=1 and starts the compose stack first. This prevents false failures
// in the regular `npm test` run while ensuring the E2E suite is always executed
// by its own mandatory CI job (see .github/workflows/ci.yml → docker-e2e).
//
// IMPORTANT: If this file is run explicitly in CI (i.e. `npx vitest run tests/e2e-docker.test.ts`)
// without DOCKER_E2E=1, the tests will FAIL (not skip) to prevent silent misconfiguration.

if (CI && !DOCKER_E2E) {
  describe("E2E: Docker Compose frontend", () => {
    it("FAIL: DOCKER_E2E=1 is required when running this file in CI", () => {
      throw new Error(
        "This test file was invoked in CI without DOCKER_E2E=1. " +
          "The docker-e2e CI job must set DOCKER_E2E=1 and start the compose stack. " +
          "If running locally, either set DOCKER_E2E=1 or use `npm test` (which excludes this file).",
      );
    });
  });
}

/**
 * Ownership check: verify the server at FRONTEND_URL is actually the Slate
 * Health SPA and not some unrelated service (e.g. a user's local dev server
 * on the same port). This prevents false positives/negatives from ambient
 * localhost state.
 */
async function verifyHostOwnership(): Promise<{
  ok: boolean;
  reason: string;
}> {
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5_000);
    const res = await fetch(FRONTEND_URL, { signal: controller.signal });
    clearTimeout(timeout);

    if (!res.ok) {
      return {
        ok: false,
        reason: `${FRONTEND_URL} returned HTTP ${res.status} — expected 200. Is the compose stack running?`,
      };
    }

    const html = await res.text();

    // Verify Slate Health SPA markers are present
    if (!html.includes('<div id="root">')) {
      return {
        ok: false,
        reason: `${FRONTEND_URL} responded but HTML is missing <div id="root">. ` +
          `This does not appear to be the Slate Health SPA. ` +
          `Another service may be running on port ${FRONTEND_PORT}.`,
      };
    }
    if (!html.includes("Slate Health")) {
      return {
        ok: false,
        reason: `${FRONTEND_URL} responded with a root div but is missing "Slate Health" branding. ` +
          `This does not appear to be the correct application. ` +
          `Another process may be using port ${FRONTEND_PORT}. ` +
          `Check with: lsof -ti :${FRONTEND_PORT} (macOS/Linux) or use a different port: ` +
          `FRONTEND_PORT=3210 DOCKER_E2E=1 npx vitest run tests/e2e-docker.test.ts`,
      };
    }

    return { ok: true, reason: "" };
  } catch (err: unknown) {
    const message =
      err instanceof Error ? err.message : String(err);
    if (message.includes("abort") || message.includes("ABORT")) {
      return {
        ok: false,
        reason: `Connection to ${FRONTEND_URL} timed out after 5s. Is docker-compose up?`,
      };
    }
    return {
      ok: false,
      reason: `Cannot reach ${FRONTEND_URL}: ${message}. Is docker-compose up?`,
    };
  }
}

describe.skipIf(!DOCKER_E2E)("E2E: Docker Compose frontend", () => {
  // Run ownership check before all tests — fail fast if the target isn't ours
  beforeAll(async () => {
    const ownership = await verifyHostOwnership();
    if (!ownership.ok) {
      throw new Error(
        `[E2E precondition failed] ${ownership.reason}\n\n` +
          `To run these tests:\n` +
          `  1. Start the compose stack: docker compose -p slate-e2e up -d --build --wait\n` +
          `  2. Run: DOCKER_E2E=1 FRONTEND_PORT=${FRONTEND_PORT} npx vitest run tests/e2e-docker.test.ts\n` +
          `  3. Tear down: docker compose -p slate-e2e down`,
      );
    }
  });

  it("frontend is accessible and serves the SPA shell with app markers", async () => {
    const res = await fetch(FRONTEND_URL);
    expect(res.status).toBe(200);
    const html = await res.text();
    // Verify the SPA shell has the root mount point
    expect(html).toContain('<div id="root">');
    // Verify app branding is present (in the HTML title or meta)
    expect(html).toContain("Slate Health");
    // Verify the JS bundle is referenced (Vite injects script tags)
    expect(html).toMatch(/<script\s/);
  });

  it("SPA fallback serves index.html for /login route", async () => {
    const res = await fetch(`${FRONTEND_URL}/login`, { redirect: "follow" });
    expect(res.status).toBe(200);
    const html = await res.text();
    // nginx SPA fallback should serve the same index.html for client-side routes
    expect(html).toContain('<div id="root">');
    expect(html).toContain("Slate Health");
  });

  it("unauthenticated access to protected route serves SPA shell for client-side redirect", async () => {
    // Fetching a protected route like /reviews should still serve the SPA
    // shell. React Router will handle the redirect to /login on the client.
    const res = await fetch(`${FRONTEND_URL}/reviews`, { redirect: "follow" });
    expect(res.status).toBe(200);
    const html = await res.text();
    expect(html).toContain('<div id="root">');
    // The SPA bundle is served so React Router can redirect -- verify the
    // bundle script tag is included, meaning the client-side redirect logic
    // (ProtectedRoute -> Navigate to /login) will execute after hydration.
    expect(html).toMatch(/<script\s/);

    // Verify the same SPA shell is served for the root path (the nginx
    // try_files fallback is working for arbitrary client-side routes).
    const rootRes = await fetch(FRONTEND_URL);
    const rootHtml = await rootRes.text();
    // Both responses should serve the same SPA -- the HTML shell content
    // must be identical (same entry point, same React Router handling).
    expect(html).toEqual(rootHtml);
  });

  it("unauthenticated API call to backend returns 401", async () => {
    const res = await fetch(`${FRONTEND_URL}/api/v1/dashboard/summary`);
    // The nginx proxy forwards /api/ to the backend, which requires auth
    expect(res.status).toBe(401);
  });

  it("backend health endpoint is accessible via nginx proxy", async () => {
    const res = await fetch(`${FRONTEND_URL}/health`);
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body).toEqual({ status: "healthy" });
  });

  it("unauthenticated API call to auth/me returns 401", async () => {
    const res = await fetch(`${FRONTEND_URL}/api/v1/auth/me`);
    expect(res.status).toBe(401);
  });

  it("login callback URL with tokens still serves the SPA shell", async () => {
    // Simulates what happens after IdP redirects back with tokens in query params.
    // The SPA shell must be served so React can process the callback client-side.
    const res = await fetch(
      `${FRONTEND_URL}/login?access_token=test-token&refresh_token=test-refresh`,
      { redirect: "follow" },
    );
    expect(res.status).toBe(200);
    const html = await res.text();
    expect(html).toContain('<div id="root">');
    // Tokens should NOT be reflected in the served HTML (they're query params
    // only -- the SPA JS processes and scrubs them).
    expect(html).not.toContain("test-token");
  });
});
