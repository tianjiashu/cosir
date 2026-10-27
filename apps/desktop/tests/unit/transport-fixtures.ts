import type {
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

/** 单测共用的 Task 视角 snapshot 构造器：只填契约必填字段，调用方覆盖需要的部分。 */
export const EMPTY_LAUNCH: TransportRunLaunch = { modelConfigId: null, reasoningEffort: null, banTools: [] };

export function transportState(overrides: Partial<TransportState> = {}): TransportState {
  return { messages: [], activity: null, ...overrides };
}

export function textPart(text: string): TransportMessagePart {
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

/** 一轮已完成的问答：用户消息 + 带结局的助手回复。 */
export function completedTurn(userId: string, runId: number, text = "answer"): TransportMessage[] {
  return [
    userMessage(userId, runId),
    assistantMessage(`assistant-${userId}`, runId, [textPart(text)], outcome("completed")),
  ];
}
