import { useEffect, useSyncExternalStore } from "react";
import {
  siFigma,
  siGithub,
  siGitlab,
  siGoogle,
  siNotion,
  siNpm,
  siPypi,
  siStackoverflow,
  siWikipedia,
  siX,
  siYoutube,
} from "simple-icons";

/**
 * 高频站点的内置图标（按注册域匹配，子域名同样命中）。它们不联网、跟随文字颜色，所以深色背景上也看得清；
 * 只收常见站点，其余交给 `useFaviconUrl`。
 */
const BRAND_ICON_PATHS = new Map<string, string>([
  ["figma.com", siFigma.path],
  ["github.com", siGithub.path],
  ["githubusercontent.com", siGithub.path],
  ["gitlab.com", siGitlab.path],
  ["google.com", siGoogle.path],
  ["notion.so", siNotion.path],
  ["npmjs.com", siNpm.path],
  ["pypi.org", siPypi.path],
  ["stackoverflow.com", siStackoverflow.path],
  ["twitter.com", siX.path],
  ["wikipedia.org", siWikipedia.path],
  ["x.com", siX.path],
  ["youtu.be", siYoutube.path],
  ["youtube.com", siYoutube.path],
]);

/** 主机名对应的内置图标路径（24×24 viewBox）；没有收录时为 null。 */
export function brandIconPath(host: string): string | null {
  for (let domain = host; domain.includes("."); domain = domain.slice(domain.indexOf(".") + 1)) {
    const path = BRAND_ICON_PATHS.get(domain);
    if (path) return path;
  }
  return null;
}

type FaviconStatus = "loading" | "loaded" | "failed";

/**
 * 每个主机一条加载结果：同一站点的多个链接只发一次请求，列表重新挂载时直接显示、不闪烁。
 * 只记在内存里，图片本身由 Chromium 的 HTTP 缓存保存；失败的主机下次启动再试。
 */
const statuses = new Map<string, FaviconStatus>();
const listeners = new Set<() => void>();

const faviconUrl = (host: string) => `https://${host}/favicon.ico`;

function settle(host: string, status: FaviconStatus) {
  statuses.set(host, status);
  listeners.forEach((notify) => notify());
}

/**
 * 只取 `/favicon.ico`：渲染进程受跨域限制读不到网页 HTML，解析不了 `<link rel="icon">`，这类站点回落到通用图标。
 * 只用 https，避免 http 站点在 `app://` 页面里成为混合内容。不带来源页，站点只知道有人取了图标。
 */
function loadFavicon(host: string) {
  if (statuses.has(host)) return;
  statuses.set(host, "loading");
  const image = new Image();
  image.referrerPolicy = "no-referrer";
  // 部分站点用 1×1 透明图占位，当作没有图标，否则链接前只留一块空白。
  image.onload = () => settle(host, image.naturalWidth > 1 ? "loaded" : "failed");
  image.onerror = () => settle(host, "failed");
  image.src = faviconUrl(host);
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => void listeners.delete(listener);
}

/** 站点图标地址；加载完成前和失败时为 null，调用方显示通用图标。`host` 必须是公网主机（`WebLink.publicHost`）。 */
export function useFaviconUrl(host: string): string | null {
  const read = () => statuses.get(host);
  const status = useSyncExternalStore(subscribe, read, read);
  useEffect(() => loadFavicon(host), [host]);
  return status === "loaded" ? faviconUrl(host) : null;
}
