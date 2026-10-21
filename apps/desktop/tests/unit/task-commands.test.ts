import { afterEach, describe, expect, it, vi } from "vitest";

import type { TransportState } from "@/lib/assistant/contract";
import {
  editTaskMessage,
  resumeTask,
  sendTaskMessage,
  submitTaskDecision,
  type TaskMessageRequest,
} from "@/lib/assistant/task-commands";
import { HttpError } from "@/lib/http/errors";

const STATE: TransportState = {
  runs: [{ runId: 4, status: "running", endReason: null, contextWindowTotal: null, messages: [], usage: null, error: null }],
  current_run_id: 4,
  approvals: {},
  error: null,
};

const REQUEST: TaskMessageRequest = {
  message: { role: "user", parts: [{ type: "text", text: "hello" }] },
  modelConfigId: 1,
  reasoningEffort: "high",
  banTools: ["read_file"],
};

function accepted(queued = false) {
  return vi.fn().mockImplementation(async () => new Response(JSON.stringify({ state: STATE, queued }), { status: 200 }));
}

function lastCall(): [string, RequestInit] {
  return vi.mocked(fetch).mock.calls.at(-1) as [string, RequestInit];
}

afterEach(() => vi.unstubAllGlobals());

describe("task commands", () => {
  it("发送消息只按 Task 寻址，携带投递方式而不携带 runId", async () => {
    vi.stubGlobal("fetch", accepted(true));

    await expect(sendTaskMessage(7, { ...REQUEST, delivery: "after_run" })).resolves.toEqual({ state: STATE, queued: true });

    const [url, init] = lastCall();
    expect(url).toMatch(/\/tasks\/7\/messages$/);
    expect(JSON.parse(String(init.body))).toEqual({ ...REQUEST, delivery: "after_run" });
  });

  it("编辑按消息寻址，请求体不含投递方式", async () => {
    vi.stubGlobal("fetch", accepted());

    await editTaskMessage(7, "msg/1", REQUEST);

    const [url, init] = lastCall();
    expect(url).toMatch(/\/tasks\/7\/messages\/msg%2F1\/edit$/);
    expect(JSON.parse(String(init.body))).toEqual(REQUEST);
  });

  it("恢复不带请求体，决定使用后端 wire 字段", async () => {
    vi.stubGlobal("fetch", accepted());

    await resumeTask(7);
    expect(lastCall()[0]).toMatch(/\/tasks\/7\/resume$/);
    expect(lastCall()[1].body).toBeUndefined();

    await submitTaskDecision(7, { requestId: "17", decision: "approve", data: { goal: "交付" } });
    expect(lastCall()[0]).toMatch(/\/tasks\/7\/decisions$/);
    expect(JSON.parse(String(lastCall()[1].body))).toEqual({ requestId: "17", decision: "approve", data: { goal: "交付" } });
  });

  it("非 2xx 抛出带后端原因的 HttpError", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ error: { code: "RUN_NOT_WAITING", message: "当前没有等待中的审批", retryable: true } }),
      { status: 409, headers: { "Content-Type": "application/json" } },
    )));

    const error = await submitTaskDecision(7, { requestId: "17", decision: "reject" }).catch((cause: unknown) => cause);
    expect(error).toBeInstanceOf(HttpError);
    expect((error as HttpError).status).toBe(409);
    expect((error as HttpError).message).toBe("当前没有等待中的审批");
  });
});
