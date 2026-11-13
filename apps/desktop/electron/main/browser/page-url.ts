import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

import { browserError } from "./errors";
import { isLocalNetworkHost } from "../../shared/local-network-host";

/**
 * 内置浏览器能打开哪些地址的唯一表达：网页（http/https）与本机文件（file）。用户与 Agent 的打开、页面自身的跳转与
 * 弹窗都按它判定；`javascript:`、`chrome:`、拉起其他应用的地址（`mailto:`、`vscode:` 等）一律拦下。网上页面跳到本机
 * 文件由 Chromium 自己拦（非 file 来源不能请求 file 地址），这里不重复；本机页面之间的链接因此照常可用。
 */
const PAGE_PROTOCOLS = new Set(["http:", "https:", "file:"]);

export function isPageUrl(url: string): boolean {
  return URL.canParse(url) && PAGE_PROTOCOLS.has(new URL(url).protocol);
}

/**
 * 用户在地址栏输入、或 Agent 给出的地址 → 可打开的地址；两条入口共用，规则一致。不合法报 `INVALID_PARAMS`，
 * 协议不在 `PAGE_PROTOCOLS` 报 `NAVIGATION_BLOCKED`（模型可据此换地址，而不是当作调用写错）。
 */
export function pageUrl(input: string): string {
  const address = addressToUrl(input.trim());
  if (!URL.canParse(address)) throw browserError("INVALID_PARAMS", `not a valid address: ${input.slice(0, 200)}`);
  const url = new URL(address);
  if (!PAGE_PROTOCOLS.has(url.protocol)) {
    throw browserError("NAVIGATION_BLOCKED", `only web pages (http, https) and local files can be opened (got ${url.protocol})`);
  }
  return url.href;
}

/**
 * 补全成带协议的地址（还没校验协议）：
 * - 本机绝对路径（`/…`、`~/…`、`C:\…`）→ file 地址；必须先于协议判断，否则 `C:\` 会被当成名为 `c:` 的协议。
 * - 写了协议的照原样；冒号后面紧跟数字的是端口不是协议（`localhost:3000` 不能当成名为 `localhost:` 的协议）。
 * - 其余按主机补：本机与局域网补 http，其余补 https。
 */
export function addressToUrl(text: string): string {
  const local = text === "~" || text.startsWith("~/") ? path.join(os.homedir(), text.slice(1)) : text;
  if (path.isAbsolute(local)) return pathToFileURL(local).href;
  if (/^[a-z][a-z0-9+.-]*:(?!\d)/i.test(text)) return text;
  const host = URL.canParse(`http://${text}`) ? new URL(`http://${text}`).hostname : "";
  return `${isLocalNetworkHost(host) ? "http" : "https"}://${text}`;
}
