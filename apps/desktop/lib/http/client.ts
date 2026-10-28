import { frontendLog } from "@/lib/logging/frontend-log";
import { newTraceId, type TraceId } from "@/lib/trace";
import { parseHttpError } from "@/lib/http/errors";
import { withTimeout } from "@/lib/async/abort-timeout";
import { getBackendRuntimeSnapshot } from "@/src/runtime-config";

export type HttpRequestInit = RequestInit & {
  traceId?: TraceId;
  /**
   * 超时毫秒数，以 TimeoutError 中止（与调用方 signal 叠加）。requestJson 覆盖到读完响应体，缺省
   * DEFAULT_JSON_TIMEOUT_MS；requestRaw / requestOk 只覆盖到拿到响应头，且不传就不设超时——它们承载
   * SSE、流式响应，以及发送这类非幂等命令（超时后结果未知，重发会重复受理）。
   */
  timeoutMs?: number;
};

/** 领域 JSON API 的统一下限保护，避免本机后端无响应时页面永久转圈。 */
const DEFAULT_JSON_TIMEOUT_MS = 30_000;

/** 仅透传取消与超时的请求选项，供 lib/api 的读取函数接收 TanStack Query 的 signal。 */
export type RequestOptions = Pick<HttpRequestInit, "signal" | "timeoutMs">;

/** 当前后端实例的基地址；唯一事实源是 runtime-config（含后端不可用期间的占位地址）。 */
export function getApiBaseUrl(): string {
  return getBackendRuntimeSnapshot().backendBaseUrl;
}

/**
 * 发起本机 HTTP 请求并返回原始响应。
 *
 * 该函数只负责 URL、公共请求头、缓存策略、可选超时和网络失败日志，不消费响应体。
 * JSON 请求、取消请求等上层调用方可以据此保留各自的响应语义。
 */
async function requestRaw(
  path: string,
  init: HttpRequestInit = {},
): Promise<Response> {
  const { traceId = newTraceId(), timeoutMs, ...requestInit } = init;
  const timeout = timeoutMs === undefined ? null : withTimeout(requestInit.signal, timeoutMs);

  try {
    return await fetch(`${getApiBaseUrl()}${path}`, {
      ...requestInit,
      signal: timeout?.signal ?? requestInit.signal,
      cache: "no-store",
      headers: {
        Accept: "application/json",
        ...requestInit.headers,
        "X-Trace-Id": traceId,
      },
    });
  } catch (error) {
    // Abort is an expected lifecycle signal for shared catalog reloads and
    // runtime teardown, not an HTTP failure worth surfacing in diagnostics.
    const isAbort =
      typeof DOMException !== "undefined" &&
      error instanceof DOMException &&
      error.name === "AbortError";
    if (isAbort) throw error;
    await frontendLog("ERROR", "http_request_failed", "前端 HTTP 请求失败", {
      traceId,
      data: { path, method: requestInit.method ?? "GET" },
      error,
    });
    throw error;
  }
}

/**
 * 发起请求并要求 2xx：非 2xx 解析为 HttpError 抛出并记日志，成功响应体留给调用方消费。
 *
 * 4xx 多是业务冲突或输入被拒（如 409 `MESSAGE_CONSUMED`），记 WARNING；5xx 才是后端故障，记 ERROR，
 * 避免预期内的冲突淹没真正的错误。
 */
export async function requestOk(path: string, init: HttpRequestInit = {}): Promise<Response> {
  const traceId = init.traceId ?? newTraceId();
  const response = await requestRaw(path, { ...init, traceId });
  if (response.ok) return response;
  const error = await parseHttpError(response);
  await frontendLog(response.status >= 500 ? "ERROR" : "WARNING", "http_response_error", "前端 HTTP 返回错误", {
    traceId,
    data: { path, method: init.method ?? "GET", status: response.status, code: error.code },
    error,
  });
  throw error;
}

/** 发起 JSON API 请求，并将非 2xx 响应转换为统一的 HttpError。 */
export async function requestJson<T>(
  path: string,
  init: HttpRequestInit = {},
): Promise<T> {
  const { timeoutMs = DEFAULT_JSON_TIMEOUT_MS, ...requestInit } = init;
  // 超时要覆盖读完响应体，所以在这里叠加，而不是交给 requestRaw（它只管到响应头）。
  const timeout = withTimeout(requestInit.signal, timeoutMs);
  try {
    const response = await requestOk(path, { ...requestInit, signal: timeout.signal });
    return (await response.json()) as T;
  } finally {
    timeout.clear();
  }
}

/** 构造带 JSON 请求体的请求 init（设置 Content-Type 并序列化 body）。 */
export function jsonRequestInit(body: unknown, init?: HttpRequestInit): HttpRequestInit {
  return {
    ...(init ?? {}),
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    body: JSON.stringify(body),
  };
}

/** 发送 JSON 请求并解析 JSON 响应（自动注入 traceId 与 Content-Type）。 */
export async function sendJson<T>(
  path: string,
  method: "POST" | "PUT" | "PATCH",
  body: unknown,
  init?: HttpRequestInit,
): Promise<T> {
  return requestJson<T>(path, jsonRequestInit(body, { ...(init ?? {}), method }));
}

/** 发送 JSON POST 请求并解析 JSON 响应。 */
export async function postJson<T>(path: string, body: unknown, init?: HttpRequestInit): Promise<T> {
  return sendJson<T>(path, "POST", body, init);
}
