import path from "node:path";
import { BrowserWindow, nativeTheme, session, shell } from "electron";

import { APP_ORIGIN } from "./app-protocol";
import { logDesktopEvent } from "./desktop-log";

const rendererUrl = process.env.COSIR_RENDERER_URL ?? `${APP_ORIGIN}/`;
const rendererOrigin = new URL(rendererUrl).origin;
/** 页面需要的权限：终端粘贴读剪贴板、全屏画布。其余（摄像头、通知、地理位置……）一律拒绝。 */
const ALLOWED_PERMISSIONS = new Set(["clipboard-read", "clipboard-sanitized-write", "fullscreen"]);
const WINDOWS_TITLEBAR_HEIGHT = 40;

let mainWindow: BrowserWindow | null = null;
let quitting = false;

/**
 * 主窗口：创建、加载、关闭即隐藏到托盘、显示与安全约束。
 *
 * 窗口先显示，后端在后台启动；前端经启动门禁呈现后端状态，不假定页面出现时后端已可用。
 * 关闭窗口只隐藏，宿主、后端与正在运行的 Agent 继续工作；只有走退出流程（`markQuitting` 之后）才真正关闭。
 *
 * 标题栏：macOS 隐藏标题栏、保留原生红绿灯；Windows 隐藏标题栏、系统窗口按钮叠在右上角
 * （`titleBarOverlay`，按钮颜色随原生外观切换）。页面让位的宽度由 `lib/platform.ts` 按同一规则推断，
 * 改这里必须同步那里。开发期由 `COSIR_RENDERER_URL` 指向 Vite；否则加载 `app://` 下的构建产物。
 */
export function createMainWindow(): BrowserWindow {
  const window = new BrowserWindow({
    title: "Cosir",
    width: 1440,
    height: 900,
    minWidth: 960,
    minHeight: 640,
    show: true,
    // 只有 macOS（原生红绿灯）与 Windows（titleBarOverlay 系统按钮）隐藏标题栏；其余平台没有替代的窗口按钮，
    // 保留原生标题栏（与 lib/platform.ts 的 native 形态一致）。
    ...(process.platform === "darwin" ? { titleBarStyle: "hidden", trafficLightPosition: { x: 20, y: 22 } } : {}),
    ...(process.platform === "win32" ? { titleBarStyle: "hidden", titleBarOverlay: titleBarOverlay() } : {}),
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
    },
  });
  restrictNavigation(window);
  logRendererFailures(window);
  window.on("close", (event) => {
    if (quitting) return;
    event.preventDefault();
    window.hide();
    logDesktopEvent("INFO", "main_window", "main_window_hidden_to_tray");
  });
  window.on("closed", () => {
    if (mainWindow === window) mainWindow = null;
  });
  void window.loadURL(rendererUrl);
  mainWindow = window;
  return window;
}

/** 托盘、Dock 重开与单实例唤醒共用：恢复并聚焦主窗口，窗口已不存在时重新创建。 */
export function showMainWindow(): void {
  const window = mainWindow ?? createMainWindow();
  if (window.isMinimized()) window.restore();
  window.show();
  window.focus();
  logDesktopEvent("INFO", "main_window", "main_window_shown");
}

export function getMainWindow(): BrowserWindow | null {
  return mainWindow;
}

/** 退出流程确认之后调用：此后关闭窗口不再拦截为隐藏。 */
export function markQuitting(): void {
  quitting = true;
}

/** 渲染进程默认会话的权限：只放行页面确实用到的几项。 */
export function restrictSessionPermissions(): void {
  session.defaultSession.setPermissionRequestHandler((_webContents, permission, callback) => {
    callback(ALLOWED_PERMISSIONS.has(permission));
  });
  session.defaultSession.setPermissionCheckHandler((_webContents, permission) => ALLOWED_PERMISSIONS.has(permission));
}

/** Windows 窗口按钮的颜色跟随原生外观（`nativeTheme.themeSource` 由页面的外观偏好设置）。 */
export function followNativeThemeInTitleBar(): void {
  if (process.platform !== "win32") return;
  nativeTheme.on("updated", () => mainWindow?.setTitleBarOverlay(titleBarOverlay()));
}

function titleBarOverlay(): Electron.TitleBarOverlayOptions {
  return {
    color: "#00000000",
    symbolColor: nativeTheme.shouldUseDarkColors ? "#f5f5f5" : "#1f1f1f",
    height: WINDOWS_TITLEBAR_HEIGHT,
  };
}

/**
 * 页面只能停留在自身来源：站外链接交给系统浏览器打开，新窗口一律拒绝。
 * 渲染进程拿不到 Node 能力，这一层保证它也不会被导航到不受信任的页面。
 */
function restrictNavigation(window: BrowserWindow): void {
  window.webContents.on("will-navigate", (event, url) => {
    if (new URL(url).origin === rendererOrigin) return;
    event.preventDefault();
    openExternally(url);
  });
  window.webContents.setWindowOpenHandler(({ url }) => {
    openExternally(url);
    return { action: "deny" };
  });
}

function openExternally(url: string): void {
  const protocol = new URL(url).protocol;
  if (protocol !== "http:" && protocol !== "https:" && protocol !== "mailto:") {
    logDesktopEvent("WARNING", "main_window", "external_url_blocked", { protocol });
    return;
  }
  void shell.openExternal(url);
}

function logRendererFailures(window: BrowserWindow): void {
  window.webContents.on("render-process-gone", (_event, details) => {
    logDesktopEvent("ERROR", "main_window", "render_process_gone", { reason: details.reason, exitCode: details.exitCode });
  });
  window.on("unresponsive", () => logDesktopEvent("WARNING", "main_window", "main_window_unresponsive"));
  window.on("responsive", () => logDesktopEvent("INFO", "main_window", "main_window_responsive"));
}
