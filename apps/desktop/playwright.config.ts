import { defineConfig, devices, type PlaywrightTestConfig } from "@playwright/test";

import { E2E_WORKERS, OUTPUT_DIR, WEB_ORIGIN, e2eStack } from "./tests/e2e/e2e-env";

const npm = process.platform === "win32" ? "npm.cmd" : "npm";

export default defineConfig({
  testDir: "./tests/e2e",
  // 视觉回归另有配置（playwright.visual.config.ts），日常运行不比对截图。
  testIgnore: "**/*.visual.spec.ts",
  outputDir: OUTPUT_DIR,
  // 每个 worker 独占一套服务（见 tests/e2e/e2e-env.ts），文件之间并行；同一文件内仍串行，beforeAll 语义不变。
  workers: E2E_WORKERS,
  timeout: 30_000,
  use: {
    // worker 里加载配置时 WEB_ORIGIN 已是本 worker 的 Vite。
    baseURL: WEB_ORIGIN,
    trace: "retain-on-failure",
  },
  // 全部服务都归本次运行所有：端口已被占用时直接失败（不复用别的运行的实例），结束时由 Playwright 清理。
  // 多个会话同时运行改用不同的 COSIR_E2E_SLOT。
  webServer: Array.from({ length: E2E_WORKERS }, (_, index) => e2eStack(index)).flatMap((stack): NonNullable<PlaywrightTestConfig["webServer"]> => [
    {
      command: `node tests/e2e/test-server.mjs`,
      url: `${stack.apiOrigin}/health`,
      env: { COSIR_E2E_API_PORT: String(stack.apiPort) },
      reuseExistingServer: false,
    },
    {
      // --strictPort：端口被占时 Vite 报错退出，而不是悄悄换端口。vite.e2e.config.ts：不监听文件、不热更新，
      // 运行中别处保存代码不会让测试页重载。每个 Vite 按端口各有一份依赖预构建缓存（dev-server-cache-dir-plugin）。
      command: `${npm} run dev -- --config vite.e2e.config.ts --host 127.0.0.1 --port ${stack.webPort} --strictPort`,
      url: stack.webOrigin,
      env: { VITE_BACKEND_URL: stack.apiOrigin },
      reuseExistingServer: false,
    },
  ]),
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
