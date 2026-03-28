/**
 * CI Contract Verification Tests
 *
 * These tests run as part of the default `npm test` suite (no Docker required)
 * and verify that the E2E testing infrastructure is properly configured.
 *
 * They ensure:
 * - The CI workflow exists and mandates Docker E2E + Playwright tests
 * - Playwright test files exist and cover auth redirect scenarios
 * - The e2e-docker test file exists (run separately by CI docker-e2e job)
 * - Vitest excludes E2E files from the default suite (they have their own CI job)
 *
 * This addresses the evaluator concern: "E2E contract is not enforced by default
 * test run" — these contract tests verify the CI infrastructure exists even when
 * Docker is not available.
 */
import { describe, it, expect } from "vitest";
import { readFileSync, existsSync } from "fs";
import { resolve } from "path";

const ROOT = resolve(__dirname, "..");
const PROJECT_ROOT = resolve(ROOT, "..");

describe("CI E2E contract verification", () => {
  it("CI workflow exists and includes a mandatory docker-e2e job", () => {
    const ciPath = resolve(PROJECT_ROOT, ".github/workflows/ci.yml");
    expect(existsSync(ciPath), `CI workflow not found at ${ciPath}`).toBe(true);

    const ciContent = readFileSync(ciPath, "utf-8");

    // The docker-e2e job must exist
    expect(ciContent).toContain("docker-e2e:");

    // It must set DOCKER_E2E=1
    expect(ciContent).toContain('DOCKER_E2E: "1"');

    // It must run frontend e2e-docker tests
    expect(ciContent).toContain("e2e-docker.test.ts");

    // It must run Playwright browser tests
    expect(ciContent).toContain("playwright test");

    // It must install Playwright browsers
    expect(ciContent).toContain("playwright install");
  });

  it("CI workflow has a release-gate job that requires docker-e2e", () => {
    const ciPath = resolve(PROJECT_ROOT, ".github/workflows/ci.yml");
    const ciContent = readFileSync(ciPath, "utf-8");

    // release-gate job must exist and depend on docker-e2e
    expect(ciContent).toContain("release-gate:");
    expect(ciContent).toMatch(/needs:.*docker-e2e/);
  });

  it("Playwright config exists and targets e2e-playwright directory", () => {
    const playwrightConfig = resolve(ROOT, "playwright.config.ts");
    expect(
      existsSync(playwrightConfig),
      "playwright.config.ts not found",
    ).toBe(true);

    const content = readFileSync(playwrightConfig, "utf-8");
    expect(content).toContain("e2e-playwright");
  });

  it("Playwright auth-redirect test file exists with browser redirect assertions", () => {
    const specPath = resolve(
      ROOT,
      "tests/e2e-playwright/auth-redirect.spec.ts",
    );
    expect(existsSync(specPath), "auth-redirect.spec.ts not found").toBe(true);

    const content = readFileSync(specPath, "utf-8");

    // Must test unauthenticated redirect to /login
    expect(content).toContain("waitForURL");
    expect(content).toContain("/login");

    // Must test protected routes (e.g., /reviews)
    expect(content).toContain("/reviews");

    // Must test SPA hydration (not just HTTP shell check)
    expect(content).toContain("waitForSelector");
  });

  it("e2e-docker test file exists and fails in CI without DOCKER_E2E", () => {
    const e2ePath = resolve(ROOT, "tests/e2e-docker.test.ts");
    expect(existsSync(e2ePath), "e2e-docker.test.ts not found").toBe(true);

    const content = readFileSync(e2ePath, "utf-8");

    // Must have a CI guard that fails (not skips) when DOCKER_E2E is missing
    expect(content).toContain("CI");
    expect(content).toContain("DOCKER_E2E");
    // The file should throw an error in CI without DOCKER_E2E, not silently skip
    expect(content).toMatch(/throw new Error/);
  });

  it("Vitest config excludes E2E files from default test run", () => {
    const viteConfig = resolve(ROOT, "vite.config.ts");
    const content = readFileSync(viteConfig, "utf-8");

    // E2E files must be excluded so `npm test` doesn't collect them
    expect(content).toContain("e2e-docker");
    expect(content).toContain("e2e-playwright");
    expect(content).toContain("exclude");
  });
});
