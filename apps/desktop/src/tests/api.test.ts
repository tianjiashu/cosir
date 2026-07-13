import { describe, it, expect, vi, afterEach, type Mock } from "vitest";
import { createTask, getTask, getTaskEvents, cancelTask } from "@/services/api";
import { ServiceError } from "@/services/types";
import { API_PATHS } from "@shared/api";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
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
    mockFetch(new Error("network down"));
    await expect(createTask({ text: "x" })).rejects.toBeInstanceOf(ServiceError);
    const calls = logSpy.mock.calls.map((c) => String(c[0]));
    expect(calls.some((m) => m.includes("/tasks") && m.includes("网络请求失败"))).toBe(true);
    // 上下文含 module 与 method
    const ctxArg = logSpy.mock.calls[0][1] as {
      module?: string;
      method?: string;
      taskId?: string;
    };
    expect(ctxArg.module).toBe("api");
    expect(ctxArg.method).toBe("POST");
    logSpy.mockRestore();
  });

  it("GET fetch 抛错 → 抛出 ServiceError 且 method=GET", async () => {
    const logSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    mockFetch(new Error("offline"));
    await expect(getTask("t1")).rejects.toBeInstanceOf(ServiceError);
    const ctxArg = logSpy.mock.calls[0][1] as {
      module?: string;
      method?: string;
      taskId?: string;
    };
    expect(ctxArg.method).toBe("GET");
    expect(ctxArg.taskId).toBe("t1");
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
    warnSpy.mockRestore();
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
      json: async () => taskRecord,
    } as unknown as Response);
    const result = await createTask({ text: "x" });
    expect(result.task_id).toBe("t-1");
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
  });
});
