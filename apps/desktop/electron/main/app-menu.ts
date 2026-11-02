import { app, Menu, type MenuItemConstructorOptions } from "electron";

/**
 * 应用菜单。不用 Electron 的默认菜单：默认的「视图」菜单里，刷新（Cmd+R）刷新的是整个应用窗口，缩放（Cmd + / -）
 * 缩放的是当前有焦点的网页——焦点在内置浏览器里时，用户以为在刷新网页，实际把应用界面重载了；缩放会悄悄改掉
 * Agent 看到的布局（它的坐标与截图按固定视口算）。菜单快捷键是窗口级的，焦点在哪个网页里都会触发，所以只能在
 * 菜单这里拿掉。
 *
 * 所以「视图」菜单（刷新、缩放、开发者工具）只在开发时保留；编辑菜单必须保留：复制、粘贴、全选等快捷键在 macOS
 * 上靠它才生效，应用的输入框与内置浏览器的网页都要用。
 */
export function installAppMenu(): void {
  const template: MenuItemConstructorOptions[] = [
    ...(process.platform === "darwin" ? [{ role: "appMenu" } as const] : [{ role: "fileMenu" } as const]),
    { role: "editMenu" },
    ...(app.isPackaged ? [] : [{ role: "viewMenu" } as const]),
    { role: "windowMenu" },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}
