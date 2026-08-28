import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    css: true,
    coverage: {
      provider: "v8",
      reporter: ["text", "html", "lcov"],
      exclude: ["src/main.tsx"],
      thresholds: {
        // Baseline for the current component suite; raise as more UI modules
        // receive behavioral coverage.
        lines: 80,
        functions: 90,
        branches: 55,
        statements: 75,
      },
    },
  },
});
