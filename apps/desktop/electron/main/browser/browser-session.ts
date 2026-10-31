import { session } from "electron";

import { logDesktopEvent } from "../desktop-log";

/**
 * 内置浏览器的持久分区：全部 Task 共用一个，登录一次处处可用（决定见设计稿「分区粒度」）；与应用自身的
 * 默认会话隔离，网页拿不到渲染进程的存储与权限。
 */
export const BROWSER_PARTITION = "persist:cosir-browser";

/**
 * 分区级设置，宿主就绪后调用一次：
 * - UA 去掉 `Electron/x`：带它的 UA 会被不少网站当成异常客户端（拒绝登录、降级页面）。
 * - 站点权限（摄像头、定位、通知……）一律拒绝，v1 不开放。
 * - 下载一律取消：Agent 与用户都不经内置浏览器落盘文件。
 */
export function configureBrowserSession(): void {
  const browserSession = session.fromPartition(BROWSER_PARTITION);
  browserSession.setUserAgent(browserSession.getUserAgent().replace(/ Electron\/\S+/, ""));
  browserSession.setPermissionRequestHandler((_webContents, permission, callback) => {
    logDesktopEvent("INFO", "browser", "browser_permission_denied", { permission });
    callback(false);
  });
  browserSession.setPermissionCheckHandler(() => false);
  browserSession.on("will-download", (event) => {
    event.preventDefault();
    logDesktopEvent("INFO", "browser", "browser_download_blocked");
  });
}
