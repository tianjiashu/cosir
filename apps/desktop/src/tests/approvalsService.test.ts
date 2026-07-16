import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchPendingApprovals, submitApprovalDecision } from "@/services/approvals";
import { useClientTraceStore } from "@/stores/clientTraceStore";
import { useConversationTraceStore } from "@/stores/conversationTraceStore";
import { beginClientTrace, endClientTrace } from "@/services/tracePropagation";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  useClientTraceStore.getState().reset();
  useConversationTraceStore.getState().resetConversationTraces();
});

describe("approvals.ts — 审批服务 trace 记录", () => {
  it("fetchPendingApprovals 记录 task_approvals trace", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, status: 200, json: async () => [] }) as unknown as Response),
    );

    await fetchPendingApprovals("task-1");

    expect(useConversationTraceStore.getState().latestTraceByTaskId["task-1"]).toMatchObject({
      taskId: "task-1",
      operation: "task_approvals",
      method: "GET",
      path: "/tasks/task-1/approvals",
    });
  });

  it("submitApprovalDecision 在当前 task trace 中记录 approval_decision", async () => {
    beginClientTrace({ taskId: "task-1" });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        ({
          ok: true,
          status: 200,
          json: async () => ({
            decision_id: "decision-1",
            approval_id: "approval-1",
            decision: "approved",
            reason: null,
            decided_at: "2026-01-01T00:00:00Z",
            idempotency_key: "key-1",
          }),
        }) as unknown as Response,
      ),
    );

    await submitApprovalDecision("approval-1", {
      decision: "approved",
      idempotency_key: "key-1",
    });
    endClientTrace();

    expect(useConversationTraceStore.getState().latestTraceByTaskId["task-1"]).toMatchObject({
      taskId: "task-1",
      approvalId: "approval-1",
      operation: "approval_decision",
      method: "POST",
      path: "/approvals/approval-1/decision",
    });
  });
});
