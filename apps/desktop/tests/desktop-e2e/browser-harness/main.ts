import { app, BrowserWindow } from "electron";

import { BrowserHost } from "../../../electron/main/browser/browser-host";
import { configureBrowserSession } from "../../../electron/main/browser/browser-session";

/**
 * 只供 `browser-engine.spec.ts` 使用的 Electron 入口：在真实 Electron 里直接驱动 `BrowserHost`，
 * 不经后端与链路。由 `global-setup.mjs` 打包，生产代码不含任何测试钩子。
 */
app.on("window-all-closed", () => undefined);

void app.whenReady().then(() => {
  configureBrowserSession();
  // 摆放用例需要一个主窗口；内容无关紧要。
  const window = new BrowserWindow({ width: 1000, height: 700, show: true });
  void window.loadURL("about:blank");
  (globalThis as { browserHarness?: unknown }).browserHarness = { host: new BrowserHost(() => window), window };
});
