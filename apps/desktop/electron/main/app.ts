import { randomBytes } from "node:crypto";

import { app, BrowserWindow } from "electron";

import { HOST_CHANNEL } from "../shared/host-contract";
import { handleAppProtocol, registerAppScheme } from "./app-protocol";
import { BackendSupervisor } from "./backend/supervisor";
import { BrowserHost } from "./browser/browser-host";
import { configureBrowserSession } from "./browser/browser-session";
import { registerBrowserMethods } from "./browser/methods";
import { logDesktopEvent } from "./desktop-log";
import { confirmExit } from "./exit-confirmation";
import { HostLinkClient } from "./host-link/client";
import { HostMethodRegistry } from "./host-link/methods";
import { registerHostIpc } from "./ipc";
import { isolateUserDataInExplicitDataRoot } from "./paths";
import { installTray } from "./tray";
import {
  createMainWindow,
  followNativeThemeInTitleBar,
  getMainWindow,
  markQuitting,
  restrictSessionPermissions,
  showMainWindow,
} from "./window";

/**
 * Electron 宿主入口：装配单实例、协议、IPC、主窗口、托盘、后端 supervisor、宿主工具链路（含内置浏览器）与退出流程。
 * 宿主只管进程、窗口与文件授权，不承载业务逻辑。
 *
 * 退出流程只有一条：托盘「退出并停止运行」、Cmd+Q、应用菜单与 Dock 退出都会触发 `before-quit`
 * → 退出确认（有运行中的终端会话时提醒）→ 关闭窗口 → `will-quit` 停止后端进程树并等它退出 → 结束进程。
 * 关闭窗口不在其中：它只隐藏到托盘。
 */
app.setName("Cosir");
isolateUserDataInExplicitDataRoot();

// 第二个实例不装配任何东西，直接结束；已在运行的实例收到 second-instance 后显示主窗口。
if (!app.requestSingleInstanceLock()) {
  app.exit(0);
} else {
  bootstrap();
}

function bootstrap(): void {
  registerAppScheme();
  // 宿主工具链路的认证 token：同一个值注入后端、由链路客户端在首帧出示。
  const hostToken = randomBytes(32).toString("hex");
  const supervisor = new BackendSupervisor(hostToken);
  // 宿主能力在这里把方法注册进链路；链路只按方法名分发。
  const hostMethods = new HostMethodRegistry();
  const browser = new BrowserHost(getMainWindow);
  registerBrowserMethods(hostMethods, browser);
  const hostLink = new HostLinkClient({
    token: hostToken,
    host: { platform: process.platform, pid: process.pid, version: app.getVersion() },
    methods: hostMethods,
  });
  browser.onChange((tabs) => {
    for (const window of BrowserWindow.getAllWindows()) window.webContents.send(HOST_CHANNEL.browserTabsChanged, tabs);
  });
  supervisor.onChange((snapshot) => {
    for (const window of BrowserWindow.getAllWindows()) window.webContents.send(HOST_CHANNEL.backendChanged, snapshot);
    hostLink.follow(snapshot);
  });

  app.on("second-instance", () => showMainWindow());
  // macOS 点击 Dock 图标：窗口被关闭（隐藏）后由这里恢复。
  app.on("activate", () => showMainWindow());

  void app.whenReady().then(() => {
    handleAppProtocol();
    restrictSessionPermissions();
    configureBrowserSession();
    registerHostIpc(supervisor, browser);
    followNativeThemeInTitleBar();
    // 窗口先出现，后端在后台启动；前端经启动门禁呈现 starting / ready / failed。
    createMainWindow();
    installTray({ show: showMainWindow, quit: () => app.quit() });
    void supervisor.start();
  });

  let exitState: "idle" | "confirming" | "confirmed" = "idle";
  app.on("before-quit", (event) => {
    if (exitState === "confirmed") return;
    event.preventDefault();
    if (exitState === "confirming") return;
    exitState = "confirming";
    void confirmExit(supervisor.readyBaseUrl()).then((confirmed) => {
      if (!confirmed) {
        exitState = "idle";
        return;
      }
      exitState = "confirmed";
      markQuitting();
      app.quit();
    });
  });

  let backendStopped = false;
  app.on("will-quit", (event) => {
    if (backendStopped) return;
    event.preventDefault();
    browser.closeAll();
    void supervisor.stop().finally(() => {
      backendStopped = true;
      logDesktopEvent("INFO", "app", "application_exited");
      app.exit(0);
    });
  });
}
