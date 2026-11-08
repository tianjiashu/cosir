import path from "node:path";

import { app, session, type DownloadItem, type WebContents, type WebPreferences } from "electron";

import { logDesktopEvent } from "../desktop-log";

/**
 * 内置浏览器的持久分区：全部 Task 共用一个，登录一次处处可用（决定见设计稿「分区粒度」）；与应用自身的
 * 默认会话隔离，网页拿不到渲染进程的存储与权限。
 */
const BROWSER_PARTITION = "persist:cosir-browser";

/**
 * 每个标签页的 webPreferences；新建的标签与页面开出的标签（`setWindowOpenHandler` 的
 * `overrideBrowserWindowOptions`）共用这一份，否则开出的页面会少了弹窗接管。
 * - `preload` + `nodeIntegrationInSubFrames`：网页 preload 接管 JS 弹窗，每个框架都运行
 *   （`electron/preload/browser-page.ts`）。
 * - `disableDialogs`：preload 之外仍可能走到 Electron 默认弹窗（如 `beforeunload`），默认处理在标签不挂在窗口上时
 *   会卡住主进程，所以保持关闭。
 * - `defaultEncoding`：没声明编码的页面按 UTF-8 解码；否则 Chromium 按系统语言猜，中文页会成乱码。
 */
export function browserPageWebPreferences(): WebPreferences {
  return {
    partition: BROWSER_PARTITION,
    sandbox: true,
    contextIsolation: true,
    nodeIntegration: false,
    nodeIntegrationInSubFrames: true,
    preload: path.join(__dirname, "browser-page-preload.cjs"),
    disableDialogs: true,
    defaultEncoding: "UTF-8",
  };
}

/** 分区级事件按来源网页找到所属标签与 Task 处理；由 `BrowserHost` 实现（它持有全部标签与下载登记）。 */
export type BrowserSessionEvents = {
  download(source: WebContents, item: DownloadItem): void;
  permissionDenied(source: WebContents, permission: string): void;
};

/**
 * 分区级设置，宿主就绪后调用一次：
 * - UA 去掉 `Electron/x`：带它的 UA 会被不少网站当成异常客户端（拒绝登录、降级页面）。
 * - 站点权限（摄像头、定位、通知……）一律拒绝，v1 不开放；拒绝后告诉所属标签。
 * - 下载登记到来源标签所属的 Task，落盘到标签创建时定下的下载目录（`downloads.ts`）。
 */
export function configureBrowserSession(events: BrowserSessionEvents): void {
  const browserSession = session.fromPartition(BROWSER_PARTITION);
  browserSession.setPermissionRequestHandler((webContents, permission, callback) => {
    logDesktopEvent("INFO", "browser", "browser_permission_denied", { permission });
    events.permissionDenied(webContents, permission);
    callback(false);
  });
  browserSession.setPermissionCheckHandler(() => false);
  browserSession.on("will-download", (_event, item, webContents) => events.download(webContents, item));
}

/** 在任何 WebContents 创建前设置唯一默认 UA；popup 的首个请求也从这里取。 */
export function configureBrowserUserAgent(): void {
  app.userAgentFallback = app.userAgentFallback.replace(/ Electron\/\S+/, "");
}
