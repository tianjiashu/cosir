import { app, BrowserWindow } from "electron";

import { HOST_CHANNEL } from "../shared/host-contract";
import { handleAppProtocol, registerAppScheme } from "./app-protocol";
import { BackendSupervisor } from "./backend/supervisor";
import { logDesktopEvent } from "./desktop-log";
import { confirmExit } from "./exit-confirmation";
import { registerHostIpc } from "./ipc";
import { isolateUserDataInExplicitDataRoot } from "./paths";
import { installTray } from "./tray";
import {
  createMainWindow,
  followNativeThemeInTitleBar,
  markQuitting,
  restrictSessionPermissions,
  showMainWindow,
} from "./window";

/**
 * Electron 宿主入口：装配单实例、协议、IPC、主窗口、托盘、后端 supervisor 与退出流程。
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
  const supervisor = new BackendSupervisor();
  supervisor.onChange((snapshot) => {
    for (const window of BrowserWindow.getAllWindows()) window.webContents.send(HOST_CHANNEL.backendChanged, snapshot);
  });

  app.on("second-instance", () => showMainWindow());
  // macOS 点击 Dock 图标：窗口被关闭（隐藏）后由这里恢复。
  app.on("activate", () => showMainWindow());

  void app.whenReady().then(() => {
    handleAppProtocol();
    restrictSessionPermissions();
    registerHostIpc(supervisor);
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
    void supervisor.stop().finally(() => {
      backendStopped = true;
      logDesktopEvent("INFO", "app", "application_exited");
      app.exit(0);
    });
  });
}
