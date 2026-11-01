import { Menu, nativeImage, Tray } from "electron";

import { iconPath } from "./app-icon";
import { logDesktopEvent } from "./desktop-log";

/** 托盘对象必须一直被引用，否则被垃圾回收后图标会从系统托盘消失。 */
let tray: Tray | null = null;

/**
 * 常驻托盘：左键显示主窗口；菜单提供「显示 Cosir」与「退出并停止运行」。
 *
 * 关闭主窗口只是隐藏，托盘是真正退出的入口之一（另有 Cmd+Q 与应用菜单），退出会经过退出确认并停止后端。
 * 菜单名称明确说明退出会停止运行，避免用户以为只是关掉窗口。
 */
export function installTray({ show, quit }: { show: () => void; quit: () => void }): void {
  const iconName = process.platform === "darwin" ? "tray-macos.png" : "tray-windows.png";
  // 同目录的 `@2x` 由 nativeImage 按屏幕倍率自动选用。
  const icon = nativeImage.createFromPath(iconPath(iconName));
  // macOS 菜单栏图标用模板图，跟随系统明暗自动着色。
  if (process.platform === "darwin") icon.setTemplateImage(true);
  tray = new Tray(icon);
  tray.setToolTip("Cosir");
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: "显示 Cosir", click: show },
    { type: "separator" },
    {
      label: "退出并停止运行",
      click: () => {
        logDesktopEvent("INFO", "tray", "application_exit_requested_from_tray");
        quit();
      },
    },
  ]));
  // Windows 左键单击不会弹出菜单，直接显示窗口；macOS 单击打开菜单，「显示 Cosir」始终在菜单里。
  tray.on("click", show);
}
