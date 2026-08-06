/**
 * Vitest configuration — the first automated tests this frontend has had.
 *
 * Kept SEPARATE from vite.config.ts on purpose. That file carries the dev
 * server's proxy allowlist and an `fs.allow` list with absolute machine paths;
 * a test run needs none of it, and inheriting it means a proxy misconfiguration
 * can break the test suite for reasons that have nothing to do with the tests.
 * The aliases are duplicated here — a small, stable duplication in exchange for
 * the two concerns being independent.
 */
import react from "@vitejs/plugin-react";
import path from "path";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
      "@shared": path.resolve(__dirname, "src/shared"),
      "@modules": path.resolve(__dirname, "src/modules"),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    // Only our own tests. Without this, a stray .spec.js inside node_modules
    // gets collected and fails for reasons no one can act on.
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    css: false,
    coverage: {
      provider: "v8",
      reportsDirectory: "./coverage",
      // No global threshold yet. Coverage on a codebase whose tests start
      // today would either be set so low it means nothing, or fail every
      // build. Add one once the suite covers something worth defending.
      reporter: ["text-summary", "html"],
      exclude: ["src/test/**", "**/*.d.ts", "src/main.tsx"],
    },
  },
});
