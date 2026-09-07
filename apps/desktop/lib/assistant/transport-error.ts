import type { TransportError } from "@/lib/assistant/contract";
import { parseStructuredHttpError } from "@/lib/http/errors";

/**
 * 从 Assistant UI transport 抛出的普通 Error 中恢复后端结构化错误。
 *
 * Assistant UI 当前会把非 2xx 响应包装成 `Status <code>: <body>`，而不会把
 * Response 对象传给应用回调。这里只解析符合后端 TransportError 契约的 JSON，
 * 不向 UI 暴露其他响应正文。
 */
export function parseTransportError(error: unknown): TransportError | null {
  if (!(error instanceof Error)) return null;

  const separator = error.message.indexOf(":");
  if (separator < 0) return null;

  try {
    const payload: unknown = JSON.parse(error.message.slice(separator + 1).trim());
    return parseStructuredHttpError(payload) as TransportError | null;
  } catch {
    return null;
  }
}
