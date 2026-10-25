import { isTerminalRunStatus, type TransportState } from "@/lib/assistant/contract";

export type ComposerAction = "send" | "stop" | "cancelling" | "resume";

/**
 * 返回当前 canonical snapshot 中最后一条已消费用户消息的 id；排队消息不计入。
 *
 * 该判断只基于后端 snapshot，不读取 assistant-ui 的 optimistic message，
 * 因而不会让尚未落库的 pending command 获得编辑入口。
 */
export function getLatestUserMessageId(state: Pick<TransportState, "messages"> | null | undefined): string | null {
  if (!state || !Array.isArray(state.messages)) return null;
  for (let index = state.messages.length - 1; index >= 0; index -= 1) {
    if (state.messages[index].role === "user") return state.messages[index].id;
  }
  return null;
}

/**
 * 判断指定用户消息是否允许编辑重跑：后端只接受「Task 已停下」时编辑最新一条已消费用户消息，
 * 这里与之对齐以免暴露必然被拒的入口。排队中的消息另由 {@link isQueuedUserMessage} 判断。
 */
export function isEditableLatestUserMessage(
  state: TransportState | null | undefined,
  messageId: string,
): boolean {
  if (!state || !isTerminalRunStatus(state.activity?.status)) return false;
  return getLatestUserMessageId(state) === messageId;
}

/** 排队中（尚未并入）的消息可随时原地编辑，不影响正在运行的 Run。 */
export function isQueuedUserMessage(
  state: TransportState | null | undefined,
  messageId: string,
): boolean {
  return Boolean(state?.queued_messages?.some((message) => message.id === messageId));
}

/**
 * 当前活动已取消或失败时允许显式续跑（同一个 Run 从模型边界继续）。
 *
 * endReason 是后端保留的展示/审计事实，不参与 resume 资格判断。
 */
export function canResumeStoppedRun(state: TransportState | null | undefined): boolean {
  if (!state?.activity || !["cancelled", "failed"].includes(state.activity.status)) return false;
  return state.messages.some((message) => message.role === "user") || Boolean(state.queued_messages?.length);
}

export function deriveComposerAction(input: {
  isRunning: boolean;
  isDraftEmpty: boolean;
  canResume: boolean;
  isCancelling?: boolean;
}): ComposerAction {
  if (input.isCancelling) return "cancelling";
  if (input.isRunning && input.isDraftEmpty) return "stop";
  if (input.canResume && input.isDraftEmpty) return "resume";
  return "send";
}
