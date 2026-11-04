import { describe, expect, it } from "vitest";

import { parseHistoryPage, parseTransportState, TransportSnapshotValidationError } from "@/lib/assistant/snapshot-validation";
import type { TransportMessagePart } from "@/lib/assistant/contract";
import {
  activity,
  agentMessage,
  assistantMessage,
  outcome,
  queuedMessage,
  terminalSession,
  transportState,
  userMessage,
} from "./transport-fixtures";

/** 去掉一个必填字段，模拟漂移的后端快照。 */
function without(value: object, key: string): Record<string, unknown> {
  const copy: Record<string, unknown> = { ...value };
  delete copy[key];
  return copy;
}

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

  it("terminal_sessions 是必填数组，条目按固定字段校验", () => {
    const withoutSessions: Record<string, unknown> = { ...transportState() };
    delete withoutSessions.terminal_sessions;
    expect(() => parseTransportState(withoutSessions)).toThrow(TransportSnapshotValidationError);

    const snapshot = transportState({
      terminal_sessions: [
        terminalSession("t1", { createdBy: "agent", label: "dev server" }),
        terminalSession("t2", { status: "exited", exitCode: 0, endReason: "shell_exited", integration: "unavailable" }),
      ],
    });
    expect(parseTransportState(snapshot)).toBe(snapshot);
  });

  it("拒绝重复会话、未知状态与多余字段", () => {
    expect(() => parseTransportState(transportState({ terminal_sessions: [terminalSession("t1"), terminalSession("t1")] })))
      .toThrow(/terminal_sessions\[1\]\.sessionId/);
    expect(() => parseTransportState(transportState({
      terminal_sessions: [{ ...terminalSession("t1"), status: "interrupted" } as never],
    }))).toThrow(/terminal_sessions\[0\]\.status/);
    expect(() => parseTransportState(transportState({
      terminal_sessions: [{ ...terminalSession("t1"), generation: "g1" } as never],
    }))).toThrow(TransportSnapshotValidationError);
    // 交接不在会话条目上，在顶层 `handoffs`。
    expect(() => parseTransportState(transportState({
      terminal_sessions: [{ ...terminalSession("t1"), handoff: null } as never],
    }))).toThrow(TransportSnapshotValidationError);
  });

  it("handoffs 是必填数组：字段固定、资源类型受控、调用与资源各自唯一", () => {
    const withoutHandoffs: Record<string, unknown> = { ...transportState() };
    delete withoutHandoffs.handoffs;
    expect(() => parseTransportState(withoutHandoffs)).toThrow(/snapshot/);

    const entry = { toolCallId: "c1", toolName: "browser_handoff", surface: "browser" as const, resourceId: "t1", reason: "登录" };
    const snapshot = transportState({ handoffs: [entry, { ...entry, toolCallId: "c2", surface: "terminal", resourceId: "t1" }] });
    expect(parseTransportState(snapshot)).toBe(snapshot);

    expect(() => parseTransportState(transportState({ handoffs: [{ ...entry, surface: "editor" } as never] }))).toThrow(/handoffs\[0\]\.surface/);
    expect(() => parseTransportState(transportState({ handoffs: [entry, { ...entry, toolCallId: "c2" }] }))).toThrow(/handoffs\[1\]/);
    expect(() => parseTransportState(transportState({ handoffs: [{ ...entry, extra: 1 } as never] }))).toThrow(TransportSnapshotValidationError);
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

  it("source 只允许出现在 user 消息上，字段固定", () => {
    const agent = transportState({ messages: [agentMessage("1", 1)] });
    expect(parseTransportState(agent)).toBe(agent);
    const onAssistant = transportState({
      messages: [{ ...assistantMessage("assistant-1", 1), source: { taskId: 1, name: "x" } }],
    });
    expect(() => parseTransportState(onAssistant)).toThrow("messages[0].source");
    const extra = transportState({ messages: [{ ...agentMessage("1", 1), source: { taskId: 1, name: "x", kind: "agent" } as never }] });
    expect(() => parseTransportState(extra)).toThrow("messages[0].source");
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
    const snapshot = withToolPart({ type: "tool-call", awaitingUser: false, toolCallId: "t1", toolName: "read_file", status: "future", args: {} });
    expect(() => parseTransportState(snapshot)).toThrow("messages[0].parts[0].status");
  });

  it("accepts a positive child Run locator and rejects invalid locators", () => {
    const part = { type: "tool-call", awaitingUser: false, toolCallId: "delegate-1", toolName: "delegate_task", status: "running", args: {}, child_task_id: 501 };
    const snapshot = withToolPart(part);
    expect(parseTransportState(snapshot)).toBe(snapshot);
    expect(() => parseTransportState(withToolPart({ ...part, child_task_id: 0 }))).toThrow("child_task_id");
  });

  it("接受后端 execute_terminal 流式输出的去重序号 terminal_output_seq", () => {
    const part = { type: "tool-call", awaitingUser: false, toolCallId: "exec-1", toolName: "execute_terminal", status: "running", args: {}, terminal_output_seq: 3 };
    const snapshot = withToolPart(part);
    expect(parseTransportState(snapshot)).toBe(snapshot);
    expect(() => parseTransportState(withToolPart({ ...part, terminal_output_seq: -1 }))).toThrow("terminal_output_seq");
  });

  it("accepts the minimal child session display contract", () => {
    const snapshot = withToolPart({
      type: "tool-call", awaitingUser: false, toolCallId: "delegate-1", toolName: "delegate_task", status: "completed", args: {},
      display_data: { kind: "delegation-result", title: "审查代码", role: "Reviewer", child_task_id: 501, status: "completed" },
    });
    expect(parseTransportState(snapshot)).toBe(snapshot);
  });

  it("accepts the send_message / task_status result contract", () => {
    const part = (display_data: Record<string, unknown>) => withToolPart({
      type: "tool-call", awaitingUser: false, toolCallId: "status-1", toolName: "task_status", status: "completed", args: {}, display_data,
    });
    const snapshot = part({
      kind: "agent-task-result", operation: "status", task_id: 501,
      agent_name: "审查代码", final_output: "已完成审查。", end_reason: null,
    });
    expect(parseTransportState(snapshot)).toBe(snapshot);
    const sent = part({ kind: "agent-task-result", operation: "send", task_id: 501, agent_name: "收件方" });
    expect(parseTransportState(sent)).toBe(sent);
    // 对方 Run 的状态读任务状态推送，不在展示数据里。
    expect(() => parseTransportState(part({
      kind: "agent-task-result", operation: "send", task_id: 501,
      agent_name: "主任务", status: "completed",
    }))).toThrow("固定字段");
    expect(() => parseTransportState(part({
      kind: "agent-task-result", operation: "send", direction: "sideways", task_id: 501,
      agent_name: null,
    }))).toThrow("固定字段");
  });

  it("accepts the fork display contract and rejects fields outside it", () => {
    const fork = (display_data: Record<string, unknown>) => withToolPart({
      type: "tool-call", awaitingUser: false, toolCallId: "fork-1", toolName: "fork_task", status: "completed", args: {}, display_data,
    });
    const snapshot = fork({ kind: "fork-task-result", title: "前端部分", child_task_id: 41 });
    expect(parseTransportState(snapshot)).toBe(snapshot);
    // 分叉的状态与结论都在它自己的对话里，卡片只带标题与 Task id。
    expect(() => parseTransportState(fork({ kind: "fork-task-result", title: "前端部分", child_task_id: 41, status: "completed" }))).toThrow("固定字段");
    expect(() => parseTransportState(fork({ kind: "fork-task-result", title: "前端部分" }))).toThrow();
  });

  it("rejects child output embedded in a parent delegation display", () => {
    const snapshot = withToolPart({
      type: "tool-call", awaitingUser: false, toolCallId: "delegate-1", toolName: "delegate_task", status: "completed", args: {},
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

  // 目的：等待用户是后端每次显式写的事实，缺省会让「排队待执行」与「等你操作」再次混在 pending 里。
  it("工具调用的 awaitingUser 必须是布尔值，缺省或非布尔都拒绝", () => {
    const base = { type: "tool-call", toolCallId: "h1", toolName: "browser_handoff", status: "pending", args: {} };
    expect(() => parseTransportState(withToolPart(base))).toThrow("messages[0].parts[0].awaitingUser");
    expect(() => parseTransportState(withToolPart({ ...base, awaitingUser: "true" }))).toThrow("awaitingUser");
    const awaiting = withToolPart({ ...base, awaitingUser: true });
    expect(parseTransportState(awaiting)).toBe(awaiting);
  });

  // 目的：展示外壳已归前端工具目录，后端不再下发；再出现即视为前后端漂移。
  it("tool-call part 不再接受 presentation / heading", () => {
    const base = { type: "tool-call", awaitingUser: false, toolCallId: "r1", toolName: "read_file", status: "running", args: { path: "a.ts" } };
    const ok = withToolPart(base);
    expect(parseTransportState(ok)).toBe(ok);
    expect(() => parseTransportState(withToolPart({ ...base, presentation: { verb: "读取文件", icon: "eye" } }))).toThrow("已知字段");
    expect(() => parseTransportState(withToolPart({ ...base, heading: { title: "读取文件", target: "a.ts" } }))).toThrow("已知字段");
  });

  it("结局带 durationMs（非负整数或 null），活动带 ISO 的 startedAt", () => {
    const timed = transportState({ messages: [assistantMessage("a1", 1, [], outcome("completed", { durationMs: 192_000 }))] });
    expect(parseTransportState(timed)).toBe(timed);
    expect(() => parseTransportState(transportState({
      messages: [assistantMessage("a1", 1, [], outcome("completed", { durationMs: -1 }))],
    }))).toThrow("outcome.durationMs");
    expect(() => parseTransportState(transportState({ messages: [assistantMessage("a1", 1, [], without(outcome("completed"), "durationMs") as never)] }))).toThrow("outcome");
    expect(() => parseTransportState(transportState({ activity: activity(1, "running", { startedAt: "not a date" }) }))).toThrow("activity.startedAt");
    expect(() => parseTransportState(transportState({ activity: without(activity(1, "running"), "startedAt") as never }))).toThrow("activity");
  });

  it("rejects an activity without the error field instead of tolerating a partial snapshot", () => {
    const partial = { ...activity(1, "running") } as Partial<ReturnType<typeof activity>>;
    delete partial.error;
    expect(() => parseTransportState(transportState({ activity: partial as never }))).toThrow("activity");
  });
});

describe("history window", () => {
  it("快照必须带非负整数的 messageOffset", () => {
    expect(parseTransportState(transportState({ messageOffset: 12 })).messageOffset).toBe(12);
    expect(() => parseTransportState(transportState({ messageOffset: -1 }))).toThrow("messageOffset");
    expect(() => parseTransportState(without(transportState(), "messageOffset"))).toThrow(TransportSnapshotValidationError);
  });

  it("更早的一页与已加载消息 id 不得重复", () => {
    const page = { messages: [userMessage("u0", 1), assistantMessage("a0", 1)], messageOffset: 0 };
    expect(parseHistoryPage(page, new Set(["u1"])).messages).toHaveLength(2);
    expect(() => parseHistoryPage(page, new Set(["a0"]))).toThrow(TransportSnapshotValidationError);
    expect(() => parseHistoryPage({ ...page, extra: 1 }, new Set())).toThrow(TransportSnapshotValidationError);
  });
});
