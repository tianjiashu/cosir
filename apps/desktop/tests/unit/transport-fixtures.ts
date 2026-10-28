import type {
  TerminalSessionEntry,
  TransportActivity,
  TransportMessage,
  TransportMessagePart,
  TransportOutcome,
  TransportQueuedMessage,
  TransportRunLaunch,
  TransportRunStatus,
  TransportState,
  TransportTerminalRunStatus,
  TransportUsage,
} from "@/lib/assistant/contract";

const EMPTY_LAUNCH: TransportRunLaunch = { modelConfigId: null, reasoningEffort: null, disabledToolGroups: [] };

/** 单测共用的 Task 视角 snapshot 构造器：只填契约必填字段，调用方覆盖需要的部分。 */
export function transportState(overrides: Partial<TransportState> = {}): TransportState {
  return { title: "测试对话", messages: [], activity: null, terminal_sessions: [], background_tasks: [], foreground_waits: [], ...overrides };
}

function textPart(text: string): TransportMessagePart {
  return { type: "text", text };
}

export function userMessage(id: string, runId: number, text = "hello"): TransportMessage {
  return { id, role: "user", parts: [textPart(text)], runId, outcome: null };
}

export function assistantMessage(
  id: string,
  runId: number,
  parts: TransportMessagePart[] = [textPart("answer")],
  outcome: TransportOutcome | null = null,
): TransportMessage {
  return { id, role: "assistant", parts, runId, outcome };
}

export function outcome(
  status: TransportTerminalRunStatus,
  overrides: Partial<TransportOutcome> = {},
): TransportOutcome {
  return { status, endReason: status === "completed" ? "stop" : null, error: null, usage: null, contextWindowTotal: null, ...overrides };
}

export function activity(
  runId: number,
  status: TransportRunStatus,
  overrides: Partial<TransportActivity> = {},
): TransportActivity {
  return {
    runId,
    status,
    endReason: null,
    error: null,
    usage: null,
    contextWindowTotal: null,
    launch: EMPTY_LAUNCH,
    ...overrides,
  };
}

export function queuedMessage(id: string, text = "queued", delivery: TransportQueuedMessage["delivery"] = "next_run"): TransportQueuedMessage {
  return { id, role: "user", parts: [textPart(text)], delivery };
}

export function usage(overrides: Partial<TransportUsage> = {}): TransportUsage {
  return {
    input_tokens: null,
    output_tokens: null,
    total_tokens: null,
    cache_hit_tokens: null,
    cache_miss_tokens: null,
    reasoning_tokens: null,
    ...overrides,
  };
}

/** snapshot `terminal_sessions` 条目：默认是用户新建、运行中的 zsh 会话。 */
export function terminalSession(sessionId: string, overrides: Partial<TerminalSessionEntry> = {}): TerminalSessionEntry {
  return {
    sessionId,
    label: "",
    shellKind: "zsh",
    createdBy: "user",
    status: "running",
    endReason: null,
    exitCode: null,
    integration: "ready",
    inputHolder: null,
    commandRunning: false,
    handoff: null,
    createdAt: "2026-10-27T00:00:00.000Z",
    ...overrides,
  };
}
