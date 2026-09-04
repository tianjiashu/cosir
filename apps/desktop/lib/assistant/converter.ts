import {
  type ThreadMessage,
  type ThreadUserMessage,
  type ThreadAssistantMessage,
} from "@assistant-ui/react";
import type { MessageStatus } from "@assistant-ui/core";
import type { ReadonlyJSONObject } from "assistant-stream/utils";
import type {
  TransportError,
  TransportMessage,
  TransportState,
  TransportTextPart,
  TransportReasoningPart,
  TransportToolCallPart,
} from "@/lib/assistant/contract";

/**
 * 把后端 Transport 文本 part 映射为 assistant-ui 的 text part。
 *
 * @param part - 后端传来的 `TransportTextPart`，含文本与运行状态。
 * @returns assistant-ui 的 text content part；`status` 由 part 的 running 状态
 *   转为 `{ type: "running" }` 或 `{ type: "complete" }`。
 */
function toTextPart(
  part: TransportTextPart,
): ThreadMessage["content"][number] {
  return {
    type: "text",
    text: part.text,
    status:
      part.status === "running"
        ? { type: "running" }
        : { type: "complete" },
  };
}

/**
 * 把后端 Transport 推理 part 映射为 assistant-ui 的 reasoning part。
 *
 * @param part - 后端传来的 `TransportReasoningPart`，含推理文本与运行状态。
 * @returns assistant-ui 的 `ReasoningMessagePart`；`status` 由 part 的 running
 *   状态转为 `{ type: "running" }` 或 `{ type: "complete" }`。
 */
function toReasoningPart(
  part: TransportReasoningPart,
): ThreadMessage["content"][number] {
  return {
    type: "reasoning",
    text: part.text,
    status:
      part.status === "running"
        ? { type: "running" }
        : { type: "complete" },
  };
}

/**
 * 把后端 Transport 工具调用 part 映射为 assistant-ui 的 tool-call part。
 *
 * @param part - 后端传来的 `TransportToolCallPart`，含工具名、参数对象与结果/错误。
 * @returns assistant-ui 的 tool-call content part；`args` 转为 ReadonlyJSONObject，
 *   从 `args` 派生 `argsText`（assistant-ui 期望原始参数文本流）以满足其契约；
 *   后端以 `error` 字段表达失败，此处转为 `isError: true` 并把错误文本填入 `result`
 *   （assistant-ui 无独立 error 通道，error 经 result 呈现），成功时仅附 `result`。
 */
function toToolCallPart(
  part: TransportToolCallPart,
): ThreadMessage["content"][number] {
  const isCancelled = part.status === "cancelled";
  const isFailed = part.status === "failed";
  const result = isCancelled
    ? { kind: "tool-cancelled" as const }
    : isFailed
      ? {
          kind: "tool-error" as const,
          code: part.errorCode,
          message: part.error ?? "工具执行失败",
          data: part.result,
        }
      : part.result;

  return {
    type: "tool-call",
    toolCallId: part.toolCallId,
    toolName: part.toolName,
    // 当前 Transport 传输的是已解析参数，不是增量 argsText；这里仅生成稳定的
    // 展示文本，不能把它当作原始参数流。
    argsText: JSON.stringify(part.args, null, 2),
    args: part.args as ReadonlyJSONObject,
    ...(isFailed || isCancelled ? { isError: isFailed, result } : result !== undefined ? { result } : {}),
  };
}

/**
 * 把后端领域 status 翻译为 assistant-ui 的 `MessageStatus`（任务书 §5.4 映射表）。
 *
 * 映射依据（唯一来源）为后端 `message.status` 与中性 `endReason`：
 * - `pending` / `running` → `{ type: "running" }`
 * - `completed` → `{ type: "complete", reason: "stop" }`
 * - `cancelled` → `{ type: "incomplete", reason: "cancelled" }`
 * - `failed` 且 `endReason === "client_disconnected"` → `{ type: "incomplete", reason: "cancelled" }`
 * - `failed` 其它 / 空 → `{ type: "incomplete", reason: "error", error: { turnId, endReason } }`
 *
 * 兜底策略：若 `status` 不属于上述任一已知值（后端未来可能新增状态），既不静默
 * 当成 `complete`，也不抛出中断渲染，而是回落为 `{ type: "incomplete", reason: "other" }`
 * 以明确标记「状态未知」，避免掩盖潜在的协议不一致。这与 assistant-ui 契约允许的
 * `reason: "other"` 对齐，且把未知状态显式呈现给 UI 而非伪装成正常完成。
 *
 * @param message - 后端传来的 `TransportMessage`，含领域 `status` 与中性 `endReason`。
 * @returns 对应的 assistant-ui `MessageStatus`。
 */
function toMessageStatus(message: TransportMessage): MessageStatus {
  const { status, endReason } = message;
  switch (status) {
    case "pending":
    case "running":
      return { type: "running" };
    case "completed":
      return { type: "complete", reason: "stop" };
    case "cancelled":
      return { type: "incomplete", reason: "cancelled" };
    case "failed":
      if (endReason === "client_disconnected") {
        return { type: "incomplete", reason: "cancelled" };
      }
      return {
        type: "incomplete",
        reason: "error",
        error: `对话运行失败：${endReason ?? "runtime_failed"}。请检查模型配置或后端日志。`,
      };
    default:
      // 未知领域状态：显式回落为 incomplete/other，不伪装成 complete。
      return { type: "incomplete", reason: "other" };
  }
}

/**
 * 把单条后端 Transport 消息映射为 assistant-ui 的 ThreadMessage。
 *
 * 职责仅为「字段对齐」+ 状态语义翻译：按 `role` 产出 user / assistant 两种
 * ThreadMessage；part 数组逐条经 `toTextPart` / `toReasoningPart` / `toToolCallPart`
 * 映射（三类 part 严格按 `part.type` 分流，推理 part 不再被误当作工具调用）；
 * `createdAt` 缺失时回落 `new Date()`；assistant 消息的 status 经 `toMessageStatus`
 * 依据任务书 §5.4 映射表完整翻译（覆盖 running / complete / cancelled / failed 等全部五行
 * 及未知状态兜底）。
 *
 * @param message - 后端传来的 `TransportMessage`。
 * @returns 与 message 字段对齐的 `ThreadMessage`（user 或 assistant 形态）。
 */
function toThreadMessage(message: TransportMessage): ThreadMessage {
  const content = message.parts.map((part) => {
    if (part.type === "text") return toTextPart(part);
    if (part.type === "reasoning") return toReasoningPart(part);
    return toToolCallPart(part);
  }) as ThreadMessage["content"];

  if (message.role === "user") {
    const userMessage: ThreadUserMessage = {
      id: message.id,
      role: "user",
      content: content as ThreadUserMessage["content"],
      attachments: [],
      createdAt: message.createdAt ? new Date(message.createdAt) : new Date(),
      metadata: {
        unstable_state: undefined,
        unstable_annotations: undefined,
        unstable_data: undefined,
        steps: undefined,
        submittedFeedback: undefined,
        timing: undefined,
        custom: {},
      },
    };
    return userMessage;
  }

  const assistantMessage: ThreadAssistantMessage = {
    id: message.id,
    role: "assistant",
    content: content as ThreadAssistantMessage["content"],
    status: toMessageStatus(message),
    createdAt: message.createdAt ? new Date(message.createdAt) : new Date(),
    metadata: {
      unstable_state: null,
      unstable_annotations: [],
      unstable_data: [],
      steps: [],
      custom: {},
    },
  };
  return assistantMessage;
}

/**
 * 用户 add-message 命令经 `isUserAddMessageCommand` 收窄后的结构。
 *
 * 独立命名以约束守卫输出与消费方入参同构：`toPendingUserMessage` /
 * `extractUserAddMessageText` 均要求传入已收窄的命令，避免 role/parts 形状
 * 在守卫与消费之间漂移。
 */
type UserAddMessageCommand = {
  type: "add-message";
  message: { role: "user"; parts: ReadonlyArray<{ type: string; text?: string }> };
};

/**
 * 判定一个待发送命令是否为「用户 add-message」命令并收窄其形状。
 *
 * assistant-ui 的 `AssistantTransportCommand` 是含宽松 `UserCommands` 的联合类型，
 * 直接按 `type` 判别无法收窄出 `message.parts`，故用本守卫做结构化收窄。
 * 收窄结果应整体传给 `toPendingUserMessage` 或 `extractUserAddMessageText`。
 *
 * @param command - 待判定的命令对象（来自 `connectionMetadata.pendingCommands`）。
 * @returns 类型谓词：是用户 add-message 命令时为 true。
 */
function isUserAddMessageCommand(command: unknown): command is UserAddMessageCommand {
  if (typeof command !== "object" || command === null) return false;
  const candidate = command as { type?: unknown; message?: unknown };
  if (candidate.type !== "add-message") return false;
  const message = candidate.message;
  return (
    typeof message === "object" &&
    message !== null &&
    (message as { role?: unknown }).role === "user" &&
    Array.isArray((message as { parts?: unknown }).parts)
  );
}

/**
 * 提取用户 add-message 命令中的全部文本（多 text part 以换行拼接）。
 *
 * 供发送请求组装（commandId 幂等键的消息指纹）与发送失败回填 composer 共用，
 * 收口在一处避免文本拼接规则漂移。
 *
 * @param command - 待发送命令（不做类型假设，非 add-message 返回空串）。
 * @returns 命令文本；无文本 part 时为空串。
 */
export function extractUserAddMessageText(command: unknown): string {
  if (!isUserAddMessageCommand(command)) return "";
  return command.message.parts
    .filter(
      (part): part is { type: "text"; text: string } =>
        part.type === "text" && typeof part.text === "string",
    )
    .map((part) => part.text)
    .join("\n");
}

/**
 * 把一个仍在传输中的用户 add-message 命令映射为乐观用户消息。
 *
 * assistant-ui 的 transport runtime 不会自动把 pendingCommands 合并进消息流，
 * converter 必须自行消费，否则从点击发送到后端第一帧 state 到达之间用户消息
 * 完全不可见，造成「消息没有发出去」的错觉。乐观 id 用 `pending-{index}` 稳定
 * 命名：服务端首帧到达后命令被 markDelivered 移除，同帧由服务端消息接管。
 *
 * @param command - 待发送命令；必须已由 `isUserAddMessageCommand` 收窄为
 *   `UserAddMessageCommand`（直接传入未收窄对象会被 TS 拒绝）。
 * @param index - 命令在 pendingCommands 中的序号，用于生成稳定乐观 id。
 * @returns assistant-ui 的 ThreadUserMessage；无有效文本 part 时返回 null。
 */
function toPendingUserMessage(command: UserAddMessageCommand, index: number): ThreadMessage | null {
  const parts = command.message.parts
    .filter(
      (part): part is { type: "text"; text: string } =>
        part.type === "text" && typeof part.text === "string" && part.text.length > 0,
    )
    .map((part) => ({ type: "text" as const, text: part.text }));
  if (parts.length === 0) return null;
  return toThreadMessage({
    id: `pending-${index}`,
    role: "user",
    parts,
    createdAt: new Date().toISOString(),
  });
}

/**
 * 把发送失败错误映射为一条 assistant 错误消息，使失败对用户可见。
 *
 * assistant-ui transport 在请求失败时会静默重置命令队列且项目未传 onError 时
 * 无任何提示；这里把本地写入 `TransportState.error` 的发送失败渲染为消息流
 * 末尾的错误条（经 thread 的 `MessagePrimitive.Error` 呈现），内容为空、仅靠
 * status.error 承载文案，避免与错误条重复。
 *
 * @param error - 本地发送失败的稳定错误结构。
 * @returns 带错误 status 的 ThreadAssistantMessage。
 */
function toSendErrorMessage(error: TransportError): ThreadAssistantMessage {
  return {
    id: "send-error",
    role: "assistant",
    content: [],
    status: {
      type: "incomplete",
      reason: "error",
      error: `消息发送失败：${error.message}`,
    },
    createdAt: new Date(),
    metadata: {
      unstable_state: null,
      unstable_annotations: [],
      unstable_data: [],
      steps: [],
      custom: {},
    },
  };
}

/**
 * 把后端 state 快照与本地待发送命令合成完整对话视图。
 *
 * 组成顺序：服务端 canonical messages（经 `toThreadMessage`）→ 待发送乐观用户
 * 消息（`toPendingUserMessage`，消除发送后首帧前的空窗）→ 本地发送失败错误条
 * （`toSendErrorMessage`，服务端下一帧 state 会覆盖 `state.error` 使其自动消失）。
 * `isRunning` 由 transport 的 `isSending` 直接透传（二者语义等价：请求与状态流
 * 消费期间均为 true），无需额外映射。
 *
 * 第二参数刻意只声明 `pendingCommands` / `isSending` 两个字段的最小字段子集，
 * 而非 assistant-ui 的完整 `AssistantTransportConnectionMetadata`：converter 是
 * 无 React 依赖的纯协议映射层，依赖最小结构可避免被 assistant-ui 内部形状
 * 变化牵连；`pendingCommands` 元素在函数内经 `isUserAddMessageCommand` 守卫
 * 收窄后才交给 `toPendingUserMessage`。
 *
 * @param state - 后端 Transport state 快照。
 * @param connectionMetadata - assistant-ui 传输元数据的最小字段子集
 *   （pendingCommands / isSending）。
 * @returns 含 messages 与 isRunning 的 assistant-ui 线程视图。
 */
function toTransportThreadView(
  state: TransportState,
  connectionMetadata: { pendingCommands: readonly unknown[]; isSending: boolean },
): { messages: ThreadMessage[]; isRunning: boolean } {
  const pendingMessages = connectionMetadata.pendingCommands
    .map((command, index) =>
      isUserAddMessageCommand(command) ? toPendingUserMessage(command, index) : null,
    )
    .filter((message): message is ThreadMessage => message !== null);
  const messages: ThreadMessage[] = [
    ...state.messages.map(toThreadMessage),
    ...pendingMessages,
  ];
  if (state.error !== null) {
    messages.push(toSendErrorMessage(state.error));
  }
  return { messages, isRunning: connectionMetadata.isSending };
}

export { toThreadMessage, toTransportThreadView };
