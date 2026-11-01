import { defineConfig, devices } from "@playwright/test";

import { API_ORIGIN, API_PORT, OUTPUT_DIR, WEB_ORIGIN, WEB_PORT } from "./tests/e2e/e2e-env";

const npm = process.platform === "win32" ? "npm.cmd" : "npm";

export default defineConfig({
  testDir: "./tests/e2e",
  outputDir: OUTPUT_DIR,
  // The local test service is intentionally one process with in-memory state.
  // Keep all browser tests in one worker so fixed test fixtures cannot race
  // while the service is being evolved toward per-worker isolation.
  workers: 1,
  timeout: 30_000,
  use: {
    baseURL: WEB_ORIGIN,
    trace: "retain-on-failure",
  },
  // 两个服务都归本次运行所有：端口已被占用时直接失败（不复用别的运行的实例），结束时由 Playwright 清理。
  // 并行运行改用不同的 COSIR_E2E_SLOT（见 tests/e2e/e2e-env.ts）。
  webServer: [
    {
      command: `node tests/e2e/test-server.mjs`,
      url: `${API_ORIGIN}/health`,
      env: { COSIR_E2E_API_PORT: String(API_PORT) },
      reuseExistingServer: false,
    },
    {
      // --strictPort：端口被占时 Vite 报错退出，而不是悄悄换端口。
      command: `${npm} run dev -- --host 127.0.0.1 --port ${WEB_PORT} --strictPort`,
      url: WEB_ORIGIN,
      env: { VITE_BACKEND_URL: API_ORIGIN },
      reuseExistingServer: false,
    },
  ],
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        launchOptions: process.env.COSIR_PLAYWRIGHT_EXECUTABLE
          ? { executablePath: process.env.COSIR_PLAYWRIGHT_EXECUTABLE }
          : undefined,
      },
    },
  ],
});
