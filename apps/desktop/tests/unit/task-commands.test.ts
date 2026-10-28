import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logging/frontend-log", () => ({ frontendLog: vi.fn().mockResolvedValue(undefined) }));

import {
  backgroundToolCall,
  cancelTask,
  cancelToolCall,
  editQueuedTaskMessage,
  rerunTaskMessage,
  resumeTask,
  sendTaskMessage,
  submitToolDecision,
  type TaskMessageRequest,
} from "@/lib/assistant/task-commands";
import { HttpError } from "@/lib/http/errors";

const REQUEST: TaskMessageRequest = {
  message: { role: "user", parts: [{ type: "text", text: "hello" }] },
  modelConfigId: 1,
  reasoningEffort: "high",
  disabledToolGroups: ["文件"],
};

/** 后端处理完这一轮返回 200 `{}`，输出经 Task 长订阅到达。 */
function accepted(status = 200) {
  return vi.fn().mockImplementation(async () => new Response("{}", { status }));
}

function lastCall(): [string, RequestInit] {
  return vi.mocked(fetch).mock.calls.at(-1) as [string, RequestInit];
}

// http client 按 runtime config 解析后端地址，它读 window；单测跑在 Node 环境，给一个空 window。
beforeEach(() => vi.stubGlobal("window", {}));
afterEach(() => vi.unstubAllGlobals());

describe("task commands", () => {
  it("发送消息只按 Task 寻址，携带投递方式而不携带 runId", async () => {
    vi.stubGlobal("fetch", accepted());

    await expect(sendTaskMessage(7, { ...REQUEST, delivery: "next_run" })).resolves.toBeUndefined();

    const [url, init] = lastCall();
    expect(url).toMatch(/\/tasks\/7\/messages$/);
    expect(JSON.parse(String(init.body))).toEqual({ ...REQUEST, delivery: "next_run" });
  });

  it("编辑排队消息用 PATCH 按消息寻址，只带正文，不带发起配置", async () => {
    vi.stubGlobal("fetch", accepted(200));

    await expect(editQueuedTaskMessage(7, "msg/1", REQUEST.message)).resolves.toBeUndefined();

    const [url, init] = lastCall();
    expect(url).toMatch(/\/tasks\/7\/queued-messages\/msg%2F1$/);
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(String(init.body))).toEqual({ message: REQUEST.message });
  });

  it("编辑重跑按消息寻址，携带发起配置但不含投递方式", async () => {
    vi.stubGlobal("fetch", accepted());

    await rerunTaskMessage(7, "msg/1", REQUEST);

    const [url, init] = lastCall();
    expect(url).toMatch(/\/tasks\/7\/messages\/msg%2F1\/rerun$/);
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual(REQUEST);
  });

  it("恢复不带请求体，决定使用后端 wire 字段", async () => {
    vi.stubGlobal("fetch", accepted());

    await resumeTask(7);
    expect(lastCall()[0]).toMatch(/\/tasks\/7\/resume$/);
    expect(lastCall()[1].body).toBeUndefined();

    await submitToolDecision(7, "call/1", { decision: "approve", data: { goal: "交付" } });
    expect(lastCall()[0]).toMatch(/\/tasks\/7\/tool-calls\/call%2F1\/decision$/);
    expect(JSON.parse(String(lastCall()[1].body))).toEqual({ decision: "approve", data: { goal: "交付" } });
  });

  it("编辑排队消息时消息已并入：409 MESSAGE_CONSUMED 原样抛出", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: { error: { code: "MESSAGE_CONSUMED", message: "消息已被处理", retryable: false } } }),
      { status: 409, headers: { "Content-Type": "application/json" } },
    )));
    const error = await editQueuedTaskMessage(7, "1", REQUEST.message).catch((cause: unknown) => cause);
    expect(error).toBeInstanceOf(HttpError);
    expect((error as HttpError).code).toBe("MESSAGE_CONSUMED");
  });

  it("非 2xx 抛出带后端原因的 HttpError", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: { error: { code: "TASK_NOT_FOUND", message: "任务不存在", retryable: false } } }),
      { status: 404, headers: { "Content-Type": "application/json" } },
    )));

    const error = await submitToolDecision(7, "call-1", { decision: "reject", feedback: "换个分工" })
      .catch((cause: unknown) => cause);
    expect(error).toBeInstanceOf(HttpError);
    expect((error as HttpError).status).toBe(404);
    expect((error as HttpError).message).toBe("任务不存在");
  });
});

/** 停止与工具调用命令幂等：失败收敛成结果对象，文案用后端给出的可读原因，不露裸 HTTP 状态码。 */
describe("idempotent task commands", () => {
  const backendError = (status: number, message: string) =>
    new Response(JSON.stringify({ detail: { error: { code: "X", message, retryable: false } } }), { status });

  it("停止 Task 按 Task 寻址，202 视为已受理", async () => {
    const fetchMock = accepted(202);
    vi.stubGlobal("fetch", fetchMock);
    await expect(cancelTask(7)).resolves.toEqual({ accepted: true });
    expect(String(fetchMock.mock.calls[0][0])).toMatch(/\/tasks\/7\/cancel$/);
  });

  it("停止被拒时展示后端原因，网络失败归为 network", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(backendError(404, "任务不存在")));
    await expect(cancelTask(7)).resolves.toEqual({ accepted: false, reason: "rejected", message: "任务不存在" });

    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    await expect(cancelTask(7)).resolves.toMatchObject({ accepted: false, reason: "network" });
  });

  it("取消工具调用按 tool_call_id 寻址，409 视为已发过信号", async () => {
    const fetchMock = accepted();
    vi.stubGlobal("fetch", fetchMock);
    await expect(cancelToolCall(12, "call/1")).resolves.toEqual({ kind: "signalled" });
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/tasks\/12\/tool-calls\/call%2F1\/cancel$/),
      expect.objectContaining({
        method: "POST",
        cache: "no-store",
        headers: expect.objectContaining({ "X-Trace-Id": expect.any(String) }),
      }),
    );

    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 409 })));
    await expect(cancelToolCall(12, "call-1")).resolves.toEqual({ kind: "already_signalled" });
  });

  it("取消工具调用失败时不声称已取消", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 500 })));
    await expect(cancelToolCall(12, "call-1")).resolves.toMatchObject({
      kind: "failed",
      reason: "rejected",
      message: "本机后端处理失败，请查看后端日志",
    });

    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    await expect(cancelToolCall(12, "call-1")).resolves.toMatchObject({ kind: "failed", reason: "network" });
  });

  it("转到后台：409 表示不可转，失败带可读原因", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 409 })));
    await expect(backgroundToolCall(12, "call-1")).resolves.toEqual({ kind: "not_detachable" });

    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(backendError(404, "任务不存在")));
    await expect(backgroundToolCall(12, "call-1")).resolves.toEqual({ kind: "failed", message: "任务不存在" });
  });
});
