/** 工具展示数据的读取小工具：只做形状收口，不解释语义。 */

export function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null ? value as Record<string, unknown> : {};
}

export function readArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

export function readRecords(value: unknown): Record<string, unknown>[] {
  return readArray(value).map(asRecord);
}

export function readString(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

/** 有实际内容的字符串；空串与纯空白按没有。 */
export function readText(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function isPrivateIpv4(hostname: string): boolean {
  const octets = hostname.split(".").map(Number);
  if (octets.length !== 4 || octets.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) return false;
  const [first, second] = octets;
  return first === 0 || first === 10 || first === 127 || (first === 169 && second === 254)
    || (first === 172 && second >= 16 && second <= 31)
    || (first === 192 && second === 168)
    || (first === 100 && second >= 64 && second <= 127);
}

/** Return an external URL only when it is safe to put in an anchor or image. */
export function safeExternalUrl(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const candidate = value.trim();
  if (!candidate) return null;
  try {
    const url = new URL(candidate);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    const hostname = url.hostname.toLowerCase().replace(/^\[|\]$/g, "");
    if (url.username || url.password || hostname === "localhost" || hostname.endsWith(".localhost")) return null;
    if (isPrivateIpv4(hostname) || hostname === "::1" || hostname.startsWith("fc") || hostname.startsWith("fd") || hostname.startsWith("fe80:")) return null;
    return candidate;
  } catch {
    return null;
  }
}
