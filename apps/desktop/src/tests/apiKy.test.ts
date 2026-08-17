// @vitest-environment happy-dom
/**
 * api.ts（基于 ky 的 httpClient）单元测试。
 *
 * 验证点：
 * - post/get/del 内部正确调用 apiClient.post/get/delete（方法、路径、body、headers）。
 * - 正常响应返回 .json() 数据。
 * - HTTP 错误（apiClient 已归一为 ServiceError）向上抛出，且错误路径的 logError 被调用并带
 *   module/path/status_code 上下文（不校验敏感值）。
 * - SSE（connectWorkspaceEventStream）仍走原生 fetch，未被 ky 接管。
 *
 * 策略：mock "@/services/httpClient" 整个模块替换 apiClient，避免真实网络；
 * 但若 SSE 用例需要真实 fetch，则 spy globalThis.fetch 即可（SSE 不走 apiClient）。
 *
 * @module tests/apiKy
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// vi.hoisted：在 vi.mock 工厂被 hoist 之前初始化 apiClientMock，供工厂闭包引用。
const h = vi.hoisted(() => ({
  apiClientMock: {
    post: vi.fn(),
    get: vi.fn(),
    delete: vi.fn(),
  },
}));

// ---- 真实 logger 在测试环境会写 console；此处用 spy 以便断言上下文。 ----
const logError = vi.fn();
const logWarn = vi.fn();
vi.mock("@/lib/logger", () => ({
  logInfo: () => {},
  logWarn: (...args: unknown[]) => logWarn(...args),
  logError: (...args: unknown[]) => logError(...args),
}));

// ---- mock conversationTraceStore，避免触碰真实 store 状态。 ----
const recordTrace = vi.fn();
vi.mock("@/stores/conversationTraceStore", () => ({
  useConversationTraceStore: { getState: () => ({ recordTrace }) },
}));

// ---- mock httpClient：用可控的 apiClient 替换，验证 api.ts 的调用与透传。 ----
vi.mock("@/services/httpClient", () => ({
  apiClient: h.apiClientMock,
  normalizeToServiceError: (e: unknown) => e,
}));

import { ServiceError } from "@/services/types";
import {
  createTask,
  getTask,
  deleteTask,
  listWorkspaces,
  getBackendHealth,
  connectWorkspaceEventStream,
  createWorkspace,
  deleteWorkspace,
  prepareWorkspace,
  listWorkspaceTasks,
  createTaskTurn,
  listTaskTurns,
  fetchChangeSet,
  keepChanges,
  revertChanges,
  listTaskEvents,
  cancelTurn,
  listAgents,
} from "@/services/api";

beforeEach(() => {
  h.apiClientMock.post.mockReset();
  h.apiClientMock.get.mockReset();
  h.apiClientMock.delete.mockReset();
  logError.mockReset();
  recordTrace.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

/** 构造一个可被 .json() 的 Response-like 对象。 */
function jsonResponse<T>(data: T): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    headers: new Headers({ "x-trace-id": "resp-trace-0000" }),
    json: async () => data,
    clone: () => jsonResponse(data),
  } as unknown as Response;
}

describe("api.ts - createTask（POST）调用正确性与错误透传", () => {
  it("正常：调用 apiClient.post，返回 .json() 数据，并写 conversation trace", async () => {
    // 测试目的：POST 路径接线正确（方法/路径/body/header），正常数据透传；
    // 可能发现缺陷：body 未传递 / 路径拼错 / 返回值未取 .json()。
    const task = { task_id: "t-1", text: "hi", workspace_id: "ws-1" } as never;
    h.apiClientMock.post.mockResolvedValue(jsonResponse(task));
    const result = await createTask({ workspace_id: "ws-1", text: "hi" });
    expect(h.apiClientMock.post).toHaveBeenCalledTimes(1);
    const [path, opts] = h.apiClientMock.post.mock.calls[0] as [string, { json: unknown; headers: Record<string, string> }];
    expect(path).toBe("/workspaces/ws-1/tasks");
    expect(opts.json).toEqual({ workspace_id: "ws-1", text: "hi" });
    expect(opts.headers["Content-Type"]).toBe("application/json");
    // trace header 注入
    expect(opts.headers["x-trace-id"]).toMatch(/^[a-f0-9]{32}$/);
    expect(result).toEqual(task);
    expect(recordTrace).toHaveBeenCalledWith(
      expect.objectContaining({ operation: "task_create", method: "POST", path: "/workspaces/ws-1/tasks" }),
    );
  });

  it("HTTP 错误：抛出 ServiceError，且 logError 带 module/path/status_code 上下文", async () => {
    // 测试目的：错误路径归一异常被正确抛出，且 logError 上下文含可排查字段；
    // 可能发现缺陷：错误被吞 / status_code 未写入日志 / 抛出的不是 ServiceError。
    const httpErr = new ServiceError("not found", { statusCode: 404 });
    h.apiClientMock.post.mockRejectedValue(httpErr);

    await expect(createTask({ workspace_id: "ws-1", text: "hi" })).rejects.toBeInstanceOf(ServiceError);
    expect(h.apiClientMock.post).toHaveBeenCalledTimes(1);

    // logError 被调用（错误路径），且上下文含 module/path/status_code。
    expect(logError).toHaveBeenCalledTimes(1);
    const [msg, errArg, ctx] = logError.mock.calls[0] as [string, unknown, Record<string, unknown>];
    expect(msg).toContain("POST");
    expect(errArg).toBe(httpErr);
    expect(ctx.module).toBe("api");
    expect(ctx.method).toBe("POST");
    expect(ctx.path).toBe("/workspaces/ws-1/tasks");
    expect(ctx.status_code).toBe(404);
    // 不校验具体 trace_id 敏感值，仅确认字段存在且为字符串。
    expect(typeof ctx.trace_id).toBe("string");
    expect(ctx.trace_id!.length).toBeGreaterThan(0);
  });
});

describe("api.ts - getTask / listWorkspaces / getBackendHealth（GET）", () => {
  it("getTask：调用 apiClient.get 并返回 .json() 数据", async () => {
    // 测试目的：GET 路径接线正确；可能发现缺陷：方法误用 post/路径拼错。
    const rec = { task_id: "t-9", status: "running" } as never;
    h.apiClientMock.get.mockResolvedValue(jsonResponse(rec));
    const result = await getTask("t-9");
    expect(h.apiClientMock.get).toHaveBeenCalledTimes(1);
    const [path] = h.apiClientMock.get.mock.calls[0] as [string];
    expect(path).toBe("/tasks/t-9");
    expect(result).toEqual(rec);
  });

  it("listWorkspaces：调用 apiClient.get 且返回 data 数组", async () => {
    // 测试目的：GET 列表路径；可能发现缺陷：返回值未解包 .data 或路径错误。
    const list = [{ workspace_id: "ws-a" }] as never;
    h.apiClientMock.get.mockResolvedValue(jsonResponse(list));
    const result = await listWorkspaces();
    expect(h.apiClientMock.get).toHaveBeenCalledTimes(1);
    const [path] = h.apiClientMock.get.mock.calls[0] as [string];
    expect(path).toBe("/workspaces");
    expect(result).toEqual(list);
  });

  it("getBackendHealth：调用 apiClient.get 且返回 data", async () => {
    // 测试目的：健康检查 GET 路径；可能发现缺陷：路径拼错。
    const health = { status: "ok" } as never;
    h.apiClientMock.get.mockResolvedValue(jsonResponse(health));
    const result = await getBackendHealth();
    expect(h.apiClientMock.get).toHaveBeenCalledTimes(1);
    const [path] = h.apiClientMock.get.mock.calls[0] as [string];
    expect(path).toBe("/health");
    expect(result).toEqual(health);
  });

  it("GET HTTP 错误：抛出 ServiceError 且 logError 带 module/path/status_code", async () => {
    // 测试目的：GET 错误路径同样归一并带上下文日志；可能发现缺陷：GET 错误未被归一。
    const httpErr = new ServiceError("forbidden", { statusCode: 403 });
    h.apiClientMock.get.mockRejectedValue(httpErr);
    await expect(getTask("t-9")).rejects.toBeInstanceOf(ServiceError);
    expect(logError).toHaveBeenCalledTimes(1);
    const ctx = logError.mock.calls[0]![2] as Record<string, unknown>;
    expect(ctx.module).toBe("api");
    expect(ctx.method).toBe("GET");
    expect(ctx.path).toBe("/tasks/t-9");
    expect(ctx.status_code).toBe(403);
  });
});

describe("api.ts - deleteTask（DELETE）", () => {
  it("deleteTask：调用 apiClient.delete 并返回（无 body）", async () => {
    // 测试目的：DELETE 路径接线正确；可能发现缺陷：方法误用 / 路径拼错。
    h.apiClientMock.delete.mockResolvedValue(jsonResponse({ deleted: true }));
    await deleteTask("t-9");
    expect(h.apiClientMock.delete).toHaveBeenCalledTimes(1);
    const [path] = h.apiClientMock.delete.mock.calls[0] as [string];
    expect(path).toBe("/tasks/t-9");
    expect(recordTrace).toHaveBeenCalledWith(
      expect.objectContaining({ operation: "task_delete", method: "DELETE", path: "/tasks/t-9" }),
    );
  });

  it("DELETE HTTP 错误：抛出 ServiceError 且 logError 带 module/path/status_code", async () => {
    // 测试目的：DELETE 错误路径归一与日志上下文；可能发现缺陷：DELETE 错误未被归一。
    const httpErr = new ServiceError("gone", { statusCode: 410 });
    h.apiClientMock.delete.mockRejectedValue(httpErr);
    await expect(deleteTask("t-9")).rejects.toBeInstanceOf(ServiceError);
    expect(logError).toHaveBeenCalledTimes(1);
    const ctx = logError.mock.calls[0]![2] as Record<string, unknown>;
    expect(ctx.module).toBe("api");
    expect(ctx.method).toBe("DELETE");
    expect(ctx.path).toBe("/tasks/t-9");
    expect(ctx.status_code).toBe(410);
  });
});

describe("api.ts - SSE 不受影响（仍走原生 fetch）", () => {
  it("connectWorkspaceEventStream 调用 globalThis.fetch 而非 apiClient", async () => {
    // 测试目的：确认 SSE 长连接未改用 ky（ky 不消费 ReadableStream，会破坏 SSE）；
    // 可能发现缺陷：重构后 SSE 误走 apiClient，导致事件流无法按帧解析。
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        controller.enqueue(encoder.encode("event: run_finished\ndata: {}\n\n"));
        controller.close();
      },
    });
    const fetchSpy = vi.fn(async () => ({
      ok: true,
      status: 200,
      statusText: "OK",
      headers: new Headers({ "x-trace-id": "sse-trace-0000" }),
      body: stream,
    })) as unknown as typeof fetch;
    vi.stubGlobal("fetch", fetchSpy);

    // 重置被 mock 模块里对 apiClient 的引用：apiClient 在本测试文件中是 mock 对象，
    // 此处断言 SSE 不调用它的任何方法。
    h.apiClientMock.post.mockClear();
    h.apiClientMock.get.mockClear();
    h.apiClientMock.delete.mockClear();

    const onEvent = vi.fn();
    const disconnect = await connectWorkspaceEventStream("ws-1", onEvent);
    expect(typeof disconnect).toBe("function");

    // 关键断言：SSE 走全局 fetch，没有经过被 mock 的 apiClient 的任意方法。
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/workspaces/ws-1/events/stream");
    expect((init.headers as Record<string, string>)["Accept"]).toBe("text/event-stream");
    expect(h.apiClientMock.post).not.toHaveBeenCalled();
    expect(h.apiClientMock.get).not.toHaveBeenCalled();
    expect(h.apiClientMock.delete).not.toHaveBeenCalled();

    // 验证帧解析仍按 SSE 协议工作（未被 ky 接管导致流被整体消费）。
    await new Promise((r) => setTimeout(r, 10));
    disconnect();
  });

  it("connectWorkspaceEventStream HTTP 非 2xx：抛 ServiceError 且 logError 带 status_code", async () => {
    // 测试目的：SSE 建连失败路径独立错误处理（不依赖 ky 归一）；
    // 可能发现缺陷：SSE 错误未被归一 / 未带 status_code 上下文。
    const fetchSpy = vi.fn(async () => ({
      ok: false,
      status: 500,
      statusText: "Internal Server Error",
      headers: new Headers(),
      body: null,
    })) as unknown as typeof fetch;
    vi.stubGlobal("fetch", fetchSpy);

    await expect(connectWorkspaceEventStream("ws-1", () => {})).rejects.toBeInstanceOf(ServiceError);
    expect(logError).toHaveBeenCalledTimes(1);
    const ctx = logError.mock.calls[0]![2] as Record<string, unknown>;
    expect(ctx.status_code).toBe(500);
    expect(ctx.module).toBe("api");
    expect(ctx.path).toBe("/workspaces/ws-1/events/stream");
  });
});

describe("api.ts - 其余公开 API 函数（复用 post/get/del 接线）", () => {
  it("createWorkspace：POST /workspaces 并写 trace", async () => {
    // 测试目的：POST 封装在更上层公开函数中的接线；可能发现缺陷：路径/body 错误。
    const ws = { workspace_id: "ws-x" } as never;
    h.apiClientMock.post.mockResolvedValue(jsonResponse(ws));
    const result = await createWorkspace({ name: "x" });
    expect(h.apiClientMock.post).toHaveBeenCalledTimes(1);
    const [path, opts] = h.apiClientMock.post.mock.calls[0] as [string, { json: unknown }];
    expect(path).toBe("/workspaces");
    expect(opts.json).toEqual({ name: "x" });
    expect(result).toEqual(ws);
    expect(recordTrace).toHaveBeenCalledWith(
      expect.objectContaining({ operation: "workspace_create", method: "POST", path: "/workspaces" }),
    );
  });

  it("deleteWorkspace：DELETE /workspaces/{id} 并写 trace", async () => {
    // 测试目的：DELETE 封装在公开函数中的接线。
    h.apiClientMock.delete.mockResolvedValue(jsonResponse({ deleted: true }));
    await deleteWorkspace("ws-x");
    const [path] = h.apiClientMock.delete.mock.calls[0] as [string];
    expect(path).toBe("/workspaces/ws-x");
    expect(recordTrace).toHaveBeenCalledWith(
      expect.objectContaining({ operation: "workspace_delete", method: "DELETE", path: "/workspaces/ws-x" }),
    );
  });

  it("prepareWorkspace：POST prepare 路径且 taskId 为 ''", async () => {
    // 测试目的：POST 空 body 路径；可能发现缺陷：body 未传导致 422。
    const resp = { ready: true } as never;
    h.apiClientMock.post.mockResolvedValue(jsonResponse(resp));
    const result = await prepareWorkspace("ws-x");
    const [path, opts] = h.apiClientMock.post.mock.calls[0] as [string, { json: unknown }];
    expect(path).toBe("/workspaces/ws-x/events/prepare");
    expect(opts.json).toEqual({});
    expect(result).toEqual(resp);
  });

  it("listWorkspaceTasks：GET 任务列表路径", async () => {
    // 测试目的：GET 列表 + taskId 上下文透传。
    const list = [{ task_id: "t-1" }] as never;
    h.apiClientMock.get.mockResolvedValue(jsonResponse(list));
    const result = await listWorkspaceTasks("ws-x");
    const [path] = h.apiClientMock.get.mock.calls[0] as [string];
    expect(path).toBe("/workspaces/ws-x/tasks");
    expect(result).toEqual(list);
    expect(recordTrace).toHaveBeenCalledWith(
      expect.objectContaining({ operation: "workspace_tasks", method: "GET", path: "/workspaces/ws-x/tasks" }),
    );
  });

  it("createTaskTurn：POST turns 路径且带 taskId", async () => {
    // 测试目的：POST 带 taskId 上下文；可能发现缺陷：taskId 未透传导致 trace 缺失。
    const turn = { turn_id: "tu-1" } as never;
    h.apiClientMock.post.mockResolvedValue(jsonResponse(turn));
    const result = await createTaskTurn("t-1", { text: "hi" });
    const [path, opts] = h.apiClientMock.post.mock.calls[0] as [string, { json: unknown }];
    expect(path).toBe("/tasks/t-1/turns");
    expect(opts.json).toEqual({ text: "hi" });
    expect(result).toEqual(turn);
  });

  it("listTaskTurns：GET turns 路径", async () => {
    // 测试目的：GET turns 路径接线。
    const list = [{ turn_id: "tu-1" }] as never;
    h.apiClientMock.get.mockResolvedValue(jsonResponse(list));
    const result = await listTaskTurns("t-1");
    const [path] = h.apiClientMock.get.mock.calls[0] as [string];
    expect(path).toBe("/tasks/t-1/turns");
    expect(result).toEqual(list);
  });

  it("fetchChangeSet：GET changes 路径且正确拼接 checkpoint query", async () => {
    // 测试目的：GET query 拼接；可能发现缺陷：checkpoint 未 encode 或丢失。
    const cs = { files: [] } as never;
    h.apiClientMock.get.mockResolvedValue(jsonResponse(cs));
    await fetchChangeSet("t-1", "tu-2");
    const [path] = h.apiClientMock.get.mock.calls[0] as [string];
    expect(path).toBe("/tasks/t-1/changes?checkpoint=tu-2");
  });

  it("fetchChangeSet 无 checkpoint：GET changes 路径无 query", async () => {
    // 测试目的：可选 checkpoint 缺省时不拼接 query；可能发现缺陷：拼出 "?checkpoint=" 空串。
    const cs = { files: [] } as never;
    h.apiClientMock.get.mockResolvedValue(jsonResponse(cs));
    await fetchChangeSet("t-1");
    const [path] = h.apiClientMock.get.mock.calls[0] as [string];
    expect(path).toBe("/tasks/t-1/changes");
  });

  it("keepChanges：POST changes/keep 路径", async () => {
    // 测试目的：POST body 含 paths；可能发现缺陷：paths 未透传。
    const cs = { files: ["a"] } as never;
    h.apiClientMock.post.mockResolvedValue(jsonResponse(cs));
    const result = await keepChanges("t-1", ["a"]);
    const [path, opts] = h.apiClientMock.post.mock.calls[0] as [string, { json: unknown }];
    expect(path).toBe("/tasks/t-1/changes/keep");
    expect(opts.json).toEqual({ paths: ["a"] });
    expect(result).toEqual(cs);
  });

  it("revertChanges：POST changes/revert 路径", async () => {
    // 测试目的：POST body 含 paths（撤销分支）。
    const cs = { files: ["a"] } as never;
    h.apiClientMock.post.mockResolvedValue(jsonResponse(cs));
    const result = await revertChanges("t-1", ["a"]);
    const [path, opts] = h.apiClientMock.post.mock.calls[0] as [string, { json: unknown }];
    expect(path).toBe("/tasks/t-1/changes/revert");
    expect(opts.json).toEqual({ paths: ["a"] });
    expect(result).toEqual(cs);
  });

  it("listTaskEvents：GET events 路径", async () => {
    // 测试目的：GET events 路径接线。
    const list = [{ event_id: "e1" }] as never;
    h.apiClientMock.get.mockResolvedValue(jsonResponse(list));
    const result = await listTaskEvents("t-1");
    const [path] = h.apiClientMock.get.mock.calls[0] as [string];
    expect(path).toBe("/tasks/t-1/events");
    expect(result).toEqual(list);
  });

  it("cancelTurn：POST cancel 路径", async () => {
    // 测试目的：POST cancel 路径；可能发现缺陷：turnId 拼错路径。
    const turn = { turn_id: "tu-1" } as never;
    h.apiClientMock.post.mockResolvedValue(jsonResponse(turn));
    const result = await cancelTurn("tu-1", "t-1");
    const [path, opts] = h.apiClientMock.post.mock.calls[0] as [string, { json: unknown }];
    expect(path).toBe("/turns/tu-1/cancel");
    expect(opts.json).toEqual({});
    expect(result).toEqual(turn);
  });

  it("listAgents：GET agents 路径且返回 data", async () => {
    // 测试目的：GET agents 路径；可能发现缺陷：路径拼错。
    const resp = { agents: [], default_agent_id: "d" } as never;
    h.apiClientMock.get.mockResolvedValue(jsonResponse(resp));
    const result = await listAgents();
    const [path] = h.apiClientMock.get.mock.calls[0] as [string];
    expect(path).toBe("/agents");
    expect(result).toEqual(resp);
  });

  it("H4：prepareWorkspace 透传 timeout:false（禁用前端 30s 超时）", async () => {
    // 测试目的：锁死 H4 修复——首次建索引可达数分钟，prepareWorkspace 必须显式
    //   禁用前端超时，否则 ky 默认 30000ms 会把仍在正常执行的建索引请求误杀，
    //   前端报超时而后端仍在跑，工作区停留在未就绪态。
    // 可能发现缺陷：timeout 未透传 / 传成了 undefined（回落默认 30s）/ 传成数字上限仍不足。
    h.apiClientMock.post.mockResolvedValue(jsonResponse({ ready: true } as never));
    await prepareWorkspace("ws-x");
    const [path, opts] = h.apiClientMock.post.mock.calls[0] as [
      string,
      { json: unknown; timeout?: number | false },
    ];
    expect(path).toBe("/workspaces/ws-x/events/prepare");
    // 关键断言：必须严格等于 false（而非 falsy——0/undefined 都会被 ky 当作「用默认值」或立即超时）。
    expect(opts.timeout).toBe(false);
    expect(opts.timeout).not.toBeUndefined();
  });
});

/**
 * H4 回归护栏：post/get/del 的 `options.timeout` 透传契约。
 *
 * 修复形态：三个内部 helper 新增可选第 N 参数 `options?: { timeout?: number | false }`，
 * 并把 `options?.timeout` 原样传给 `apiClient.post/get/delete`。
 * 语义要求：
 * - 传 `false` → 透传 `false`（禁用前端超时，交由后端护栏控制）。
 * - 传数字 → 透传该数字。
 * - 不传 options → 透传 `undefined`，由 ky 实例默认 30000ms 兜底（向后兼容）。
 */
describe("api.ts - H4：timeout 透传与向后兼容", () => {
  it("不传 options 的 POST（createTask）：timeout 为 undefined（回落 ky 默认 30000ms）", async () => {
    // 测试目的：向后兼容护栏——绝大多数短请求不应受 H4 影响，必须继续走默认超时。
    // 可能发现缺陷：修复把 timeout 硬编码成 false，导致所有请求都失去超时保护，
    //   后端挂死时前端永久 pending、无任何错误反馈。
    h.apiClientMock.post.mockResolvedValue(jsonResponse({ task_id: "t-1" } as never));
    await createTask({ workspace_id: "ws-1", text: "hi" });
    const [, opts] = h.apiClientMock.post.mock.calls[0] as [string, { timeout?: number | false }];
    expect(opts.timeout).toBeUndefined();
    // 显式确认没有被写成 false（false 会禁用超时，是本护栏要防的反向缺陷）。
    expect(opts.timeout).not.toBe(false);
  });

  it("不传 options 的 GET（getTask）：timeout 为 undefined", async () => {
    // 测试目的：GET 分支的向后兼容；可能发现缺陷：get 的 options 参数插错位置
    //   （get 的第 2 参数是 taskId），把 taskId 当作 options 解析导致 timeout 异常。
    h.apiClientMock.get.mockResolvedValue(jsonResponse({ task_id: "t-9" } as never));
    await getTask("t-9");
    const [, opts] = h.apiClientMock.get.mock.calls[0] as [string, { timeout?: number | false }];
    expect(opts.timeout).toBeUndefined();
  });

  it("不传 options 的 DELETE（deleteTask/deleteWorkspace）：timeout 为 undefined", async () => {
    // 测试目的：DELETE 分支的向后兼容；可能发现缺陷：del 新增参数破坏既有调用签名。
    h.apiClientMock.delete.mockResolvedValue(jsonResponse({ deleted: true } as never));
    await deleteTask("t-9");
    const [, delOpts] = h.apiClientMock.delete.mock.calls[0] as [string, { timeout?: number | false }];
    expect(delOpts.timeout).toBeUndefined();

    h.apiClientMock.delete.mockReset();
    h.apiClientMock.delete.mockResolvedValue(jsonResponse({ deleted: true } as never));
    await deleteWorkspace("ws-x");
    const [, wsOpts] = h.apiClientMock.delete.mock.calls[0] as [string, { timeout?: number | false }];
    expect(wsOpts.timeout).toBeUndefined();
  });

  it("timeout 字段始终出现在传给 apiClient 的 options 对象中（key 存在性）", async () => {
    // 测试目的：区分「透传 undefined」与「根本没接线」——若 helper 压根没写 timeout 字段，
    //   上一条 toBeUndefined 断言也会通过（假绿）。此处用 in 操作符锁死接线真实存在。
    // 可能发现缺陷：timeout 透传代码被删除后，undefined 类断言无法察觉（变异测试盲区）。
    h.apiClientMock.post.mockResolvedValue(jsonResponse({} as never));
    await createTask({ workspace_id: "ws-1", text: "hi" });
    const postOpts = (h.apiClientMock.post.mock.calls[0] as [string, Record<string, unknown>])[1];
    expect("timeout" in postOpts).toBe(true);

    h.apiClientMock.get.mockResolvedValue(jsonResponse({} as never));
    await getTask("t-1");
    const getOpts = (h.apiClientMock.get.mock.calls[0] as [string, Record<string, unknown>])[1];
    expect("timeout" in getOpts).toBe(true);

    h.apiClientMock.delete.mockResolvedValue(jsonResponse({} as never));
    await deleteTask("t-1");
    const delOpts = (h.apiClientMock.delete.mock.calls[0] as [string, Record<string, unknown>])[1];
    expect("timeout" in delOpts).toBe(true);
  });

  it("timeout 透传不破坏 headers / json 等既有字段", async () => {
    // 测试目的：新增字段是「叠加」而非「替换」——trace header 与 body 必须仍在。
    // 可能发现缺陷：重构 options 对象时覆盖掉 headers，导致 x-trace-id 丢失、
    //   前后端 trace 断链，日志无法关联。
    h.apiClientMock.post.mockResolvedValue(jsonResponse({ ready: true } as never));
    await prepareWorkspace("ws-x");
    const [, opts] = h.apiClientMock.post.mock.calls[0] as [
      string,
      { json: unknown; headers: Record<string, string>; timeout?: number | false },
    ];
    expect(opts.timeout).toBe(false);
    expect(opts.json).toEqual({});
    expect(opts.headers["Content-Type"]).toBe("application/json");
    expect(opts.headers["x-trace-id"]).toMatch(/^[a-f0-9]{32}$/);
  });

  it("prepareWorkspace 的 taskId 位为 undefined（timeout 未被误传到 taskId 参数位）", async () => {
    // 测试目的：H4 修复在 post 上新增的是第 4 参数；若参数顺序写错（把 {timeout:false}
    //   放到第 3 位 taskId），trace 上下文会被污染且 timeout 完全失效。
    // 可能发现缺陷：参数错位——timeout 落到 taskId 位，前端超时保护形同未改。
    h.apiClientMock.post.mockResolvedValue(jsonResponse({ ready: true } as never));
    await prepareWorkspace("ws-x");
    const [, opts] = h.apiClientMock.post.mock.calls[0] as [string, { timeout?: number | false }];
    // timeout 必须真的落在 ky options 上，而非被吞进 taskId。
    expect(opts.timeout).toBe(false);
    // trace 记录仍以 workspace 维度写入（taskId 为空串），确认第 3 参数未被占用。
    expect(recordTrace).toHaveBeenCalledWith(
      expect.objectContaining({ operation: "workspace_event_prepare", taskId: "" }),
    );
  });

  it("prepareWorkspace 错误路径：仍抛 ServiceError（禁用超时不影响错误归一）", async () => {
    // 测试目的：timeout:false 只关闭前端计时器，不得改变错误处理链路。
    // 可能发现缺陷：为禁用超时而绕过了 try/catch 或 logError 上下文。
    const httpErr = new ServiceError("prepare failed", { statusCode: 500 });
    h.apiClientMock.post.mockRejectedValue(httpErr);
    await expect(prepareWorkspace("ws-x")).rejects.toBeInstanceOf(ServiceError);
    expect(logError).toHaveBeenCalledTimes(1);
    const ctx = logError.mock.calls[0]![2] as Record<string, unknown>;
    expect(ctx.module).toBe("api");
    expect(ctx.method).toBe("POST");
    expect(ctx.path).toBe("/workspaces/ws-x/events/prepare");
    expect(ctx.status_code).toBe(500);
  });
});

describe("api.ts - 其余公开 API 函数（错误透传）", () => {
  it("POST 错误在公开函数中透传：createWorkspace 抛 ServiceError", async () => {
    // 测试目的：错误透传贯穿公开函数（不只测内部 helper）；
    // 可能发现缺陷：某公开函数吞掉错误未向上抛。
    const httpErr = new ServiceError("conflict", { statusCode: 409 });
    h.apiClientMock.post.mockRejectedValue(httpErr);
    await expect(createWorkspace({ name: "x" })).rejects.toBeInstanceOf(ServiceError);
    expect(logError).toHaveBeenCalledTimes(1);
    const ctx = logError.mock.calls[0]![2] as Record<string, unknown>;
    expect(ctx.status_code).toBe(409);
    expect(ctx.path).toBe("/workspaces");
  });
});
