import { defineConfig } from "@playwright/test";

/**
 * 桌面 E2E：用 Playwright 的 Electron 驱动真实宿主（构建产物经 app:// 加载、真实后端子进程）。
 * 与浏览器 E2E（playwright.config.ts，Vite + 内存测试服务）分开：这里覆盖宿主的进程与窗口行为，
 * 不覆盖业务界面细节。运行前须先 `npm run build`（`test:e2e:desktop` 已串好）。
 */
export default defineConfig({
  testDir: "./tests/desktop-e2e",
  globalSetup: "./tests/desktop-e2e/global-setup.mjs",
  workers: 1,
  timeout: 180_000,
  use: { trace: "retain-on-failure" },
});
