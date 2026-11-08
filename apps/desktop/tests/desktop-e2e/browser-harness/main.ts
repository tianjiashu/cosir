import { app, BrowserWindow } from "electron";

import { BrowserHost } from "../../../electron/main/browser/browser-host";
import { configureBrowserSession, configureBrowserUserAgent } from "../../../electron/main/browser/browser-partition";
import { registerBrowserMethods } from "../../../electron/main/browser/browser-methods";
import { HostMethodRegistry } from "../../../electron/main/host-link/method-registry";

/**
 * 只供 `browser-engine.spec.ts` 使用的 Electron 入口：在真实 Electron 里直接驱动 `BrowserHost`，或经与生产相同的
 * 方法注册表（`invoke`：按导出的 schema 校验蛇形参数、按 `wire.ts` 出线上形状）调用，不经后端与 WebSocket。
 * 由 `global-setup.mjs` 打包，生产代码不含任何测试钩子。
 */
configureBrowserUserAgent();
app.on("window-all-closed", () => undefined);

void app.whenReady().then(() => {
  // 不打扰正在用电脑的人：不显示窗口、不在 Dock 里出现、不抢焦点。视图挂在隐藏窗口上时摆放、缩放与点击照常
  // （实测）；后台标签的点击靠焦点模拟，与窗口是否可见无关。
  app.dock?.hide();
  // 摆放用例需要一个主窗口；内容无关紧要。
  const window = new BrowserWindow({ width: 1000, height: 700, show: false });
  void window.loadURL("about:blank");
  // 与生产同一契约：窗口销毁后取到 null（`getMainWindow`），退出前先收掉全部标签（`app.ts` 的 will-quit）。
  const host = new BrowserHost(() => (window.isDestroyed() ? null : window));
  app.on("will-quit", () => host.closeAll());
  configureBrowserSession(host);
  const methods = new HostMethodRegistry();
  registerBrowserMethods(methods, host);
  const invoke = (method: string, params: unknown) => methods.get(method)!(params, { signal: new AbortController().signal, meta: {} });
  (globalThis as { browserHarness?: unknown }).browserHarness = { host, window, invoke };
});
