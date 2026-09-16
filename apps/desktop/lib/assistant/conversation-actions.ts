import type { TransportState } from "@/lib/assistant/contract";

export type ComposerAction = "send" | "stop" | "cancelling" | "resume";

/**
 * 返回当前 canonical snapshot 中最后一条用户消息的 id。
 *
 * 该判断只基于后端 snapshot，不读取 assistant-ui 的 optimistic message，
 * 因而不会让尚未落库的 pending command 获得编辑入口。
 */
export function getLatestUserMessageId(state: Pick<TransportState, "runs"> | null | undefined): string | null {
  if (!state || !Array.isArray(state.runs)) return null;
  for (const run of [...state.runs].reverse()) {
    for (const message of [...run.messages].reverse()) {
      if (message.role === "user") return message.id;
    }
  }
  return null;
}

/**
 * 判断指定用户消息是否是当前对话唯一允许编辑重跑的消息。
 */
export function isLatestUserMessage(
  state: Pick<TransportState, "runs"> | null | undefined,
  messageId: string,
): boolean {
  return getLatestUserMessageId(state) === messageId;
}

export function isEditableLatestRunUserMessage(
  state: TransportState | null | undefined,
  messageId: string,
): boolean {
  if (!state || state.current_run_id == null) return false;
  if (!isLatestUserMessage(state, messageId)) return false;
  const run = state.runs.find((candidate) => candidate.runId === state.current_run_id);
  return run?.messages.some((message) => message.id === messageId && message.role === "user") ?? false;
}

/**
 * 只要当前 canonical run 是 cancelled 就允许用户继续运行。
 *
 * endReason 是后端保留的展示/审计事实，不参与 resume 资格判断。
 */
export function isResumableCancelledRun(state: TransportState | null | undefined): boolean {
  if (!state || !Array.isArray(state.runs) || state.current_run_id == null) return false;
  const run = state.runs.find((candidate) => candidate.runId === state.current_run_id);
  if (!run || run.status !== "cancelled") return false;
  return run.messages.some((message) => message.role === "user");
}

export function getTransportRunId(state: unknown): number | null {
  if (typeof state !== "object" || state === null) return null;
  const currentRunId = (state as { current_run_id?: unknown }).current_run_id;
  return typeof currentRunId === "number" ? currentRunId : null;
}

export function deriveComposerAction(input: {
  isRunning: boolean;
  isDraftEmpty: boolean;
  canResume: boolean;
  isCancelling?: boolean;
}): ComposerAction {
  if (input.isCancelling) return "cancelling";
  if (input.isRunning) return "stop";
  if (input.canResume && input.isDraftEmpty) return "resume";
  return "send";
}
