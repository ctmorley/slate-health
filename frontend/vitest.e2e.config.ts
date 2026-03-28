/// <reference types="vitest" />
import { defineConfig } from "vite";

/**
 * Dedicated Vitest configuration for E2E (Docker) tests.
 *
 * These tests are excluded from the default vitest config (vite.config.ts) so
 * they don't run during `npm test`. This config includes ONLY the e2e-docker
 * test files and is referenced by the `test:e2e` npm script.
 */
export default defineConfig({
  test: {
    globals: true,
    include: ["tests/e2e-docker*.test.ts", "tests/e2e-canary*.test.ts"],
    exclude: ["**/node_modules/**", "**/dist/**"],
    testTimeout: 30_000,
  },
});
