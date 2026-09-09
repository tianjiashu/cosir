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
  TransportState,
  TransportTextPart,
  TransportToolCallPart,
  TransportToolStatus,
} from "@/lib/assistant/contract";

type UserAddMessageCommand = {
  type: "add-message";
  sourceId?: string | null;
  message: {
    role: "user";
    parts: ReadonlyArray<{ type: string; text?: string }>;
  };
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

function isUserAddMessageCommand(command: unknown): command is UserAddMessageCommand {
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

function sanitizeWebToolData(toolName: string, value: TransportToolCallPart["data"]): TransportToolCallPart["data"] {
  if (toolName === "web_extract" && (!value || value.kind !== "web-extract-status")) {
    return null;
  }
  if (toolName === "web_extract" && value && value.kind === "web-extract-status") {
    return {
      kind: "web-extract-status",
      provider: typeof value.provider === "string" ? value.provider : "",
      sites: Array.isArray(value.sites)
        ? value.sites.flatMap((site) => {
            if (typeof site !== "object" || site === null) return [];
            const candidate = site as Record<string, unknown>;
            if (typeof candidate.site !== "string" || typeof candidate.url !== "string") return [];
            const status = candidate.status;
            if (status !== "pending" && status !== "running" && status !== "success" && status !== "failed" && status !== "truncated") return [];
            return [{
              site: candidate.site,
              url: candidate.url,
              status,
              ...(typeof candidate.error_code === "string" ? { error_code: candidate.error_code } : {}),
              ...(candidate.truncated === true ? { truncated: true } : {}),
            }];
          })
        : [],
    };
  }
  return value;
}

/**
 * 把后端 tool-call part 映射为 assistant-ui part。
 *
 * `artifact` 是 UI-only 适配数据，保留后端五态、presentation、data 和错误；
 * 它不会被发送回后端，也不会成为对话事实。assistant-ui 的标准字段只承载
 * args/argsText/result/isError，renderer 从 artifact 读取后端展示声明。
 */
export function toToolCallPart(part: TransportToolCallPart): ThreadMessage["content"][number] {
  const backendStatus = normalizeToolStatus(part.status);
  const artifact = {
    backendStatus,
    presentation: part.presentation ?? {},
    data: sanitizeWebToolData(part.toolName, part.data ?? null),
    error: part.error ?? null,
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

  const safeResult = part.toolName === "web_search" || part.toolName === "web_extract" ? undefined : part.result;
  return {
    type: "tool-call",
    toolCallId: part.toolCallId,
    toolName: part.toolName,
    args: args as ReadonlyJSONObject,
    argsText: JSON.stringify(args, null, 2),
    artifact,
    ...(backendStatus === "completed" && safeResult !== undefined
      ? { result: safeResult, isError: part.isError ?? false }
      : {}),
  };
}

export function toMessageStatus(message: TransportMessage): MessageStatus {
  switch (message.status) {
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
      if (message.endReason === "client_disconnected" || message.endReason === "cancelled") {
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

export function toThreadMessage(
  message: TransportMessage,
  options: { isLastRunMessage?: boolean } = {},
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
      createdAt: message.createdAt ? new Date(message.createdAt) : new Date(),
      metadata: {
        unstable_state: undefined,
        unstable_annotations: undefined,
        unstable_data: undefined,
        steps: undefined,
        submittedFeedback: undefined,
        timing: undefined,
        custom: {
          runId: message.runId ?? null,
          isLastRunMessage: options.isLastRunMessage ?? false,
        },
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
      custom: {
        runId: message.runId ?? null,
        isLastRunMessage: options.isLastRunMessage ?? false,
      },
    },
  };
  return assistantMessage;
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

function toPendingUserMessage(command: UserAddMessageCommand): ThreadMessage | null {
  const parts = command.message.parts
    .filter((part): part is { type: "text"; text: string } => part.type === "text" && typeof part.text === "string" && part.text.length > 0)
    .map((part) => ({ type: "text" as const, text: part.text }));
  if (parts.length === 0) return null;
  return toThreadMessage({
    id: `pending-${getOrCreateTransportCommandId(command)}`,
    role: "user",
    status: "completed",
    endReason: null,
    parts,
    createdAt: new Date().toISOString(),
  });
}

function toSnapshotErrorMessage(error: TransportError): ThreadAssistantMessage {
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
  const lastAssistantMessageIds = new Set<string>();
  const seenRunIds = new Set<number>();
  for (const message of [...state.messages].reverse()) {
    if (message.role !== "assistant" || message.runId == null || seenRunIds.has(message.runId)) continue;
    seenRunIds.add(message.runId);
    lastAssistantMessageIds.add(message.id);
  }
  const messages = state.messages.map((message) =>
    toThreadMessage(message, { isLastRunMessage: lastAssistantMessageIds.has(message.id) }),
  );
  if (state.error) messages.push(toSnapshotErrorMessage(state.error));
  return {
    messages: [...messages, ...pendingMessages],
    // 未知 run 状态保持非成功的活动态，直到服务端给出明确终态；不能把未知值
    // 当作 idle，也不能在 EOF 后伪造 completed。
    isRunning: connectionMetadata.isSending || !["idle", "completed", "failed", "cancelled", "interrupted"].includes(state.run.status),
    // initial-state/recovery 入口已通过 parseTransportState 验证；这里仅按官方
    // AssistantTransportState 的 JSON state 字段透传，不构造或回写领域事实。
    state: state as unknown as ReadonlyJSONValue,
  };
}
