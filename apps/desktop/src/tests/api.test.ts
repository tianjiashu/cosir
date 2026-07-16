import { describe, it, expect, vi, afterEach, type Mock } from "vitest";
import { createTask, getTask, getTaskEvents, getTaskCheckpoints, cancelTask } from "@/services/api";
import { ServiceError } from "@/services/types";
import { API_PATHS } from "@shared/api";
import { useClientTraceStore } from "@/stores/clientTraceStore";
import { useConversationTraceStore } from "@/stores/conversationTraceStore";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  useClientTraceStore.getState().reset();
  useConversationTraceStore.getState().resetConversationTraces();
});

function mockFetch(
  response: Partial<Response> | Error | null,
): Mock<(url: string, init?: RequestInit) => Promise<Response>> {
  const fetchImpl = vi.fn(async (_url: string, _init?: RequestInit) => {
    if (response instanceof Error) throw response;
    return response as Response;
  });
  vi.stubGlobal("fetch", fetchImpl);
  return fetchImpl;
}

describe("api.ts — post/get 网络失败分支", () => {
  it("POST fetch 抛错 → 抛出 ServiceError 且经 logError（含 path/method）", async () => {
    const logSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    const fetchImpl = mockFetch(new Error("network down"));
    await expect(createTask({ text: "x" })).rejects.toBeInstanceOf(ServiceError);
    const init = fetchImpl.mock.calls[0][1] as RequestInit;
    const headers = init.headers as Record<string, string>;
    expect(headers["x-trace-id"]).toMatch(/^[0-9a-f]{32}$/);
    const calls = logSpy.mock.calls.map((c) => String(c[0]));
    expect(calls.some((m) => m.includes("/tasks") && m.includes("网络请求失败"))).toBe(true);
    // 上下文含 module 与 method
    const ctxArg = logSpy.mock.calls[0][1] as {
      module?: string;
      method?: string;
      task_id?: string;
      trace_id?: string;
    };
    expect(ctxArg.module).toBe("api");
    expect(ctxArg.method).toBe("POST");
    expect(ctxArg.trace_id).toMatch(/^[0-9a-f]{32}$/);
    logSpy.mockRestore();
  });

  it("GET fetch 抛错 → 抛出 ServiceError 且 method=GET", async () => {
    const logSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    mockFetch(new Error("offline"));
    await expect(getTask("t1")).rejects.toBeInstanceOf(ServiceError);
    const ctxArg = logSpy.mock.calls[0][1] as {
      module?: string;
      method?: string;
      task_id?: string;
    };
    expect(ctxArg.method).toBe("GET");
    expect(ctxArg.task_id).toBe("t1");
    logSpy.mockRestore();
  });

  it("非 2xx 响应 → 抛出 ServiceError 且 statusCode 正确", async () => {
    mockFetch({
      ok: false,
      status: 404,
      statusText: "Not Found",
      clone: () => ({
        json: async () => ({ detail: "task not found" }),
      }),
    } as unknown as Response);
    try {
      await getTask("missing");
      throw new Error("should have thrown");
    } catch (e) {
      expect(e).toBeInstanceOf(ServiceError);
      expect((e as ServiceError).statusCode).toBe(404);
      expect((e as ServiceError).message).toContain("task not found");
    }
  });

  it("非 2xx 响应且响应体非 JSON → 使用原始消息（logWarn 触发）", async () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    mockFetch({
      ok: false,
      status: 500,
      statusText: "Server Error",
      clone: () => ({
        json: async () => {
          throw new SyntaxError("not json");
        },
      }),
    } as unknown as Response);
    try {
      await getTaskEvents("t1");
      throw new Error("should have thrown");
    } catch (e) {
      expect(e).toBeInstanceOf(ServiceError);
      expect((e as ServiceError).statusCode).toBe(500);
    }
    // 解析失败分支应触发 logWarn
    expect(warnSpy).toHaveBeenCalled();
    const warnMsg = String(warnSpy.mock.calls[0][0]);
    expect(warnMsg).toContain("解析错误响应体 JSON 失败");
    const errorCtx = errorSpy.mock.calls[0][1] as Record<string, unknown>;
    expect(errorCtx.trace_id).toMatch(/^[0-9a-f]{32}$/);
    expect(errorCtx.status_code).toBe(500);
    warnSpy.mockRestore();
    errorSpy.mockRestore();
  });

  it("GET 任务详情、事件和检查点会记录各自对话 trace", async () => {
    const fetchImpl = vi.fn(async (url: string) => {
      if (url.endsWith("/events")) {
        return { ok: true, status: 200, json: async () => [] } as unknown as Response;
      }
      if (url.endsWith("/checkpoints")) {
        return { ok: true, status: 200, json: async () => [] } as unknown as Response;
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({
          task_id: "t1",
          session_id: "s1",
          agent_id: "a1",
          input_text: "x",
          status: "running",
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z",
        }),
      } as unknown as Response;
    });
    vi.stubGlobal("fetch", fetchImpl);

    await getTask("t1");
    await getTaskEvents("t1");
    await getTaskCheckpoints("t1");

    const byOperation = useConversationTraceStore.getState().latestTraceByTaskIdAndOperation.t1;
    expect(byOperation?.task_get).toMatchObject({ operation: "task_get", path: API_PATHS.TASK_DETAIL("t1") });
    expect(byOperation?.task_events).toMatchObject({ operation: "task_events", path: API_PATHS.TASK_EVENTS("t1") });
    expect(byOperation?.task_checkpoints).toMatchObject({
      operation: "task_checkpoints",
      path: API_PATHS.TASK_CHECKPOINTS("t1"),
    });
  });

  it("2xx 响应但 JSON 解析失败（response.json 抛错）→ 抛出 ServiceError 且经 logError 记录", async () => {
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    mockFetch({
      ok: true,
      status: 200,
      json: async () => {
        throw new SyntaxError("unexpected token");
      },
    } as unknown as Response);
    // 契约要求（api.ts @throws {ServiceError}）：2xx 但响应体非 JSON 时，
    // 应包装为 ServiceError 并经 logError 写日志（含定位上下文）。
    let thrown: unknown;
    try {
      await getTask("t1");
    } catch (e) {
      thrown = e;
    }
    expect(thrown).toBeInstanceOf(ServiceError);
    // 错误路径应产生 logError 记录（经统一出口，含定位上下文）
    const apiErrorLogged = errSpy.mock.calls.some((c) =>
      String(c[0]).includes("网络请求失败") || String(c[0]).includes("解析"),
    );
    expect(apiErrorLogged).toBe(true);
    errSpy.mockRestore();
  });

  it("正常 2xx POST → 返回解析后的数据", async () => {
    const taskRecord = {
      task_id: "t-1",
      session_id: "s-1",
      agent_id: "a-1",
      input_text: "x",
      status: "pending",
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
    mockFetch({
      ok: true,
      status: 200,
      headers: new Headers({
        "x-trace-id": "1234567890abcdef1234567890abcdef",
      }),
      json: async () => taskRecord,
    } as unknown as Response);
    const result = await createTask({ text: "x" });
    expect(result.task_id).toBe("t-1");
    const lastTrace = useClientTraceStore.getState().lastTrace;
    expect(lastTrace?.traceId).toBe("1234567890abcdef1234567890abcdef");
    expect(useConversationTraceStore.getState().latestTraceByTaskId["t-1"]).toMatchObject({
      traceId: expect.stringMatching(/^[0-9a-f]{32}$/),
      taskId: "t-1",
      operation: "task_create",
      method: "POST",
      path: API_PATHS.TASKS,
    });
  });

  it("cancelTask 走 POST 且路径正确", async () => {
    const fetchImpl = mockFetch({
      ok: true,
      status: 200,
      json: async () => ({ task_id: "t1", status: "cancelled" }),
    } as unknown as Response);
    await cancelTask("t1");
    const calledUrl = fetchImpl.mock.calls[0][0] as string;
    expect(calledUrl).toBe(API_PATHS.TASK_CANCEL("t1"));
    expect(useConversationTraceStore.getState().latestTraceByTaskId.t1).toMatchObject({
      taskId: "t1",
      operation: "task_cancel",
      method: "POST",
      path: API_PATHS.TASK_CANCEL("t1"),
    });
  });
});
