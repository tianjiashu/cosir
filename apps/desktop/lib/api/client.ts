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
  return "/api/backend";
}

export async function apiRequest<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(`${getApiBaseUrl()}${path}`, {
    ...init,
    // Workspace/task/conversation state is server-authoritative; never reuse a
    // browser-cached snapshot after a send, cancel, or task switch.
    cache: "no-store",
    headers: {
      Accept: "application/json",
      ...init?.headers,
    },
  });

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
    throw new Error(detail ?? `API request failed: ${response.status}`);
  }

  return (await response.json()) as T;
}
