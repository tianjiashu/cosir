import { frontendLog } from "@/lib/logging/frontend-log";
import { newTraceId, type TraceId } from "@/lib/trace";
import { parseHttpError } from "@/lib/http/errors";

declare global {
  interface Window {
    __COSIR_RUNTIME_CONFIG__?: {
      backendBaseUrl?: string;
    };
  }
}

export type HttpRequestInit = RequestInit & { traceId?: TraceId };

export function getApiBaseUrl(): string {
  if (typeof window !== "undefined") {
    const runtimeBaseUrl = window.__COSIR_RUNTIME_CONFIG__?.backendBaseUrl;
    if (runtimeBaseUrl) return runtimeBaseUrl.replace(/\/$/, "");
  }
  const developmentUrl = import.meta.env.VITE_BACKEND_URL;
  if (developmentUrl) return developmentUrl.replace(/\/$/, "");
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
  const requestInit = { ...(init ?? {}), traceId };
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
