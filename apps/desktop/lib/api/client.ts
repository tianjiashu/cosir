import { frontendLog } from "@/lib/logging/frontend-log";
import { newTraceId, type TraceId } from "@/lib/trace";

declare global {
  interface Window {
    __COSIR_RUNTIME_CONFIG__?: {
      backendBaseUrl?: string;
    };
  }
}

export function getApiBaseUrl(): string {
  if (typeof window !== "undefined") {
    const runtimeBaseUrl = window.__COSIR_RUNTIME_CONFIG__?.backendBaseUrl;
    if (runtimeBaseUrl) return runtimeBaseUrl.replace(/\/$/, "");
  }
  const developmentUrl = import.meta.env.VITE_BACKEND_URL;
  if (developmentUrl) return developmentUrl.replace(/\/$/, "");
  return "http://127.0.0.1:8000";
}

export async function apiRequest<T>(
  path: string,
  init?: RequestInit & { traceId?: TraceId },
): Promise<T> {
  const traceId = getTraceId(init);
  const requestInit = { ...(init ?? {}) };
  delete requestInit.traceId;
  let response: Response;
  try {
    response = await fetch(`${getApiBaseUrl()}${path}`, {
      ...requestInit,
      // Workspace/task/conversation state is server-authoritative; never reuse a
      // browser-cached snapshot after a send, cancel, or task switch.
      cache: "no-store",
      headers: {
        Accept: "application/json",
        ...requestInit.headers,
        "X-Trace-Id": traceId,
      },
    });
  } catch (error) {
    await frontendLog("ERROR", "api_request_failed", "前端 API 请求失败", {
      traceId,
      data: { path, method: requestInit.method ?? "GET" },
      error,
    });
    throw error;
  }

  if (!response.ok) {
    const body = await response.text();
    let detail: string | undefined;
    try {
      const parsed = JSON.parse(body) as {
        detail?: unknown;
        error?: { message?: unknown };
      };
      if (typeof parsed.error?.message === "string") {
        detail = parsed.error.message;
      } else if (typeof parsed.detail === "string") {
        detail = parsed.detail;
      } else if (
        parsed.detail &&
        typeof parsed.detail === "object" &&
        "error" in parsed.detail &&
        typeof parsed.detail.error === "object" &&
        parsed.detail.error !== null &&
        "message" in parsed.detail.error &&
        typeof parsed.detail.error.message === "string"
      ) {
        detail = parsed.detail.error.message;
      }
    } catch {
      // 非 JSON 错误响应保留状态码即可。
    }
    const error = new Error(detail ?? `API request failed: ${response.status}`);
    await frontendLog("ERROR", "api_response_error", "前端 API 返回错误", {
      traceId,
      data: { path, method: requestInit.method ?? "GET", status: response.status },
      error,
    });
    throw error;
  }

  return (await response.json()) as T;
}

/**
 * 发起 Assistant Transport 请求并返回流响应。
 *
 * 普通 CRUD 请求由 `apiRequest` 解析 JSON；Assistant Transport 的响应是
 * text/event-stream，调用方只读取响应头中的新 conversation 身份或把流交给
 * `useAssistantTransportRuntime`，不能把它当 JSON 解析。
 */
export async function assistantTransportRequest(
  body: unknown,
  traceId: TraceId = newTraceId(),
): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(`${getApiBaseUrl()}/assistant`, {
      method: "POST",
      cache: "no-store",
      headers: {
        Accept: "text/event-stream",
        "Content-Type": "application/json",
        "X-Trace-Id": traceId,
      },
      body: JSON.stringify(body),
    });
  } catch (error) {
    await frontendLog("ERROR", "assistant_transport_request_failed", "Assistant Transport 请求失败", {
      traceId,
      data: { path: "/assistant", method: "POST" },
      error,
    });
    throw error;
  }
  if (!response.ok) {
    const bodyText = await response.text();
    let message: string | undefined;
    try {
      const parsed = JSON.parse(bodyText) as { detail?: unknown; error?: { message?: unknown } };
      if (typeof parsed.error?.message === "string") message = parsed.error.message;
      else if (typeof parsed.detail === "string") message = parsed.detail;
      else if (
        parsed.detail && typeof parsed.detail === "object" &&
        "error" in parsed.detail && typeof parsed.detail.error === "object" &&
        parsed.detail.error !== null && "message" in parsed.detail.error &&
        typeof parsed.detail.error.message === "string"
      ) message = parsed.detail.error.message;
    } catch {
      // 保留状态码错误。
    }
    const error = new Error(message ?? `Assistant Transport request failed: ${response.status}`);
    await frontendLog("ERROR", "assistant_transport_response_error", "Assistant Transport 返回错误", {
      traceId,
      data: { path: "/assistant", method: "POST", status: response.status },
      error,
    });
    throw error;
  }
  return response;
}

/** 从请求扩展字段读取调用方 trace；普通请求没有扩展字段时生成独立 trace。 */
function getTraceId(init?: RequestInit & { traceId?: TraceId }): TraceId {
  return init?.traceId ?? newTraceId();
}
