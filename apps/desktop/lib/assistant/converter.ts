import type { MessageStatus } from "@assistant-ui/core";
import type {
  ThreadAssistantMessage,
  ThreadMessage,
  ThreadUserMessage,
} from "@assistant-ui/react";
import type { ReadonlyJSONObject, ReadonlyJSONValue } from "assistant-stream/utils";

import type {
  TransportError,
  TransportMessage,
  TransportReasoningPart,
  TransportRun,
  TransportState,
  TransportTextPart,
  TransportToolCallPart,
  TransportToolStatus,
} from "@/lib/assistant/contract";

export type UserAddMessageCommand = {
  type: "add-message";
  sourceId?: string | null;
  parentId?: string | null;
  message: {
    role: "user";
    parts: ReadonlyArray<{ type: string; text?: string }>;
  };
};

/** The render-time Run fields required to project one canonical message. */
export type TransportMessageRenderContext = {
  runId: TransportRun["runId"];
  runStatus: TransportRun["status"];
  endReason: TransportRun["endReason"];
  isLastRunMessage: boolean;
};

const optimisticCommandIds = new WeakMap<object, string>();

/**
 * 为一个 assistant-ui command 提供稳定的幂等标识。
 *
 * WeakMap 保证同一个 runtime 内，converter 和 request transformer 对同一
 * command 使用同一个 ID；UUID 保证 WebView/页面重载后重新创建的模块实例
 * 不会从相同的递增序列重新开始，避免命令 ID 撞上后端已有记录。
 */
export function getOrCreateTransportCommandId(command: object): string {
  const existing = optimisticCommandIds.get(command);
  if (existing) return existing;
  const commandId = `transport-${globalThis.crypto.randomUUID()}`;
  optimisticCommandIds.set(command, commandId);
  return commandId;
}

export function isUserAddMessageCommand(command: unknown): command is UserAddMessageCommand {
  if (typeof command !== "object" || command === null) return false;
  const candidate = command as { type?: unknown; message?: unknown };
  if (candidate.type !== "add-message" || typeof candidate.message !== "object" || candidate.message === null) {
    return false;
  }
  const message = candidate.message as { role?: unknown; parts?: unknown };
  return message.role === "user" && Array.isArray(message.parts);
}

function toPartStatus(status: string | undefined) {
  if (status === "running") return { type: "running" as const };
  if (status === "completed") return { type: "complete" as const };
  return { type: "incomplete" as const, reason: "other" as const };
}

function toTextPart(part: TransportTextPart, role: TransportMessage["role"]): ThreadMessage["content"][number] {
  return { type: "text", text: part.text, status: role === "user" && part.status === undefined ? { type: "complete" } : toPartStatus(part.status) };
}

function toReasoningPart(part: TransportReasoningPart): ThreadMessage["content"][number] {
  return {
    type: "reasoning",
    text: part.text,
    status: toPartStatus(part.status),
    ...(part.unstable_summary !== undefined ? { unstable_summary: part.unstable_summary } : {}),
  };
}

function normalizeToolStatus(status: string | undefined): TransportToolStatus {
  if (status === "pending" || status === "running" || status === "completed" || status === "failed" || status === "cancelled") {
    return status;
  }
  return "unknown";
}

/**
 * 把后端 tool-call part 映射为 assistant-ui part。
 *
 * `artifact` 是 UI-only 适配数据，保留后端五态、presentation、display_data 和错误；
 * 它不会被发送回后端，也不会成为对话事实。assistant-ui 的标准字段只承载
 * args/argsText/isError，renderer 从 artifact 读取后端展示声明。
 */
export function toToolCallPart(part: TransportToolCallPart): ThreadMessage["content"][number] {
  const backendStatus = normalizeToolStatus(part.status);
  const displayData = part.display_data ?? null;
  const displayDataRecord = typeof displayData === "object" && displayData !== null ? displayData as Record<string, unknown> : {};
  const statusHint = typeof displayDataRecord.status_hint === "string" ? displayDataRecord.status_hint : "执行失败";
  const artifact = {
    backendStatus,
    presentation: part.presentation ?? {},
    display_data: displayData,
    error: backendStatus === "cancelled"
      ? "已取消"
      : backendStatus === "failed" ? statusHint : null,
    errorCode: part.errorCode ?? null,
  };
  const args = part.args ?? {};

  if (backendStatus === "failed") {
    return {
      type: "tool-call",
      toolCallId: part.toolCallId,
      toolName: part.toolName,
      args: args as ReadonlyJSONObject,
      argsText: JSON.stringify(args, null, 2),
      artifact,
      // Assistant UI's optional client-side tracker uses result presence to
      // close a tool-call stream. This is a sanitized UI sentinel only: the
      // canonical backend status/error stays in artifact and the raw
      // TransportState is never replaced by this projection.
      result: { kind: "tool-terminal", status: "failed" },
      isError: true,
    };
  }

  if (backendStatus === "cancelled") {
    return {
      type: "tool-call",
      toolCallId: part.toolCallId,
      toolName: part.toolName,
      args: args as ReadonlyJSONObject,
      argsText: JSON.stringify(args, null, 2),
      artifact,
      // See the failed branch above. Never place the backend exception or
      // tool output in this sentinel.
      result: { kind: "tool-terminal", status: "cancelled" },
      isError: false,
    };
  }

  if (backendStatus === "unknown") {
    return {
      type: "tool-call",
      toolCallId: part.toolCallId,
      toolName: part.toolName,
      args: args as ReadonlyJSONObject,
      argsText: JSON.stringify(args, null, 2),
      artifact,
      result: { kind: "tool-unknown", message: "工具状态未知，等待后端确认" },
      isError: true,
    };
  }

  return {
    type: "tool-call",
    toolCallId: part.toolCallId,
    toolName: part.toolName,
    args: args as ReadonlyJSONObject,
    argsText: JSON.stringify(args, null, 2),
    artifact,
    ...(backendStatus === "completed" ? { isError: part.isError ?? false } : {}),
  };
}

function toMessageStatusForRun(status: TransportRun["status"], endReason: TransportRun["endReason"]): MessageStatus {
  switch (status) {
    case "pending":
    case "running":
      return { type: "running" };
    case "completed":
      return { type: "complete", reason: "stop" };
    case "cancelled":
      return { type: "incomplete", reason: "cancelled" };
    case "interrupted":
      return {
        type: "incomplete",
        reason: "error",
        error: "上次对话运行已中断，可以继续发送新消息。",
      };
    case "failed":
      if (endReason === "client_disconnected" || endReason === "cancelled") {
        return { type: "incomplete", reason: "cancelled" };
      }
      return {
        type: "incomplete",
        reason: "error",
        error: "对话运行失败，请检查模型配置或后端状态。",
      };
    default:
      return { type: "incomplete", reason: "other" };
  }
}

export function toMessageStatus(message: TransportMessage, run: TransportRun): MessageStatus {
  return toMessageStatusForRun(run.status, run.endReason);
}

function toThreadMessageWithContext(
  message: TransportMessage,
  context: TransportMessageRenderContext,
): ThreadMessage {
  const content = message.parts
    .map((part) => {
      switch (part.type) {
        case "text":
          return toTextPart(part, message.role);
        case "reasoning":
          return toReasoningPart(part);
        case "tool-call":
          return toToolCallPart(part);
        default:
          return null;
      }
    })
    .filter((part): part is NonNullable<typeof part> => part !== null) as ThreadMessage["content"];

  if (message.role === "user") {
    const userMessage: ThreadUserMessage = {
      id: message.id,
      role: "user",
      content: content as ThreadUserMessage["content"],
      attachments: [],
      createdAt: new Date(),
      metadata: {
        unstable_state: undefined,
        unstable_annotations: undefined,
        unstable_data: undefined,
        steps: undefined,
        submittedFeedback: undefined,
        timing: undefined,
        custom: {
          runId: context.runId,
          isLastRunMessage: context.isLastRunMessage,
        },
      },
    };
    return userMessage;
  }

  const assistantMessage: ThreadAssistantMessage = {
    id: message.id,
    role: "assistant",
    content: content as ThreadAssistantMessage["content"],
    status: toMessageStatusForRun(context.runStatus, context.endReason),
    createdAt: new Date(),
    metadata: {
      unstable_state: null,
      unstable_annotations: [],
      unstable_data: [],
      steps: [],
      custom: {
        runId: context.runId,
        isLastRunMessage: context.isLastRunMessage,
      },
    },
  };
  return assistantMessage;
}

export function toThreadMessage(
  message: TransportMessage,
  run: TransportRun,
  options: { isLastRunMessage?: boolean } = {},
): ThreadMessage {
  return toThreadMessageWithContext(message, {
    runId: run.runId,
    runStatus: run.status,
    endReason: run.endReason,
    isLastRunMessage: options.isLastRunMessage ?? false,
  });
}

/** Project one canonical message from only the Run fields needed by the UI. */
export function toThreadMessageWithRenderContext(
  message: TransportMessage,
  context: TransportMessageRenderContext,
): ThreadMessage {
  return toThreadMessageWithContext(message, context);
}

export function extractUserAddMessageText(command: unknown): string {
  if (!isUserAddMessageCommand(command)) return "";
  return command.message.parts
    .filter((part): part is { type: "text"; text: string } => part.type === "text" && typeof part.text === "string")
    .map((part) => part.text)
    .join("\n");
}

export function getUserAddMessageSourceId(command: unknown): string | null {
  if (!isUserAddMessageCommand(command)) return null;
  return typeof command.sourceId === "string" && command.sourceId.length > 0
    ? command.sourceId
    : null;
}

export function toPendingUserMessage(command: unknown): ThreadMessage | null {
  if (!isUserAddMessageCommand(command)) return null;
  const parts = command.message.parts
    .filter((part): part is { type: "text"; text: string } => part.type === "text" && typeof part.text === "string" && part.text.length > 0)
    .map((part) => ({ type: "text" as const, text: part.text }));
  if (parts.length === 0) return null;
  return toThreadMessage({
    id: `pending-${getOrCreateTransportCommandId(command)}`,
    role: "user",
    parts,
  }, {
    runId: -1,
    status: "completed",
    endReason: null,
    messages: [],
    usage: null,
  });
}

export function toSnapshotErrorMessage(error: TransportError): ThreadAssistantMessage {
  return {
    id: "snapshot-error",
    role: "assistant",
    content: [],
    status: { type: "incomplete", reason: "error", error: error.message },
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

export function toTransportThreadView(
  state: TransportState,
  connectionMetadata: { pendingCommands: readonly unknown[]; isSending: boolean },
): { messages: ThreadMessage[]; isRunning: boolean; state: ReadonlyJSONValue } {
  const pendingMessages = connectionMetadata.pendingCommands
    .map((command) => isUserAddMessageCommand(command) ? toPendingUserMessage(command) : null)
    .filter((message): message is ThreadMessage => message !== null);
  const messages = state.runs.flatMap((run) => {
    const lastAssistantMessageId = [...run.messages].reverse().find((message) => message.role === "assistant")?.id;
    return run.messages.map((message) =>
      toThreadMessage(message, run, { isLastRunMessage: message.id === lastAssistantMessageId }),
    );
  });
  if (state.error) messages.push(toSnapshotErrorMessage(state.error));
  const currentRun = state.current_run_id === null
    ? null
    : state.runs.find((run) => run.runId === state.current_run_id) ?? null;
  const hasPendingCommands = connectionMetadata.pendingCommands.length > 0;
  const currentRunIsTerminal = currentRun !== null
    && ["idle", "completed", "failed", "cancelled", "interrupted"].includes(currentRun.status);
  return {
    messages: [...messages, ...pendingMessages],
    // 服务端 Run 终态是唯一事实源，优先级高于 transport 的 sending 标记。
    // EOF/React 提交存在时序差异：如果 isSending 仍为 true，不能因此把
    // 已经 failed/cancelled 的 Run 重新投影为运行中。待发送命令仍然保留
    // sending 语义，以免新消息尚未创建 Run 时输入区提前恢复。
    isRunning: hasPendingCommands
      || (currentRun !== null ? !currentRunIsTerminal : connectionMetadata.isSending),
    // initial-state/recovery 入口已通过 parseTransportState 验证；这里仅按官方
    // AssistantTransportState 的 JSON state 字段透传，不构造或回写领域事实。
    state: state as unknown as ReadonlyJSONValue,
  };
}
