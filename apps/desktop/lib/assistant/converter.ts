import type { MessageStatus } from "@assistant-ui/core";
import type { CompleteAttachment, CreateAttachment } from "@assistant-ui/core";
import type {
  ThreadAssistantMessage,
  ThreadMessage,
  ThreadUserMessage,
} from "@assistant-ui/react";
import type { ReadonlyJSONObject, ReadonlyJSONValue } from "assistant-stream/utils";

import type {
  TransportError,
  TransportImagePart,
  TransportMessage,
  TransportReasoningPart,
  TransportRun,
  TransportState,
  TransportTextPart,
  TransportToolCallPart,
  TransportToolStatus,
} from "@/lib/assistant/contract";
import { getLocalAttachmentById, LOCAL_FILE_DATA_PREFIX } from "@/lib/assistant/attachments/local-attachment-registry";
import { localFileTokenIds } from "@/lib/assistant/attachments/local-file-token";
import {
  createEditableUserDocument,
  editableDocumentAttachments,
  type EditableUserDocument,
} from "@/lib/assistant/editable-user-document";

export type UserAddMessageCommand = {
  type: "add-message";
  sourceId?: string | null;
  parentId?: string | null;
  message: {
    role: "user";
    attachments?: ReadonlyArray<{
      id: string;
      name: string;
      contentType: string;
      path?: string | null;
    }>;
    parts: ReadonlyArray<
      | { type: "text"; text: string }
      | { type: "image"; image: string }
      // assistant-ui composer-only part. use-runtime-transport resolves it to
      // a local path embedded in text before the HTTP request is serialized.
      | { type: "file"; data: string; filename?: string; mimeType: string; sourceType?: "id" }
    >;
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

function hideLocalFileTokens(text: string): string {
  // Keep the token in the underlying editable text for edit/resend recovery,
  // but render it as an HTML comment so internal IDs never appear in chat UI.
  return text.replace(/\[\[cosir-(?:file|image):[^\]]+\]\]/g, (token) => `<!-- ${token} -->`);
}

function revealLocalFileTokens(text: string): string {
  return text.replace(/<!--\s*(\[\[cosir-(?:file|image):[^\]]+\]\])\s*-->/g, "$1");
}

function revealEditableFileTokens(text: string): string {
  return revealLocalFileTokens(text).replace(/\[\[cosir-image:[^\]]+\]\]/g, "");
}

function toImageAttachment(part: TransportImagePart, index: number): CompleteAttachment {
  return {
    id: part.image,
    type: "image" as const,
    name: `image-${index + 1}`,
    contentType: "image/*",
    content: [{ type: "image" as const, image: part.image }],
    status: { type: "complete" as const },
  };
}

function toFileAttachment(part: { file: string; id?: string; name: string; contentType: string }): CompleteAttachment | null {
  const localId = part.file.startsWith(LOCAL_FILE_DATA_PREFIX)
    ? part.file.slice(LOCAL_FILE_DATA_PREFIX.length)
    : undefined;
  const id = part.id ?? localId;
  // A file path or remote locator is content data, never an attachment identity.
  // Only explicit IDs and our local-file locator can be restored safely.
  if (!id) return null;
  return {
    // Restore must use the same registry ID that appears in the inline text
    // token, while the content keeps its local-file data locator.
    id,
    type: "file" as const,
    name: part.name,
    contentType: part.contentType,
    content: [{
      type: "file" as const,
      data: part.file,
      filename: part.name,
      mimeType: part.contentType,
      sourceType: "id" as const,
    }],
    status: { type: "complete" as const },
  };
}

export type EditableUserMessageDraft = {
  document: EditableUserDocument;
  text: string;
  attachments: readonly CompleteAttachment[];
};

function attachmentForUserPart(
  part: Extract<ThreadUserMessage["content"][number], { type: "file" | "image" }>,
  attachments: readonly CompleteAttachment[],
): CompleteAttachment | null {
  if (part.type === "image") {
    return attachments.find((attachment) => attachment.type === "image" && attachment.id === part.image)
      ?? toImageAttachment({ type: "image", image: part.image }, 0);
  }

  const localId = part.data.startsWith(LOCAL_FILE_DATA_PREFIX)
    ? part.data.slice(LOCAL_FILE_DATA_PREFIX.length)
    : undefined;
  return attachments.find((attachment) =>
    attachment.type !== "image"
      && (attachment.id === localId
        || attachment.content.some((content) => content.type === "file" && content.data === part.data)),
  ) ?? (localId ? toFileAttachment({
    id: localId,
    file: part.data,
    name: part.filename ?? "附件",
    contentType: part.mimeType,
  }) : null);
}

/**
 * Project a canonical user message into the text-plus-attachments shape used
 * by the edit composer.
 *
 * Canonical user content keeps files and images as ordered message parts so
 * the sent bubble can render them inline. assistant-ui's edit runtime lifts
 * those non-text parts into attachments, but it cannot retain their position
 * in the text and also combines them with `message.attachments`. This helper
 * creates one stable attachment per canonical non-text part and inserts each
 * ordinary file token at its original text position. Images are projected to
 * the separate preview surface. The caller owns
 * replacing the edit composer state with this result; this function performs
 * no runtime or persistence side effects.
 */
export function toEditableUserMessageDraft(message: ThreadUserMessage): EditableUserMessageDraft {
  const knownAttachmentTokens = new Set(
    message.content
      .filter((part): part is Extract<typeof part, { type: "text" }> => part.type === "text")
      .flatMap((part) => [...part.text.matchAll(/\[\[cosir-file:([^\]]+)\]\]/g)]
        .map((match) => `file:${match[1]}`)),
  );
  const orderedAttachments: CompleteAttachment[] = [];
  const seenAttachmentIds = new Set<string>();
  const textParts: string[] = [];
  let pendingTokens = "";
  let previousWasText = false;

  const addAttachment = (attachment: CompleteAttachment | null) => {
    if (!attachment || seenAttachmentIds.has(attachment.id)) return;
    seenAttachmentIds.add(attachment.id);
    orderedAttachments.push(attachment);
  };

  message.content.forEach((part, index) => {
    if (part.type === "text") {
      textParts.push(`${pendingTokens}${revealEditableFileTokens(part.text)}`);
      pendingTokens = "";
      previousWasText = true;
      return;
    }

    if (part.type !== "file" && part.type !== "image") {
      previousWasText = false;
      return;
    }

    const attachment = attachmentForUserPart(part, message.attachments);
    addAttachment(attachment);

    if (!attachment) {
      previousWasText = false;
      return;
    }

    // Images are rendered by the separate preview surface in edit mode. They
    // must not become inline text tokens, otherwise the contenteditable and
    // the preview surface represent the same attachment twice.
    if (part.type === "image") {
      previousWasText = false;
      return;
    }
    const tokenId = attachment.id;
    if (knownAttachmentTokens.has(`file:${tokenId}`)) {
      previousWasText = false;
      return;
    }
    const token = `[[cosir-file:${tokenId}]]`;
    if (previousWasText && textParts.length > 0) {
      textParts[textParts.length - 1] += token;
    } else if (message.content[index + 1]?.type === "text") {
      pendingTokens += token;
    } else {
      textParts.push(token);
    }
    previousWasText = false;
  });

  if (pendingTokens) textParts.push(pendingTokens);

  // Keep any attachment metadata not represented by content as a deterministic
  // fallback. Canonical messages should not normally take this branch, but it
  // prevents an edit from silently dropping an attachment if a partial
  // snapshot is observed during reconnect.
  for (const attachment of message.attachments) addAttachment(attachment);

  const document = createEditableUserDocument(textParts.join(""), orderedAttachments);
  return {
    document,
    // Transport text parts are the exact fragments around attachment parts;
    // inserting formatting separators here would move the attachment relative
    // to the user's original text.
    text: document.text,
    attachments: editableDocumentAttachments(document),
  };
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
          return toTextPart(
            message.role === "user" ? { ...part, text: hideLocalFileTokens(part.text) } : part,
            message.role,
          );
        case "reasoning":
          return toReasoningPart(part);
        case "tool-call":
          return toToolCallPart(part);
        case "image":
          return { type: "image", image: part.image };
        case "file":
          return {
            type: "file",
            data: part.file,
            filename: part.name,
            mimeType: part.contentType,
            sourceType: "id",
          };
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
      // Canonical user attachments live in ordered content parts. Keeping a
      // second top-level copy makes assistant-ui lift both sources and creates
      // duplicate edit attachments with different generated IDs.
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
  const messageParts: TransportMessage["parts"] = [];
  for (const part of command.message.parts) {
    if (part.type === "text") {
      if (part.text.length > 0) messageParts.push({ type: "text", text: part.text });
    } else if (part.type === "image") {
      messageParts.push({ type: "image", image: part.image });
    } else {
      messageParts.push({
        type: "file",
        file: part.data,
        name: part.filename ?? "附件",
        contentType: part.mimeType,
      });
    }
  }
  if (messageParts.length === 0) return null;
  const pending = toThreadMessage({
    id: `pending-${getOrCreateTransportCommandId(command)}`,
    role: "user",
    parts: messageParts,
  }, {
    runId: -1,
    status: "completed",
    endReason: null,
    messages: [],
    usage: null,
  });
  // The optimistic message follows the same single-source shape as the
  // canonical projection. The content parts already carry all attachments.
  return pending.role === "user" ? { ...pending, attachments: [] } : pending;
}

export function extractUserAddMessageAttachments(command: unknown): CreateAttachment[] {
  if (!isUserAddMessageCommand(command)) return [];
  const attachments = command.message.parts.flatMap((part, index) => {
    if (part.type === "image") return [toImageAttachment(part, index)];
    if (part.type === "file") {
      const attachment = toFileAttachment({
        file: part.data,
        name: part.filename ?? "附件",
        contentType: part.mimeType,
      });
      return attachment ? [attachment] : [];
    }
    return [];
  });
  const knownIds = new Set(attachments.map((attachment) => attachment.id));
  const localFiles = localFileTokenIds(extractUserAddMessageText(command)).flatMap((id) => {
    if (knownIds.has(id)) return [];
    const local = getLocalAttachmentById(id);
    if (!local) return [];
    knownIds.add(id);
    const attachment = toFileAttachment({
      id,
      file: `${LOCAL_FILE_DATA_PREFIX}${id}`,
      name: local.name,
      contentType: local.contentType,
    });
    return attachment ? [attachment] : [];
  });
  return [...attachments, ...localFiles];
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
