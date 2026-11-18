import type { Cookie, Session } from "electron";

import { writeNetworkRecord } from "./network-record";

/**
 * 常驻的分区级 Cookie 变化诊断：CDP 只能看到 Cookie 随请求发出、Set-Cookie 被拦，看不到存进去以后的事——被覆盖、
 * 过期、被挤出或被页面脚本删除（「登录成功却是游客」要据此区分）。`cause` 原样记下 Chromium 给的原因。
 * 只记身份与属性，绝不记值；与标签网络记录写进同一个日志，按时间对照。整个分区共用，没有标签归属。
 */
export function observeCookieChanges(browserSession: Session): void {
  browserSession.cookies.on("changed", (_event, cookie: Cookie, cause: string, removed: boolean) => {
    writeNetworkRecord("browser_cookie_changed", {
      name: cookie.name.slice(0, 128), domain: cookie.domain, path: cookie.path?.slice(0, 500), host_only: cookie.hostOnly,
      secure: cookie.secure, http_only: cookie.httpOnly, same_site: cookie.sameSite,
      session: cookie.session, expiration_date: cookie.expirationDate, cause, removed,
    });
  });
}
