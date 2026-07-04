// Minimal vitest config for the live handshake probe (Task 1).
// Only the probe file is included; no jsdom environment overrides.
// Used by scripts/collab_handshake_probe.py — NOT the normal `pnpm test` run.
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.probe.test.ts"],
  },
});
