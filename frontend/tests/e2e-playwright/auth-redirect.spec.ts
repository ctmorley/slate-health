/**
 * Playwright E2E test: true browser redirect validation.
 *
 * This test validates actual client-side routing behavior by loading the SPA
 * in a real browser, waiting for hydration, and asserting URL transitions.
 *
 * Unlike the fetch-based e2e-docker tests (which only verify the HTML shell),
 * these tests confirm that React Router's ProtectedRoute component correctly
 * redirects unauthenticated users to /login in the browser.
 *
 * Prerequisites:
 *   - Docker Compose stack is running (`docker compose up -d --build`)
 *   - Playwright is installed (`npx playwright install chromium`)
 *
 * Run:
 *   FRONTEND_URL=http://localhost:3000 npx playwright test tests/e2e-playwright/
 *
 * CI integration:
 *   # .github/workflows/e2e.yml (add after vitest e2e step)
 *   - name: Install Playwright
 *     run: npx playwright install --with-deps chromium
 *   - name: Run Playwright redirect tests
 *     working-directory: frontend
 *     env:
 *       FRONTEND_URL: http://localhost:3000
 *     run: npx playwright test tests/e2e-playwright/
 */
import { test, expect } from "@playwright/test";

const FRONTEND_URL = process.env.FRONTEND_URL ?? "http://localhost:3000";

test.describe("Authentication redirect (browser)", () => {
  test("unauthenticated visit to / redirects to /login", async ({ page }) => {
    await page.goto(FRONTEND_URL);
    // Wait for React to hydrate and ProtectedRoute to redirect
    await page.waitForURL("**/login", { timeout: 10_000 });
    expect(page.url()).toContain("/login");
  });

  test("unauthenticated visit to /reviews redirects to /login", async ({ page }) => {
    await page.goto(`${FRONTEND_URL}/reviews`);
    await page.waitForURL("**/login", { timeout: 10_000 });
    expect(page.url()).toContain("/login");
  });

  test("unauthenticated visit to /agents/eligibility redirects to /login", async ({ page }) => {
    await page.goto(`${FRONTEND_URL}/agents/eligibility`);
    await page.waitForURL("**/login", { timeout: 10_000 });
    expect(page.url()).toContain("/login");
  });

  test("/login page renders SSO buttons after hydration", async ({ page }) => {
    await page.goto(`${FRONTEND_URL}/login`);
    // Wait for the SPA to hydrate and render the login form
    await page.waitForSelector("[data-testid='sso-provider-list']", { timeout: 10_000 });
    // At least one SSO button should be visible
    const providerList = page.locator("[data-testid='sso-provider-list']");
    await expect(providerList).toBeVisible();
    // The page should have the Slate Health branding
    await expect(page.locator("text=Slate Health")).toBeVisible();
  });

  test("login page scrubs tokens from URL on callback", async ({ page }) => {
    // Simulate an IdP redirect-back with tokens in query params
    await page.goto(`${FRONTEND_URL}/login?access_token=fake-tok&refresh_token=fake-ref`);
    // After hydration, the URL should have the tokens scrubbed
    await page.waitForFunction(
      () => !window.location.search.includes("access_token"),
      null,
      { timeout: 10_000 },
    );
    expect(page.url()).not.toContain("access_token");
    expect(page.url()).not.toContain("refresh_token");
  });
});
