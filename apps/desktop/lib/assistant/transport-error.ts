import { parseStructuredHttpError, type StructuredHttpError } from "@/lib/http/errors";

/**
 * 从 Assistant UI transport 抛出的普通 Error 中恢复后端的结构化 **HTTP 错误体**。
 *
 * Assistant UI 当前会把非 2xx 响应包装成 `Status <code>: <body>`，而不会把
 * Response 对象传给应用回调。这里只解析符合后端 HTTP 错误契约
 * （`{error:{code,message,retryable}}`，与 `lib/http/errors.ts` 的 `StructuredHttpError` 对齐）
 * 的 JSON，不向 UI 暴露其它响应正文。
 *
 * 与 Transport snapshot 的错误契约（`lib/assistant/contract.ts` 的 `TransportError`，
 * 只有 `code` / `message`）是两套独立契约，不要互相复用类型。
 */
export function parseTransportError(error: unknown): StructuredHttpError | null {
  if (!(error instanceof Error)) return null;

  const separator = error.message.indexOf(":");
  if (separator < 0) return null;

  try {
    const payload: unknown = JSON.parse(error.message.slice(separator + 1).trim());
    return parseStructuredHttpError(payload);
  } catch {
    return null;
  }
}
