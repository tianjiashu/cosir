import { describe, it, expect, vi, afterEach, type Mock } from "vitest";
import { cancelTurn, createTask, getTask, listAgents, listTaskTurns } from "@/services/api";
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
    await expect(createTask({ text: "x", workspace_id: "workspace-1" })).rejects.toBeInstanceOf(ServiceError);
    const init = fetchImpl.mock.calls[0][1] as RequestInit;
    const headers = init.headers as Record<string, string>;
    expect(headers["x-trace-id"]).toMatch(/^[0-9a-f]{32}$/);
    expect(logSpy).toHaveBeenCalled();
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
      await listTaskTurns("t1");
      throw new Error("should have thrown");
    } catch (e) {
      expect(e).toBeInstanceOf(ServiceError);
      expect((e as ServiceError).statusCode).toBe(500);
    }
    // 解析失败分支应触发 logWarn
    expect(warnSpy).toHaveBeenCalled();
    expect(String(warnSpy.mock.calls[0][0])).toContain("JSON");
    const errorCtx = errorSpy.mock.calls[0][1] as Record<string, unknown>;
    expect(errorCtx.trace_id).toMatch(/^[0-9a-f]{32}$/);
    expect(errorCtx.status_code).toBe(500);
    warnSpy.mockRestore();
    errorSpy.mockRestore();
  });

  it("GET 任务详情和轮次列表会记录各自对话 trace", async () => {
    const fetchImpl = vi.fn(async (url: string) => {
      if (url.endsWith("/turns")) {
        return { ok: true, status: 200, json: async () => [] } as unknown as Response;
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({
          task_id: "t1",
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
    await listTaskTurns("t1");

    const byOperation = useConversationTraceStore.getState().latestTraceByTaskIdAndOperation.t1;
    expect(byOperation?.task_get).toMatchObject({ operation: "task_get", path: API_PATHS.TASK_DETAIL("t1") });
    expect(byOperation?.task_turns).toMatchObject({ operation: "task_turns", path: API_PATHS.TASK_TURNS("t1") });
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
    expect(errSpy).toHaveBeenCalled();
    errSpy.mockRestore();
  });

  it("正常 2xx POST → 返回解析后的数据", async () => {
    const taskRecord = {
      task_id: "t-1",
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
    const result = await createTask({ text: "x", workspace_id: "workspace-1" });
    expect(result.task_id).toBe("t-1");
    const lastTrace = useClientTraceStore.getState().lastTrace;
    expect(lastTrace?.traceId).toBe("1234567890abcdef1234567890abcdef");
    expect(useConversationTraceStore.getState().latestTraceByTaskId["t-1"]).toMatchObject({
      traceId: expect.stringMatching(/^[0-9a-f]{32}$/),
      taskId: "t-1",
      operation: "task_create",
      method: "POST",
      path: API_PATHS.WORKSPACE_TASKS("workspace-1"),
    });
  });

  it("cancelTurn 走 POST 且路径正确", async () => {
    const fetchImpl = mockFetch({
      ok: true,
      status: 200,
      json: async () => ({ turn_id: "turn-1", task_id: "t1", status: "cancelled" }),
    } as unknown as Response);
    await cancelTurn("turn-1", "t1");
    const calledUrl = fetchImpl.mock.calls[0][0] as string;
    expect(calledUrl).toBe(API_PATHS.TURN_CANCEL("turn-1"));
    expect(useConversationTraceStore.getState().latestTraceByTaskId.t1).toMatchObject({
      taskId: "t1",
      operation: "turn_cancel",
      method: "POST",
      path: API_PATHS.TURN_CANCEL("turn-1"),
    });
  });

  it("listAgents 走 GET /agents 并返回默认 agent", async () => {
    const fetchImpl = mockFetch({
      ok: true,
      status: 200,
      json: async () => ({
        agents: [
          {
            agent_id: "developer",
            role: "developer",
            goal: "coding",
            allowed_tools: ["read_file"],
            context_policy: "text_only_v1",
            workflow: "react_like_v1",
            model_name: "deepseek-v4-flash",
            max_steps: 1000,
          },
        ],
        default_agent_id: "developer",
      }),
    } as unknown as Response);

    const result = await listAgents();

    expect(fetchImpl.mock.calls[0][0]).toBe(API_PATHS.AGENTS);
    expect(result.default_agent_id).toBe("developer");
    expect(result.agents[0].agent_id).toBe("developer");
  });
});
