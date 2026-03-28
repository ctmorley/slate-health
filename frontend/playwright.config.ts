import { defineConfig } from "@playwright/test";

/**
 * Playwright configuration for browser-based E2E tests.
 *
 * These tests validate actual client-side routing behavior (e.g., React Router
 * redirects) by running in a real browser, unlike the fetch-based e2e-docker
 * tests that only verify the HTML shell.
 *
 * Prerequisites:
 *   - Docker Compose stack running (`docker compose up -d --build`)
 *   - Playwright browsers installed (`npx playwright install chromium`)
 *
 * Run locally:
 *   FRONTEND_URL=http://localhost:3000 npx playwright test
 *
 * In CI, the docker-e2e job handles this automatically.
 */
export default defineConfig({
  testDir: "./tests/e2e-playwright",
  timeout: 30_000,
  retries: 1,
  use: {
    baseURL: process.env.FRONTEND_URL ?? "http://localhost:3000",
    headless: true,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { browserName: "chromium" },
    },
  ],
  /* Do not start a dev server — the Docker Compose stack provides the frontend */
  webServer: undefined,
});
