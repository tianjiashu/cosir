/**
 * 内置浏览器「只允许 http(s)」这条规则的唯一表达：Agent 打开的地址、页面自身的跳转与弹窗都按它判定，
 * `file:`、`javascript:`、`chrome:` 等一律拦下。解析不了的地址视为非网页地址。
 */
const WEB_PROTOCOLS = new Set(["http:", "https:"]);

/** 地址的协议（如 `https:`）；解析不了时为空串。 */
export function urlProtocol(url: string): string {
  return URL.canParse(url) ? new URL(url).protocol : "";
}

export function isWebProtocol(protocol: string): boolean {
  return WEB_PROTOCOLS.has(protocol);
}
