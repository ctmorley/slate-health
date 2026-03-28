/**
 * E2E infrastructure canary test.
 *
 * This test runs in the default vitest suite (npm test) and verifies that the
 * E2E test infrastructure exists and is correctly wired into CI. If someone
 * accidentally deletes the E2E tests, the Playwright specs, or removes the CI
 * job, this canary test will fail in the regular test run.
 *
 * This addresses the "E2E contract not enforced by default" concern: while the
 * actual E2E tests only run in the docker-e2e CI job (they need a compose
 * stack), this canary ensures the infrastructure can't silently disappear.
 */
import { describe, it, expect } from "vitest";
import { existsSync, readFileSync } from "fs";
import { resolve } from "path";

const ROOT = resolve(__dirname, "..");
const PROJECT_ROOT = resolve(ROOT, "..");

describe("E2E infrastructure canary", () => {
  it("e2e-docker.test.ts exists", () => {
    const path = resolve(ROOT, "tests/e2e-docker.test.ts");
    expect(existsSync(path)).toBe(true);
  });

  it("Playwright auth-redirect spec exists", () => {
    const path = resolve(ROOT, "tests/e2e-playwright/auth-redirect.spec.ts");
    expect(existsSync(path)).toBe(true);
  });

  it("Playwright config exists", () => {
    const path = resolve(ROOT, "playwright.config.ts");
    expect(existsSync(path)).toBe(true);
  });

  it("CI workflow references docker-e2e job with frontend E2E and Playwright steps", () => {
    const ciPath = resolve(PROJECT_ROOT, ".github/workflows/ci.yml");
    expect(existsSync(ciPath)).toBe(true);

    const ci = readFileSync(ciPath, "utf-8");
    // The CI must have the docker-e2e job
    expect(ci).toContain("docker-e2e:");
    // It must run the frontend docker E2E tests
    expect(ci).toContain("e2e-docker.test.ts");
    // It must run Playwright browser redirect tests
    expect(ci).toContain("playwright test");
    // It must set DOCKER_E2E=1
    expect(ci).toContain('DOCKER_E2E: "1"');
  });

  it("CI workflow has a release-gate job that requires docker-e2e and all test jobs", () => {
    const ciPath = resolve(PROJECT_ROOT, ".github/workflows/ci.yml");
    const ci = readFileSync(ciPath, "utf-8");
    // The release-gate job is the required status check for branch protection.
    // It ensures Docker E2E, Playwright, and all test suites must pass.
    expect(ci).toContain("release-gate:");
    expect(ci).toContain("needs: [test, frontend-test, docker-e2e, prod-compose]");
  });

  it("e2e-docker tests are excluded from default vitest run (by vite.config.ts)", () => {
    const configPath = resolve(ROOT, "vite.config.ts");
    const config = readFileSync(configPath, "utf-8");
    // The exclude list must contain e2e-docker to prevent false skips in npm test
    expect(config).toContain("e2e-docker");
  });

  it("pre-push hook script exists for E2E reminder", () => {
    const hookPath = resolve(PROJECT_ROOT, "scripts/pre-push-e2e-check.sh");
    expect(existsSync(hookPath)).toBe(true);
  });

  it("package.json has test:preflight script for mandatory pre-merge validation", () => {
    const pkgPath = resolve(ROOT, "package.json");
    const pkg = JSON.parse(readFileSync(pkgPath, "utf-8"));
    expect(pkg.scripts["test:preflight"]).toBeDefined();
    // Preflight must run both unit tests and E2E
    expect(pkg.scripts["test:preflight"]).toContain("vitest run");
    expect(pkg.scripts["test:preflight"]).toContain("run-e2e.sh");
  });
});
