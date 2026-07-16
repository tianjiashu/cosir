import { afterEach, describe, expect, it, vi } from "vitest";
import { cancelRunTask, fetchRecoverableRuns } from "@/services/runs";
import { useClientTraceStore } from "@/stores/clientTraceStore";
import { useConversationTraceStore } from "@/stores/conversationTraceStore";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  useClientTraceStore.getState().reset();
  useConversationTraceStore.getState().resetConversationTraces();
});

describe("runs.ts — 恢复服务 trace 记录", () => {
  it("fetchRecoverableRuns 记录 recoverable_runs trace", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, status: 200, json: async () => [] }) as unknown as Response),
    );

    await fetchRecoverableRuns();

    expect(useConversationTraceStore.getState().latestTrace).toMatchObject({
      taskId: "",
      operation: "recoverable_runs",
      method: "GET",
      path: "/runs/recoverable",
    });
  });

  it("cancelRunTask 记录 task_cancel trace", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ task_id: "task-1" }) }) as unknown as Response),
    );

    await cancelRunTask("task-1");

    expect(useConversationTraceStore.getState().latestTraceByTaskId["task-1"]).toMatchObject({
      taskId: "task-1",
      operation: "task_cancel",
      method: "POST",
      path: "/tasks/task-1/cancel",
    });
  });
});
