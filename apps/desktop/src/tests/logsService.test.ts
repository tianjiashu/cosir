import { describe, it, expect, vi, afterEach, type Mock } from "vitest";
import { fetchLogsByTrace, fetchRecentLogs } from "@/services/logs";
import { ServiceError } from "@/services/types";
import { useClientTraceStore } from "@/stores/clientTraceStore";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  useClientTraceStore.getState().reset();
});

function mockFetch(response: Partial<Response> | Error): Mock<(url: string, init?: RequestInit) => Promise<Response>> {
  const fetchImpl = vi.fn(async (_url: string, _init?: RequestInit) => {
    if (response instanceof Error) throw response;
    return response as Response;
  });
  vi.stubGlobal("fetch", fetchImpl);
  return fetchImpl;
}

describe("logs.ts — 后端日志查询服务", () => {
  it("fetchRecentLogs 调用 /logs/recent 并携带 trace header", async () => {
    const fetchImpl = mockFetch({
      ok: true,
      status: 200,
      headers: new Headers({ "x-trace-id": "1234567890abcdef1234567890abcdef" }),
      json: async () => ({ entries: [], text: "" }),
    } as unknown as Response);

    const result = await fetchRecentLogs({ level: "ERROR", limit: 20 });

    expect(result.entries).toEqual([]);
    const [url, init] = fetchImpl.mock.calls[0];
    expect(String(url)).toBe("/logs/recent?level=ERROR&limit=20");
    expect((init?.headers as Record<string, string>)["x-trace-id"]).toMatch(/^[0-9a-f]{32}$/);
  });

  it("fetchLogsByTrace 调用 /logs/query 并带 trace_id 参数", async () => {
    const fetchImpl = mockFetch({
      ok: true,
      status: 200,
      json: async () => ({ entries: [{ event_name: "x" }], text: "line" }),
    } as unknown as Response);

    const result = await fetchLogsByTrace({ trace_id: "trace-1", level: "INFO" });

    expect(result.text).toBe("line");
    expect(String(fetchImpl.mock.calls[0][0])).toBe("/logs/query?trace_id=trace-1&level=INFO");
  });

  it("fetchLogsByTrace 空 trace_id 直接抛出 ServiceError", async () => {
    await expect(fetchLogsByTrace({ trace_id: "" })).rejects.toBeInstanceOf(ServiceError);
  });

  it("非 2xx 响应会抛出 ServiceError", async () => {
    mockFetch({
      ok: false,
      status: 500,
      json: async () => ({ detail: "failed" }),
    } as unknown as Response);

    await expect(fetchRecentLogs()).rejects.toMatchObject({
      message: "日志查询失败: failed",
      statusCode: 500,
    });
  });

  it("fetch 原生异常会包装成中文 ServiceError", async () => {
    mockFetch(new DOMException("The string did not match the expected pattern."));

    await expect(fetchRecentLogs()).rejects.toMatchObject({
      message: "日志查询网络失败",
      statusCode: 0,
    });
  });
});
