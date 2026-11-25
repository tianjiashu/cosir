import { isTerminalRunStatus, type InputDelivery, type TransportMessage, type TransportState } from "@/lib/assistant/contract";

export type ComposerAction = "send" | "stop" | "cancelling" | "resume";

/**
 * 返回当前 canonical snapshot 中最后一条已消费的输入所在消息：user 消息（含 Agent 消息），或带途中来信的回复；排队消息不计入。
 *
 * 该判断只基于后端 snapshot，不读取 assistant-ui 的 optimistic message，
 * 因而不会让尚未落库的 pending command 获得撤回入口。
 */
function latestUserMessage(state: Pick<TransportState, "messages"> | null | undefined): TransportMessage | null {
  if (!state || !Array.isArray(state.messages)) return null;
  for (let index = state.messages.length - 1; index >= 0; index -= 1) {
    const message = state.messages[index];
    if (!message.inherited && (message.role === "user" || message.parts.some((part) => part.type === "agent-message"))) return message;
  }
  return null;
}

/**
 * 判断指定用户消息是否允许撤回（编辑即撤回后放回输入框）：后端只接受「Session 已停下」时撤回最新一条
 * 已消费用户消息开启的回合，这里与之对齐以免暴露必然被拒的入口。最新一条是 Agent 消息时没有可撤回
 * 的（后端同样拒绝：越过它撤回更早的用户消息会把另一个 Agent 交来的消息一起撤回，对方不会重发）。
 * 渲染与点击之间的竞态由后端 409 裁决。还没并入的消息另由 {@link messageDelivery} 判断。
 */
export function isWithdrawableLatestUserMessage(
  state: TransportState | null | undefined,
  messageId: string,
): boolean {
  if (!state || !isTerminalRunStatus(state.activity?.status)) return false;
  const latest = latestUserMessage(state);
  return latest?.id === messageId && latest.source === null;
}

/**
 * 还没并入的用户消息的去向（converter 写进 `metadata.custom.delivery`）；已并入的为 null。
 *
 * - `next_run`：排队，画在输入框上方的排队条里（含正在提交的 ⌘回车发送），不进对话流；
 * - `immediate`：插话，照常是对话流末尾的用户气泡，等下一步并入，可「立即发送」打断当前工具。
 */
export function messageDelivery(custom: Record<string, unknown> | undefined): InputDelivery | null {
  const delivery = custom?.delivery;
  return delivery === "next_run" || delivery === "immediate" ? delivery : null;
}

/**
 * 排队条里的消息 id 以 `|` 拼成的字符串（没有时为空串），供 `useAuiState` selector 使用：返回原始值，
 * 队列不变时订阅方不随流式 token 重渲染。排队条与对话流的过滤共用这一份。
 */
export function queueBarMessageIdsKey(
  messages: readonly { id: string; role: string; metadata: { custom?: Record<string, unknown> } }[],
): string {
  return messages
    .filter((message) => message.role === "user" && messageDelivery(message.metadata.custom) === "next_run")
    .map((message) => message.id)
    .join("|");
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
