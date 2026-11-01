import { app, BrowserWindow } from "electron";

import { BrowserHost } from "../../../electron/main/browser/browser-host";
import { configureBrowserSession } from "../../../electron/main/browser/browser-session";

/**
 * 只供 `browser-engine.spec.ts` 使用的 Electron 入口：在真实 Electron 里直接驱动 `BrowserHost`，
 * 不经后端与链路。由 `global-setup.mjs` 打包，生产代码不含任何测试钩子。
 */
app.on("window-all-closed", () => undefined);

void app.whenReady().then(() => {
  // 不打扰正在用电脑的人：不显示窗口、不在 Dock 里出现、不抢焦点。视图挂在隐藏窗口上时摆放、缩放与点击照常
  // （实测）；后台标签的点击靠焦点模拟，与窗口是否可见无关。
  app.dock?.hide();
  // 摆放用例需要一个主窗口；内容无关紧要。
  const window = new BrowserWindow({ width: 1000, height: 700, show: false });
  void window.loadURL("about:blank");
  const host = new BrowserHost(() => window);
  configureBrowserSession(host);
  (globalThis as { browserHarness?: unknown }).browserHarness = { host, window };
});
