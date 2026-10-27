import { describe, expect, it } from "vitest";

import { parseTransportState, TransportSnapshotValidationError } from "@/lib/assistant/snapshot-validation";
import type { TransportMessagePart } from "@/lib/assistant/contract";
import {
  activity,
  assistantMessage,
  outcome,
  queuedMessage,
  transportState,
  userMessage,
} from "./transport-fixtures";

const withToolPart = (part: Record<string, unknown>) => transportState({
  messages: [assistantMessage("assistant-run-1", 1, [part as unknown as TransportMessagePart])],
  activity: activity(1, "running"),
});

describe("Transport snapshot validation", () => {
  it("returns the complete server snapshot without creating fields", () => {
    const snapshot = transportState();
    expect(parseTransportState(snapshot)).toBe(snapshot);
  });

  it("Task 标题是必填的非空字符串", () => {
    const withoutTitle: Record<string, unknown> = { ...transportState() };
    delete withoutTitle.title;
    expect(() => parseTransportState(withoutTitle)).toThrow(TransportSnapshotValidationError);
    expect(() => parseTransportState(transportState({ title: "  " }))).toThrow(TransportSnapshotValidationError);
  });

  it("接受 Task 视角的完整 snapshot：消息、结局、活动与排队消息", () => {
    const snapshot = transportState({
      messages: [userMessage("1", 1), assistantMessage("assistant-1", 1, undefined, outcome("completed"))],
      activity: activity(1, "completed", { endReason: "stop", contextWindowTotal: 128_000 }),
      queued_messages: [queuedMessage("2")],
    });
    expect(parseTransportState(snapshot)).toBe(snapshot);
  });

  it.each(["runs", "current_run_id", "approvals", "context_window_total"])("拒绝已删除的 Run 视角字段 %s", (field) => {
    expect(() => parseTransportState({ ...transportState(), [field]: [] })).toThrow(TransportSnapshotValidationError);
  });

  it("rejects a missing messages field instead of treating it as an empty conversation", () => {
    const snapshot = transportState() as Partial<ReturnType<typeof transportState>>;
    delete snapshot.messages;
    expect(() => parseTransportState(snapshot)).toThrow("snapshot");
  });

  it("消息必须携带 runId 与 outcome 两个字段", () => {
    const message = { id: "1", role: "user", parts: [] };
    expect(() => parseTransportState({ ...transportState(), messages: [message] })).toThrow("messages[0]");
  });

  it("outcome 只允许出现在助手消息上，且状态必须是终态", () => {
    const onUser = transportState({ messages: [{ ...userMessage("1", 1), outcome: outcome("completed") }] });
    expect(() => parseTransportState(onUser)).toThrow("messages[0].outcome");
    const running = transportState({
      messages: [assistantMessage("assistant-run-1", 1, [], { ...outcome("completed"), status: "running" as never })],
    });
    expect(() => parseTransportState(running)).toThrow("messages[0].outcome.status");
  });

  it.each(["idle", "pending", "interrupted"])("活动状态收敛到后端五个值，拒绝 %s", (status) => {
    const snapshot = transportState({ activity: activity(1, status as never) });
    expect(() => parseTransportState(snapshot)).toThrow("activity.status");
  });

  it.each([null, 0, 128_000])("接受活动窗口值 %s，保持原快照不补字段", (contextWindowTotal) => {
    const snapshot = transportState({ activity: activity(1, "running", { contextWindowTotal }) });
    expect(parseTransportState(snapshot)).toBe(snapshot);
  });

  it.each([undefined, -1, 0.5, "128000", true, NaN, Infinity])("拒绝非法活动窗口值 %s", (contextWindowTotal) => {
    const snapshot = transportState({ activity: activity(1, "running", { contextWindowTotal: contextWindowTotal as never }) });
    expect(() => parseTransportState(snapshot)).toThrow("activity");
  });

  it("消息 id 在消息与排队消息之间唯一", () => {
    const snapshot = transportState({ messages: [userMessage("1", 1)], queued_messages: [queuedMessage("1")] });
    expect(() => parseTransportState(snapshot)).toThrow("queued_messages[0].id");
  });

  it("排队消息只携带 id、role、parts、delivery", () => {
    const snapshot = transportState({ queued_messages: [{ ...queuedMessage("2"), runId: 1 } as never] });
    expect(() => parseTransportState(snapshot)).toThrow("queued_messages[0]");
  });

  it("rejects invalid tool lifecycle state with a diagnostic path", () => {
    const snapshot = withToolPart({ type: "tool-call", toolCallId: "t1", toolName: "read_file", status: "future", args: {} });
    expect(() => parseTransportState(snapshot)).toThrow("messages[0].parts[0].status");
  });

  it("accepts a positive child Run locator and rejects invalid locators", () => {
    const part = { type: "tool-call", toolCallId: "delegate-1", toolName: "delegate_task", status: "running", args: {}, child_task_id: 501 };
    const snapshot = withToolPart(part);
    expect(parseTransportState(snapshot)).toBe(snapshot);
    expect(() => parseTransportState(withToolPart({ ...part, child_task_id: 0 }))).toThrow("child_task_id");
  });

  it("accepts the minimal child session display contract", () => {
    const snapshot = withToolPart({
      type: "tool-call", toolCallId: "delegate-1", toolName: "delegate_task", status: "completed", args: {},
      display_data: { kind: "delegation-result", title: "审查代码", role: "Reviewer", child_task_id: 501, status: "completed" },
    });
    expect(parseTransportState(snapshot)).toBe(snapshot);
  });

  it("accepts the child agent send/status result contract", () => {
    const snapshot = withToolPart({
      type: "tool-call", toolCallId: "status-1", toolName: "child_agent_status", status: "completed", args: {},
      display_data: {
        kind: "child-agent-result", operation: "status", child_task_id: 501, status: "completed",
        agent_id: "reviewer", agent_name: "审查代码", final_output: "已完成审查。", end_reason: null,
      },
    });
    expect(parseTransportState(snapshot)).toBe(snapshot);
  });

  it("rejects child output embedded in a parent delegation display", () => {
    const snapshot = withToolPart({
      type: "tool-call", toolCallId: "delegate-1", toolName: "delegate_task", status: "completed", args: {},
      display_data: { kind: "delegation-result", title: "审查代码", child_task_id: 501, status: "completed", final_output: "不应进入父级委派卡片" },
    });
    expect(() => parseTransportState(snapshot)).toThrow("已知字段");
  });

  it("rejects a top-level command error: command rejections are returned by HTTP, not the snapshot", () => {
    const snapshot = { ...transportState(), error: { code: "RUN_NOT_RESUMABLE", message: "x" } };
    expect(() => parseTransportState(snapshot)).toThrow("snapshot");
  });

  it("accepts a controlled outcome error payload and rejects one missing its message", () => {
    const failed = outcome("failed", {
      endReason: "model_insufficient_quota",
      error: { code: "model_insufficient_quota", message: "模型服务配额或余额不足，请充值或更换模型" },
    });
    const snapshot = transportState({ messages: [assistantMessage("assistant-run-1", 1, [], failed)] });
    expect(parseTransportState(snapshot)).toBe(snapshot);
    const broken = transportState({
      messages: [assistantMessage("assistant-run-1", 1, [], { ...failed, error: { code: "x" } as never })],
    });
    expect(() => parseTransportState(broken)).toThrow("messages[0].outcome.error");
  });

  it("rejects an activity without the error field instead of tolerating a partial snapshot", () => {
    const partial = { ...activity(1, "running") } as Partial<ReturnType<typeof activity>>;
    delete partial.error;
    expect(() => parseTransportState(transportState({ activity: partial as never }))).toThrow("activity");
  });
});
