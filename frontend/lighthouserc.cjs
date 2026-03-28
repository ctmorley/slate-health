/**
 * Lighthouse CI configuration for Slate Health frontend.
 *
 * Enforces a minimum performance score of 80 as a release gate.
 * Run with: npx lhci autorun
 *
 * In CI, the frontend should be built and served (e.g. via `vite preview`
 * or the production nginx container) before running Lighthouse.
 */
module.exports = {
  ci: {
    collect: {
      // In CI, override with --collect.url=http://localhost:80
      startServerCommand: 'npx vite preview --port 4173',
      startServerReadyPattern: 'Local',
      startServerReadyTimeout: 15000,
      url: ['http://localhost:4173/'],
      numberOfRuns: 3,
      settings: {
        // Use mobile emulation (Lighthouse default) for conservative scores
        preset: 'desktop',
      },
    },
    assert: {
      assertions: {
        // RELEASE GATE: performance score must be >= 80
        'categories:performance': ['error', { minScore: 0.8 }],
        // Best practices should also be reasonable
        'categories:best-practices': ['warn', { minScore: 0.8 }],
        // Accessibility is important for healthcare
        'categories:accessibility': ['warn', { minScore: 0.7 }],
        // SEO not critical for internal dashboard
        'categories:seo': 'off',
      },
    },
    upload: {
      // Store results as temporary public links (no server needed)
      target: 'temporary-public-storage',
    },
  },
};
