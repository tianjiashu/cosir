import { app, Menu, shell, type MenuItemConstructorOptions } from "electron";

import type { BrowserNetLogCapture } from "./browser/diagnostics/netlog-capture";
import { appLogDir } from "./paths";
import { appMenuKeys, type MenuKeyItem } from "../shared/app-menu-keys";
import { electronAccelerator } from "../shared/key-names";

/**
 * 应用菜单。不用 Electron 的默认菜单：默认的「视图」菜单里，刷新（Cmd+R）刷新的是整个应用窗口，缩放（Cmd + / -）
 * 缩放的是当前有焦点的网页——焦点在内置浏览器里时，用户以为在刷新网页，实际把应用界面重载了；缩放会悄悄改掉
 * Agent 看到的布局（它的坐标与截图按固定视口算）。菜单快捷键是窗口级的，焦点在哪个网页里都会触发，所以只能在
 * 菜单这里拿掉。
 *
 * 所以「视图」菜单（刷新、缩放、开发者工具）只在开发时保留；编辑菜单必须保留：复制、粘贴、全选等快捷键在 macOS
 * 上靠它才生效，应用的输入框与内置浏览器的网页都要用。
 *
 * 「诊断」菜单打包后也在：内置浏览器出网络问题时，用户在复现那一刻开启原生网络记录（`BrowserNetLogCapture`），
 * 开始与结束都会重装菜单以刷新可用项。
 *
 * 各菜单的条目与快捷键写在 `electron/shared/app-menu-keys.ts`，渲染侧快捷键登记表的保留键也从那里来；
 * 这里只把它装成 Electron 菜单，不另写快捷键。
 */
export function installAppMenu(netLog: BrowserNetLogCapture): void {
  const keys = appMenuKeys(process.platform === "darwin" || process.platform === "win32" ? process.platform : "linux");
  const template: MenuItemConstructorOptions[] = [
    { role: process.platform === "darwin" ? "appMenu" : "fileMenu", submenu: keys.app.map(menuItem) },
    { role: "editMenu", submenu: keys.edit.map(menuItem) },
    ...(app.isPackaged ? [] : [{ role: "viewMenu", submenu: keys.view.map(menuItem) } as const]),
    { role: "windowMenu", submenu: keys.window.map(menuItem) },
    {
      label: "诊断",
      submenu: [
        { label: "开始记录浏览器网络（15 分钟）", enabled: !netLog.active, click: () => netLog.start() },
        { label: "停止记录浏览器网络", enabled: netLog.active, click: () => void netLog.stop() },
        { type: "separator" },
        { label: "打开日志文件夹", click: () => void shell.openPath(appLogDir()) },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function menuItem(item: MenuKeyItem): MenuItemConstructorOptions {
  if ("separator" in item) return { type: "separator" };
  if ("submenu" in item) return { label: item.label, submenu: item.submenu.map(menuItem) };
  return { role: item.role, ...(item.keys ? { accelerator: electronAccelerator(item.keys) } : {}) };
}
