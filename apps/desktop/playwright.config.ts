import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  globalSetup: "./tests/e2e/global-setup.mjs",
  // The local test service is intentionally one process with in-memory state.
  // Keep all browser tests in one worker so fixed test fixtures cannot race
  // while the service is being evolved toward per-worker isolation.
  workers: 1,
  timeout: 30_000,
  use: {
    baseURL: "http://127.0.0.1:4173",
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command: "npm.cmd run dev -- --host 127.0.0.1 --port 4173",
      url: "http://127.0.0.1:4173",
      // Reuse a manually started Vite instance when present; Playwright still
      // starts Vite automatically when this URL is not already available.
      reuseExistingServer: true,
    },
  ],
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
