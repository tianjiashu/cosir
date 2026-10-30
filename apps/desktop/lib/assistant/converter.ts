import { isActiveRunStatus } from "@/lib/assistant/contract";
import type { MessageStatus } from "@assistant-ui/core";
import type { CompleteAttachment, CreateAttachment, RunConfig } from "@assistant-ui/core";
import type {
  ThreadAssistantMessage,
  ThreadMessage,
  ThreadUserMessage,
} from "@assistant-ui/react";
import type { ReadonlyJSONObject } from "assistant-stream/utils";

import type {
  TransportActivity,
  TransportImagePart,
  TransportMessage,
  TransportMessagePart,
  TransportOutcome,
  TransportReasoningPart,
  TransportTextPart,
  TransportToolCallPart,
  TransportToolStatus,
} from "@/lib/assistant/contract";
import { getLocalAttachmentById, LOCAL_FILE_DATA_PREFIX } from "@/lib/assistant/attachments/local-attachment-registry";
import { localFileTokenIds, splitLocalFileTokens } from "@/lib/assistant/attachments/local-file-token";
import { materializeSkillMentions, restoreSkillMentionTokens } from "@/lib/assistant/skill-mention-token";
import {
  createEditableUserDocument,
  editableDocumentAttachments,
  type EditableUserDocument,
} from "@/lib/assistant/editable-user-document";

const EDIT_SESSION_KEY = "cosirEditSession";

/**
 * 写进编辑 composer 的 runConfig，标记「这个编辑会话已写入过草稿」。消息行重新挂载（虚拟列表滚动）
 * 时 assistant-ui 的编辑 composer（正文、runConfig）不变，据此继续而不是再用原消息覆盖用户已改的草稿；
 * 全新的编辑 composer 从线程 composer 复制 runConfig，不带这个标记。
 */
export const EDIT_SESSION_RUN_CONFIG: RunConfig = { custom: { [EDIT_SESSION_KEY]: true } };

export function isContinuedEditSession(runConfig: RunConfig | undefined): boolean {
  return runConfig?.custom?.[EDIT_SESSION_KEY] === true;
}

export type UserAddMessageCommand = {
  type: "add-message";
  commandId?: string;
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

/**
 * 渲染一条消息所需的上下文；由 frame store 按扁平消息列表与 `activity` 计算，不回查 Run。
 *
 * - `outcome`：该回复的结局（只在 Run 最后一条助手消息上、Run 终态后非空）。
 * - `isRunEnd`：该 Run 最后一条助手消息；回复末尾的用量、fork、诊断入口挂在这里。
 * - `activity`：消息是当前活动那一轮的回复末尾时为该活动（据此显示运行中与实时用量），否则 null。
 */
export type TransportMessageRenderContext = {
  /** 所属 Run；排队与乐观消息尚无 Run，为 null。只供诊断与日志。 */
  runId: number | null;
  outcome: TransportOutcome | null;
  isRunEnd: boolean;
  activity: TransportActivity | null;
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
      textParts.push(`${pendingTokens}${restoreSkillMentionTokens(part.text)}`);
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
    const token = `[[cosir-file:${attachment.id}]]`;
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
  const artifact = {
    backendStatus,
    presentation: part.presentation ?? {},
    display_data: displayData,
    team_run_id: part.team_run_id,
    // 失败 / 取消的短提示由后端给出（status_hint、「执行异常」「已中断」等），前端不重新推导；
    // 只在缺失时兜底，保证终态卡片总有一句提示。
    error: backendStatus === "cancelled"
      ? part.error || "已取消"
      : backendStatus === "failed" ? part.error || "执行失败" : null,
    errorCode: part.errorCode ?? null,
    child_task_id: part.child_task_id,
    agent_role: part.agent_role,
    deferred: part.deferred === true,
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
      // Transport state is never replaced by this projection.
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

/**
 * 助手消息的状态只看它自己的结局与当前活动：有 `outcome` 按结局；没有 `outcome` 且是当前活动
 * Run 的回复末尾、活动处于活动态时为运行中；其余（同一 Run 内较早的回复）都已完成。
 */
export function toAssistantMessageStatus(context: TransportMessageRenderContext): MessageStatus {
  const { outcome } = context;
  if (outcome !== null) {
    switch (outcome.status) {
      case "completed":
        return { type: "complete", reason: "stop" };
      case "cancelled":
        return { type: "incomplete", reason: "cancelled" };
      case "failed":
        if (outcome.endReason === "client_disconnected" || outcome.endReason === "cancelled") {
          return { type: "incomplete", reason: "cancelled" };
        }
        return {
          type: "incomplete",
          reason: "error",
          // 后端错误契约优先：模型 HTTP 失败时 message 可以直接来自响应体。
          // 没有 error 契约或文案为空白时回退到本地通用文案，避免渲染空白错误气泡。
          error: outcome.error?.message?.trim() ? outcome.error.message : "对话运行失败，请检查模型配置或后端状态。",
        };
    }
  }
  if (context.isRunEnd && isActiveRunStatus(context.activity?.status)) return { type: "running" };
  return { type: "complete", reason: "stop" };
}

function toThreadMessageWithContext(
  message: Pick<TransportMessage, "id" | "role" | "parts">,
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
        custom: { runId: context.runId },
      },
    };
    return userMessage;
  }

  const assistantMessage: ThreadAssistantMessage = {
    id: message.id,
    role: "assistant",
    content: content as ThreadAssistantMessage["content"],
    status: toAssistantMessageStatus(context),
    createdAt: new Date(),
    metadata: {
      unstable_state: null,
      unstable_annotations: [],
      unstable_data: [],
      steps: [],
      custom: {
        runId: context.runId,
        isRunEnd: context.isRunEnd,
        outcome: context.outcome,
        activity: context.activity,
      },
    },
  };
  return assistantMessage;
}

/** Project one canonical or queued message with its precomputed render context. */
export function toThreadMessageWithRenderContext(
  message: Pick<TransportMessage, "id" | "role" | "parts">,
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

/**
 * 乐观消息按后端 canonical 形状投影（见后端 `build_user_input_parts`）：正文里的普通文件 token
 * 在原位换成 file part（同一文件只占首次出现的位置），Skill token 物化成发送后的标记；
 * 没有 token 的 file part 追加在后。
 * 这样消息气泡与编辑草稿只处理一种形状，乐观消息被 canonical 替换时附件不挪位。
 */
export function toPendingUserMessage(command: unknown): ThreadMessage | null {
  if (!isUserAddMessageCommand(command)) return null;
  const toFilePart = (part: Extract<UserAddMessageCommand["message"]["parts"][number], { type: "file" }>): TransportMessagePart => ({
    type: "file",
    file: part.data,
    name: part.filename ?? "附件",
    contentType: part.mimeType,
  });
  const filesById = new Map(command.message.parts.flatMap((part) => (
    part.type === "file" && part.data.startsWith(LOCAL_FILE_DATA_PREFIX)
      ? [[part.data.slice(LOCAL_FILE_DATA_PREFIX.length), part] as const]
      : []
  )));
  const placed = new Set<object>();
  const placedIds = new Set<string>();
  const messageParts: TransportMessagePart[] = [];
  for (const part of command.message.parts) {
    if (part.type === "text") {
      for (const piece of splitLocalFileTokens(materializeSkillMentions(part.text))) {
        if (piece.type === "text") {
          messageParts.push({ type: "text", text: piece.text });
          continue;
        }
        if (placedIds.has(piece.token)) continue;
        placedIds.add(piece.token);
        const file = filesById.get(piece.token);
        if (file) {
          placed.add(file);
          messageParts.push(toFilePart(file));
          continue;
        }
        // 输入框里的普通文件只以 token 存在、命令不带 file part，名称从本机附件登记取；
        // 登记已失效也占住原位，后端会以「附件不可用」拒绝这条命令。
        const local = getLocalAttachmentById(piece.token);
        messageParts.push({
          type: "file",
          file: `${LOCAL_FILE_DATA_PREFIX}${piece.token}`,
          name: local?.name ?? "附件",
          contentType: local?.contentType ?? "application/octet-stream",
        });
      }
    } else if (part.type === "image") {
      messageParts.push({ type: "image", image: part.image });
    } else if (!placed.has(part)) {
      messageParts.push(toFilePart(part));
    }
  }
  if (messageParts.length === 0) return null;
  // 乐观消息只需要渲染上下文，不构造尚不存在的后端 Run 事实。
  const pending = toThreadMessageWithRenderContext({
    id: `pending-${getOrCreateTransportCommandId(command)}`,
    role: "user",
    parts: messageParts,
  }, { runId: null, outcome: null, isRunEnd: false, activity: null });
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
