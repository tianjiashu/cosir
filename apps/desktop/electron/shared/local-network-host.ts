/**
 * 本机或局域网主机：宿主（地址栏与 Agent 打开时补 http——开发服务、局域网设备通常只开 http）与渲染进程（链接在所属
 * Session 的内置浏览器打开、不对外取站点图标）共用这一份判定，不在两边各写一份。
 *
 * `hostname` 是 URL 解析后的主机名（小写，IPv6 带方括号）。IP 字面量按回环与保留网段判断；域名里只有 `localhost`、
 * `*.localhost`、不带点的单名与 `.local` 能确定不在公网。
 */
export function isLocalNetworkHost(hostname: string): boolean {
  const host = hostname.toLowerCase();
  if (host.startsWith("[")) return host === "[::1]" || /^\[(?:f[cd]|fe[89ab])/.test(host);
  const octets = host.split(".").map(Number);
  if (octets.length === 4 && octets.every((part) => Number.isInteger(part))) {
    const [first, second] = octets;
    return first === 0 || first === 10 || first === 127 || (first === 169 && second === 254)
      || (first === 172 && second >= 16 && second <= 31)
      || (first === 192 && second === 168)
      || (first === 100 && second >= 64 && second <= 127);
  }
  return host === "localhost" || host.endsWith(".localhost") || !host.includes(".") || host.endsWith(".local");
}
