import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Separate from vite.config.ts to avoid the dual vite-types clash between the
// app's Vite and the one Vitest bundles. Unit + component specs only — Playwright
// owns e2e/snapshot specs under tests/e2e.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    coverage: {
      provider: "v8",
      reporter: ["text", "html"],
      // Gate the foundation LOGIC the pages depend on. Excluded: type-only
      // modules, trivial wiring, the page stubs (S3/S4/S5 own + cover those),
      // and the dev-only harness routes.
      include: ["src/lib/**", "src/hooks/**", "src/components/shared/**"],
      exclude: [
        "src/lib/types.ts", // type declarations only
        "src/lib/queryClient.ts", // trivial QueryClient construction
        "src/**/index.ts", // barrels
        "src/**/*.test.{ts,tsx}",
      ],
      // A FLOOR (below current ~85/77/79/88) so the foundation + its tests are
      // gated and S3/S4/S5 inherit it; raise as coverage grows, never lower to pass.
      thresholds: {
        statements: 80,
        branches: 72,
        functions: 75,
        lines: 80,
      },
    },
  },
});
