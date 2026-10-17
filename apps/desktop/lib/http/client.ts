import { frontendLog } from "@/lib/logging/frontend-log";
import { newTraceId, type TraceId } from "@/lib/trace";
import { parseHttpError } from "@/lib/http/errors";
import { withTimeout } from "@/lib/async/abort-timeout";

declare global {
  interface Window {
    __COSIR_RUNTIME_CONFIG__?: {
      backendBaseUrl?: string;
    };
  }
}

export type HttpRequestInit = RequestInit & {
  traceId?: TraceId;
  /**
   * 仅 requestJson 使用：整个请求（含读取响应体）的超时毫秒数，默认
   * DEFAULT_JSON_TIMEOUT_MS。超时以 TimeoutError 拒绝；长耗时操作（连接测试等）显式放宽。
   * requestRaw 不设默认超时，因为它承载 SSE 与流式响应。
   */
  timeoutMs?: number;
};

/** 领域 JSON API 的统一下限保护，避免本机后端无响应时页面永久转圈。 */
const DEFAULT_JSON_TIMEOUT_MS = 30_000;

/** 仅透传取消与超时的请求选项，供 lib/api 的读取函数接收 TanStack Query 的 signal。 */
export type RequestOptions = Pick<HttpRequestInit, "signal" | "timeoutMs">;

/** 去掉 URL 末尾的斜杠，避免拼接路径时产生双斜杠。 */
function stripTrailingSlash(url: string): string {
  return url.replace(/\/$/, "");
}

export function getApiBaseUrl(): string {
  if (typeof window !== "undefined") {
    const runtimeBaseUrl = window.__COSIR_RUNTIME_CONFIG__?.backendBaseUrl;
    if (runtimeBaseUrl) return stripTrailingSlash(runtimeBaseUrl);
  }
  const developmentUrl = import.meta.env.VITE_BACKEND_URL;
  if (developmentUrl) return stripTrailingSlash(developmentUrl);
  return "http://127.0.0.1:8000";
}

/**
 * 发起本机 HTTP 请求并返回原始响应。
 *
 * 该函数只负责 URL、公共请求头、缓存策略和网络失败日志，不消费响应体。
 * JSON 请求、取消请求等上层调用方可以据此保留各自的响应语义。
 */
export async function requestRaw(
  path: string,
  init: HttpRequestInit = {},
): Promise<Response> {
  const traceId = init.traceId ?? newTraceId();
  const requestInit = { ...init };
  delete requestInit.traceId;
  delete requestInit.timeoutMs;

  try {
    return await fetch(`${getApiBaseUrl()}${path}`, {
      ...requestInit,
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

/** 发起 JSON API 请求，并将非 2xx 响应转换为统一的 HttpError。 */
export async function requestJson<T>(
  path: string,
  init?: HttpRequestInit,
): Promise<T> {
  const traceId = init?.traceId ?? newTraceId();
  const timeout = withTimeout(init?.signal, init?.timeoutMs ?? DEFAULT_JSON_TIMEOUT_MS);
  try {
    return await requestJsonWithin<T>(path, { ...(init ?? {}), traceId, signal: timeout.signal });
  } finally {
    timeout.clear();
  }
}

async function requestJsonWithin<T>(path: string, requestInit: HttpRequestInit & { traceId: TraceId }): Promise<T> {
  const traceId = requestInit.traceId;
  const response = await requestRaw(path, requestInit);

  if (!response.ok) {
    const error = await parseHttpError(response);
    await frontendLog("ERROR", "http_response_error", "前端 HTTP 返回错误", {
      traceId,
      data: {
        path,
        method: requestInit.method ?? "GET",
        status: response.status,
        code: error.code,
      },
      error,
    });
    throw error;
  }

  return (await response.json()) as T;
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
