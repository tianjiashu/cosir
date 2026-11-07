import { ToolCallIndex } from "@/lib/assistant/tool-call-index";
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
  TransportToolCallPart,
  TransportUsage,
} from "@/lib/assistant/contract";

const EMPTY_LAUNCH: TransportRunLaunch = { modelConfigId: null, reasoningEffort: null, disabledToolGroups: [] };

/** 单测共用的 Task 视角 snapshot 构造器：只填契约必填字段，调用方覆盖需要的部分。 */
export function transportState(overrides: Partial<TransportState> = {}): TransportState {
  return { title: "测试对话", messages: [], messageOffset: 0, activity: null, terminal_sessions: [], background_tasks: [], foreground_waits: [], handoffs: [], ...overrides };
}

function textPart(text: string): TransportMessagePart {
  return { type: "text", text };
}

/** 工具调用 part 构造器：默认 running、不等用户，调用方覆盖需要的部分。 */
export function toolCallPart(toolCallId: string, toolName: string, overrides: Partial<TransportToolCallPart> = {}): TransportToolCallPart {
  return {presentation: "activity",  type: "tool-call", toolCallId, toolName, status: "running", args: {}, awaitingUser: false, ...overrides };
}

export function userMessage(id: string, runId: number, text = "hello"): TransportMessage {
  return { id, role: "user", parts: [textPart(text)], runId, source: null, outcome: null };
}

/** Agent 消息：另一个 Agent 交来、开启一轮的 user 消息，带发送方。 */
export function agentMessage(id: string, runId: number, text = "请完成子任务", sourceTaskId = 9): TransportMessage {
  return { id, role: "user", parts: [textPart(text)], runId, outcome: null, source: { taskId: sourceTaskId, name: "主任务" } };
}

export function assistantMessage(
  id: string,
  runId: number,
  parts: TransportMessagePart[] = [textPart("answer")],
  outcome: TransportOutcome | null = null,
): TransportMessage {
  return { id, role: "assistant", parts, runId, outcome, source: null };
}

export function outcome(
  status: TransportTerminalRunStatus,
  overrides: Partial<TransportOutcome> = {},
): TransportOutcome {
  return { status, endReason: status === "completed" ? "stop" : null, error: null, usage: null, contextWindowTotal: null, durationMs: 0, ...overrides };
}

export function activity(
  runId: number,
  status: TransportRunStatus,
  overrides: Partial<TransportActivity> = {},
): TransportActivity {
  return {modelActivity: null,
    runId,
    status,
    endReason: null,
    error: null,
    usage: null,
    contextWindowTotal: null,
    launch: EMPTY_LAUNCH,
    startedAt: "2026-10-27T00:00:00.000Z",
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
    commandRunning: false,
    createdAt: "2026-10-27T00:00:00.000Z",
    ...overrides,
  };
}

/** 与 frame store 相同的完整快照派生入口，供独立镜像/命令单测使用。 */
export function indexedState(state: TransportState) {
  const index = new ToolCallIndex();
  index.replace(state.messages);
  return { ...state, toolCalls: index.snapshot };
}
