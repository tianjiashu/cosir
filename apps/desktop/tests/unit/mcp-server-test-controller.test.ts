import { describe, expect, it, vi } from "vitest";

import { MCPServerTestController, type MCPServerTestApi } from "@/lib/mcp/server-test-controller";
import type { MCPServerConfiguration, MCPServerTestResult } from "@/lib/api/extensions";

const config = (name: string): MCPServerConfiguration => ({
  name, transport: "streamable_http", command: "", args: [], env: {}, url: `https://${name}.example/mcp`, headers: {}, oauth: null, enabled: true,
});

const result = (success: boolean, extra: Partial<MCPServerTestResult> = {}): MCPServerTestResult => ({
  status: "completed", success, tools: [], error: success ? null : "boom", failure_kind: null, agent_available: true, ...extra,
});

/** 可以手动结算的 Promise：用来模拟「一个服务还在测、另一个已经出结果」。 */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function controllerWith(overrides: Partial<MCPServerTestApi>) {
  const api: MCPServerTestApi = {
    test: vi.fn(async () => result(true)),
    authorize: vi.fn(async () => result(true)),
    wait: vi.fn(async () => result(true)),
    cancel: vi.fn(async () => ({})),
    openBrowser: vi.fn(),
    onSettled: vi.fn(),
    ...overrides,
  };
  return { controller: new MCPServerTestController(api), api };
}

describe("MCPServerTestController", () => {
  // 潜在缺陷：一个服务在测试（或等授权）时，其他服务全被锁住。
  it("tests servers independently so one slow server never blocks another", async () => {
    const slow = deferred<MCPServerTestResult>();
    const { controller } = controllerWith({
      test: vi.fn((server: MCPServerConfiguration) => (server.name === "slow" ? slow.promise : Promise.resolve(result(true)))),
    });

    const slowRun = controller.test("server:slow", config("slow"));
    await controller.test("server:fast", config("fast"));

    expect(controller.getSnapshot()["server:slow"]).toEqual({ phase: "testing" });
    expect(controller.getSnapshot()["server:fast"]).toEqual({ phase: "done", result: result(true) });
    slow.resolve(result(false));
    await slowRun;
    expect(controller.getSnapshot()["server:slow"]).toEqual({ phase: "done", result: result(false) });
  });

  it("tests a batch in parallel and refreshes the list after each result", async () => {
    const { controller, api } = controllerWith({});

    await controller.testAll([{ key: "server:a", config: config("a") }, { key: "server:b", config: config("b") }]);

    expect(api.test).toHaveBeenCalledTimes(2);
    expect(api.onSettled).toHaveBeenCalledTimes(2);
  });

  it("opens the browser only after the user asks to sign in, then reports the outcome", async () => {
    const { controller, api } = controllerWith({
      authorize: vi.fn(async () => ({ status: "authorization_required" as const, flow_id: "f1", authorization_url: "https://mcp.linear.app/authorize?x=1" })),
    });

    await controller.authorize("server:linear", config("linear"));

    expect(api.openBrowser).toHaveBeenCalledWith("https://mcp.linear.app/authorize?x=1");
    expect(api.wait).toHaveBeenCalledWith("f1");
    expect(controller.getSnapshot()["server:linear"]).toEqual({ phase: "done", result: result(true) });
  });

  // 潜在缺陷：放弃授权后界面一直转圈，或者取消之后才失败返回的等待请求把状态改回失败原因。
  it("cancelling frees the card at once and ignores the late failing wait", async () => {
    const waiting = deferred<MCPServerTestResult>();
    const { controller, api } = controllerWith({
      authorize: vi.fn(async () => ({ status: "authorization_required" as const, flow_id: "f1", authorization_url: "https://mcp.linear.app/authorize" })),
      wait: vi.fn(() => waiting.promise),
    });

    const run = controller.authorize("server:linear", config("linear"));
    await vi.waitFor(() => expect(controller.getSnapshot()["server:linear"]).toMatchObject({ phase: "authorizing", flowId: "f1", host: "mcp.linear.app" }));
    await controller.cancel("server:linear");
    waiting.reject(new Error("404"));
    await run;

    expect(api.cancel).toHaveBeenCalledWith("f1");
    const state = controller.getSnapshot()["server:linear"];
    expect(state?.phase === "done" && state.result.error).toBe("已取消授权");
  });

  // 潜在缺陷：关掉或重开编辑框只丢了前端进展，后端的回调监听一直挂到超时。
  it("clearing an authorizing key ends the backend authorization", async () => {
    const waiting = deferred<MCPServerTestResult>();
    const { controller, api } = controllerWith({
      authorize: vi.fn(async () => ({ status: "authorization_required" as const, flow_id: "f1", authorization_url: "https://mcp.linear.app/authorize" })),
      wait: vi.fn(() => waiting.promise),
    });

    const run = controller.authorize("draft", config("draft"));
    await vi.waitFor(() => expect(controller.getSnapshot()["draft"]).toMatchObject({ phase: "authorizing", flowId: "f1" }));
    controller.clear("draft");
    waiting.reject(new Error("cancelled"));
    await run;

    expect(api.cancel).toHaveBeenCalledWith("f1");
    expect(controller.getSnapshot()["draft"]).toBeUndefined();
  });

  // 潜在缺陷：授权地址还没回来就取消了，地址迟到时既没人结束它，也不该再弹浏览器。
  it("ends an authorization whose address arrives after it was abandoned, without opening the browser", async () => {
    const started = deferred<{ status: "authorization_required"; flow_id: string; authorization_url: string }>();
    const { controller, api } = controllerWith({ authorize: vi.fn(() => started.promise) });

    const run = controller.authorize("server:linear", config("linear"));
    controller.cancel("server:linear");
    started.resolve({ status: "authorization_required", flow_id: "late", authorization_url: "https://mcp.linear.app/authorize" });
    await run;

    expect(api.cancel).toHaveBeenCalledWith("late");
    expect(api.openBrowser).not.toHaveBeenCalled();
    const state = controller.getSnapshot()["server:linear"];
    expect(state?.phase === "done" && state.result.error).toBe("已取消授权");
  });

  it("starting over on the same key ends the authorization it replaces", async () => {
    const { controller, api } = controllerWith({
      authorize: vi.fn(async () => ({ status: "authorization_required" as const, flow_id: "f1", authorization_url: "https://mcp.linear.app/authorize" })),
      wait: vi.fn(() => new Promise<MCPServerTestResult>(() => {})),
    });

    void controller.authorize("draft", config("draft"));
    await vi.waitFor(() => expect(controller.getSnapshot()["draft"]).toMatchObject({ flowId: "f1" }));
    void controller.test("draft", config("draft"));

    expect(api.cancel).toHaveBeenCalledWith("f1");
  });

  it("turns request failures into a failed result instead of leaving the card spinning", async () => {
    const { controller } = controllerWith({ test: vi.fn(async () => { throw new Error("network down"); }) });

    await controller.test("server:a", config("a"));

    const state = controller.getSnapshot()["server:a"];
    expect(state?.phase === "done" && state.result.success).toBe(false);
  });

  it("clearing a key drops a result that arrives afterwards", async () => {
    const pending = deferred<MCPServerTestResult>();
    const { controller } = controllerWith({ test: vi.fn(() => pending.promise) });

    const run = controller.test("draft", config("draft"));
    controller.clear("draft");
    pending.resolve(result(true));
    await run;

    expect(controller.getSnapshot()["draft"]).toBeUndefined();
  });
});
