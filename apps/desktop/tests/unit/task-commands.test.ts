import { afterEach, describe, expect, it, vi } from "vitest";

import {
  editQueuedTaskMessage,
  rerunTaskMessage,
  resumeTask,
  sendTaskMessage,
  submitTeamDecision,
  type TaskMessageRequest,
} from "@/lib/assistant/task-commands";
import { HttpError } from "@/lib/http/errors";

const REQUEST: TaskMessageRequest = {
  message: { role: "user", parts: [{ type: "text", text: "hello" }] },
  modelConfigId: 1,
  reasoningEffort: "high",
  banTools: ["read_file"],
};

/** 后端处理完这一轮返回 200 `{}`，输出经 Task 长订阅到达。 */
function accepted(status = 200) {
  return vi.fn().mockImplementation(async () => new Response("{}", { status }));
}

function lastCall(): [string, RequestInit] {
  return vi.mocked(fetch).mock.calls.at(-1) as [string, RequestInit];
}

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

    await submitTeamDecision(7, { teamRunId: 17, decision: "approve", data: { goal: "交付" } });
    expect(lastCall()[0]).toMatch(/\/tasks\/7\/decisions$/);
    expect(JSON.parse(String(lastCall()[1].body))).toEqual({ teamRunId: 17, decision: "approve", data: { goal: "交付" } });
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

    const error = await submitTeamDecision(7, { teamRunId: 17, decision: "reject" }).catch((cause: unknown) => cause);
    expect(error).toBeInstanceOf(HttpError);
    expect((error as HttpError).status).toBe(404);
    expect((error as HttpError).message).toBe("任务不存在");
  });
});
