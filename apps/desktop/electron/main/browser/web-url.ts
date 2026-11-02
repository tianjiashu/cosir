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

/** 本机地址：开发服务通常只开 http，补 https 会连不上。 */
const LOCAL_HOST = /^(localhost|127(?:\.\d{1,3}){3}|0\.0\.0\.0|\[::1\])(?::\d+)?(?:[/?#]|$)/i;

/**
 * 用户在地址栏输入的文字 → 带协议的地址（还没校验协议，交给 `params.httpUrl`）。写了协议的照原样；没写的补上：本机
 * 地址补 http，其余补 https。冒号后面紧跟数字的是端口不是协议（`localhost:3000` 不能当成名为 `localhost:` 的协议）。
 */
export function addressToUrl(text: string): string {
  if (/^[a-z][a-z0-9+.-]*:(?!\d)/i.test(text)) return text;
  return `${LOCAL_HOST.test(text) ? "http" : "https"}://${text}`;
}

