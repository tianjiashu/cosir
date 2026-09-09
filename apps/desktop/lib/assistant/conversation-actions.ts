import type { TransportState } from "@/lib/assistant/contract";

export type ComposerAction = "send" | "stop" | "resume";

/**
 * 返回当前 canonical snapshot 中最后一条用户消息的 id。
 *
 * 该判断只基于后端 snapshot，不读取 assistant-ui 的 optimistic message，
 * 因而不会让尚未落库的 pending command 获得编辑入口。
 */
export function getLatestUserMessageId(state: Pick<TransportState, "messages"> | null | undefined): string | null {
  if (!state || !Array.isArray(state.messages)) return null;
  for (const message of [...state.messages].reverse()) {
    if (message.role === "user") return message.id;
  }
  return null;
}

/**
 * 判断指定用户消息是否是当前对话唯一允许编辑重跑的消息。
 */
export function isLatestUserMessage(
  state: Pick<TransportState, "messages"> | null | undefined,
  messageId: string,
): boolean {
  return getLatestUserMessageId(state) === messageId;
}

export function isEditableLatestRunUserMessage(
  state: TransportState | null | undefined,
  messageId: string,
): boolean {
  if (!state || state.run.runId == null) return false;
  if (!isLatestUserMessage(state, messageId)) return false;
  return state.messages.some(
    (message) => message.id === messageId
      && message.role === "user"
      && message.runId === state.run.runId,
  );
}

/**
 * 只要当前 canonical run 是 cancelled 就允许用户继续运行。
 *
 * endReason 是后端保留的展示/审计事实，不参与 resume 资格判断。
 */
export function isResumableCancelledRun(state: TransportState | null | undefined): boolean {
  if (!state || typeof state.run !== "object" || state.run === null || !Array.isArray(state.messages)) return false;
  const runId = state.run.runId;
  if (runId == null || state.run.status !== "cancelled") return false;

  return state.messages.some(
    (message) => message.role === "user" && message.runId === runId,
  );
}

export function getTransportRunId(state: unknown): number | null {
  if (typeof state !== "object" || state === null) return null;
  const run = (state as { run?: unknown }).run;
  if (typeof run !== "object" || run === null) return null;
  const runId = (run as { runId?: unknown }).runId;
  return typeof runId === "number" ? runId : null;
}

export function deriveComposerAction(input: {
  isRunning: boolean;
  isDraftEmpty: boolean;
  canResume: boolean;
}): ComposerAction {
  if (input.isRunning) return "stop";
  if (input.canResume && input.isDraftEmpty) return "resume";
  return "send";
}
