import { ToolCallIndex } from "@/lib/assistant/tools/tool-call-index";
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

/** 用户消息；第二个参数是它所在的回合（夹具里常与回复、活动的回合号一一对应）。 */
export function userMessage(id: string, turn: number, text = "hello"): TransportMessage {
  return { id, role: "user", parts: [textPart(text)], turn, inherited: false, source: null, outcome: null };
}

/** Agent 消息：另一个 Agent 交来、开启回合的 user 消息，带发送方。 */
export function agentMessage(id: string, turn: number, text = "请完成子任务", sourceTaskId = 9): TransportMessage {
  return { id, role: "user", parts: [textPart(text)], turn, inherited: false, outcome: null, source: { taskId: sourceTaskId, name: "主任务" } };
}

/** 回合途中到达的 Agent 消息。 */
export function agentMessagePart(id: string, text = "追加要求"): TransportMessagePart {
  return { type: "agent-message", id, text, source: { taskId: 9, name: "主任务" } };
}

/** 回复；第二个参数是它所在的回合。 */
export function assistantMessage(
  id: string,
  turn: number,
  parts: TransportMessagePart[] = [textPart("answer")],
  outcome: TransportOutcome | null = null,
): TransportMessage {
  return { id, role: "assistant", parts, turn, inherited: false, outcome, source: null };
}

export function outcome(
  status: TransportTerminalRunStatus,
  overrides: Partial<TransportOutcome> = {},
): TransportOutcome {
  return { status, endReason: status === "completed" ? "stop" : null, error: null, usage: null, contextWindowTotal: null, durationMs: 0, ...overrides };
}

/** 当前活动；第一个参数是它最后一个可见回合（当前回复所在的回合），可否恢复默认按状态。 */
export function activity(
  turn: number | null,
  status: TransportRunStatus,
  overrides: Partial<TransportActivity> = {},
): TransportActivity {
  return {modelActivity: null,
    status,
    turn,
    resumable: status === "cancelled" || status === "failed",
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
