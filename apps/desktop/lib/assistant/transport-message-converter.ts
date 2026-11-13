import { projectTurn, turnPhaseOf, type TurnLayout } from "./turn/turn-layout";
import { isActiveRunStatus } from "@/lib/assistant/contract";
import type { MessageStatus } from "@assistant-ui/core";
import type { CompleteAttachment, CreateAttachment } from "@assistant-ui/core";
import type {
  ThreadAssistantMessage,
  ThreadMessage,
  ThreadUserMessage,
} from "@assistant-ui/react";
import type { ReadonlyJSONObject } from "assistant-stream/utils";

import type {
  BackgroundTaskEntry,
  TransportActivity,
  TransportImagePart,
  TransportMessage,
  TransportMessagePart,
  TransportMessageSource,
  TransportOutcome,
  TransportTextPart,
  TransportToolCallPart,
} from "@/lib/assistant/contract";
import type { ToolArtifact } from "@/lib/assistant/tools/tool-artifact";
import {
  createEditableUserDocument,
  editableDocumentAttachments,
  type EditableUserDocument,
} from "@/lib/assistant/composer/editable-user-document";

export type UserAddMessageCommand = {
  type: "add-message";
  message: {
    role: "user";
    /** 正文（带 `<file>` / `<ref>` / `<skill>` 胶囊标签）与已上传图片的 locator，无旁挂附件。 */
    parts: ReadonlyArray<{ type: "text"; text: string } | { type: "image"; image: string }>;
  };
};

/**
 * 渲染一条消息所需的上下文；由 frame store 按扁平消息列表与 `activity` 计算，前端看不到 Run。
 *
 * - `outcome`：Run 的结局（只在它最后一个可见回合的回复上、Run 终态后非空）；用量与诊断挂在这里。
 * - `activity`：消息是当前回复（`activity.turn` 那一回合的回复）时为该活动（据此显示运行中与实时用量），
 *   否则 null。
 */
export type TransportMessageRenderContext = {
  /** 所在回合；排队与乐观消息还没有回合，为 null。诊断与上下文观测按它进入。 */
  turn: number | null;
  /** 来自分叉继承的历史：不可交互。 */
  inherited: boolean;
  /** Agent 消息的发送方（见 `TransportMessage.source`）；排队、乐观与助手消息为 null。 */
  source: TransportMessageSource | null;
  outcome: TransportOutcome | null;
  activity: TransportActivity | null;
  backgroundTasks?: readonly BackgroundTaskEntry[];
};

const optimisticCommandIds = new WeakMap<object, string>();

/**
 * 为一条乐观命令提供稳定的消息标识（`toPendingUserMessage` 据此生成乐观消息 id）。
 *
 * WeakMap 保证同一条 command 每次转换都得到同一个 id，乐观消息在重渲染间保持身份；
 * UUID 保证页面重载后重新创建的模块实例不会从相同序列重新开始。
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
  return { type: "text", text: part.text, providerMetadata: { cosir: { finalReply: part.finalReply === true } }, status: role === "user" && part.status === undefined ? { type: "complete" } : toPartStatus(part.status) };
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

export type EditableUserMessageDraft = {
  document: EditableUserDocument;
  text: string;
  attachments: readonly CompleteAttachment[];
};

/** 图片 part 对应的可放回输入框的附件；图片仍是附件条，不进正文。 */
function toImageAttachmentForPart(part: Extract<ThreadUserMessage["content"][number], { type: "image" }>, attachments: readonly CompleteAttachment[]): CompleteAttachment {
  return attachments.find((attachment) => attachment.type === "image" && attachment.id === part.image)
    ?? toImageAttachment({ type: "image", image: part.image }, 0);
}

/**
 * 把 canonical 用户消息投影成「正文 + 图片附件」草稿形状，供撤回与编辑排队消息放回输入框。
 *
 * canonical 正文里的 `<file>` / `<ref>` / `<skill>` 标签就是胶囊指针：编辑器装载时按同一套语法
 * 解析成原子节点，这里不搬附件、也不重建 token 顺序。图片由输入框的附件条承载。
 */
export function toEditableUserMessageDraft(message: ThreadUserMessage): EditableUserMessageDraft {
  const images: CompleteAttachment[] = [];
  const seenIds = new Set<string>();
  const addImage = (attachment: CompleteAttachment) => {
    if (seenIds.has(attachment.id)) return;
    seenIds.add(attachment.id);
    images.push(attachment);
  };
  const textParts: string[] = [];

  for (const part of message.content) {
    if (part.type === "text") {
      textParts.push(part.text);
      continue;
    }
    if (part.type === "image") addImage(toImageAttachmentForPart(part, message.attachments));
  }
  // 快照与 assistant-ui 消息模型都可能带图片附件，同一 locator 只保留一份。
  for (const attachment of message.attachments) {
    if (attachment.type === "image") addImage(attachment);
  }

  // Transport 文本 part 是正文的精确片段，插入分隔符会改变用户原文。
  const text = textParts.join("");
  const document = createEditableUserDocument(text, images);
  return { document, text, attachments: editableDocumentAttachments(document) };
}

/**
 * 把后端 tool-call part 映射为 assistant-ui part。
 *
 * `artifact`（形状见 `tool-artifact.ts`）是 UI-only 适配数据，保留后端五态、等待用户与展示数据；它不会
 * 被发送回后端，也不会成为对话事实。assistant-ui 的标准字段只承载 args/argsText/isError。
 */
export function toToolCallPart(part: TransportToolCallPart): ThreadMessage["content"][number] {
  let converted = toolCallParts.get(part);
  if (!converted) {
    converted = convertToolCallPart(part);
    toolCallParts.set(part, converted);
  }
  return converted;
}

// Transport mutation 保留未变 part 的身份。正文更新不能重建同一消息中已存在的工具 part/artifact。
const toolCallParts = new WeakMap<TransportToolCallPart, ThreadMessage["content"][number]>();

function convertToolCallPart(part: TransportToolCallPart): ThreadMessage["content"][number] {
  const backendStatus = part.status;
  const artifact: ToolArtifact = {
    backendStatus,
    intent: part.intent ?? null,
    presentation: part.presentation,
    awaitingUser: part.awaitingUser,
    display_data: part.display_data ?? null,
    team_run_id: part.team_run_id,
    // 失败 / 取消的短提示由后端给出（status_hint、「执行异常」「已中断」等），前端不重新推导；
    // 只在缺失时兜底，保证终态卡片总有一句提示。
    error: backendStatus === "cancelled"
      ? part.error || "已取消"
      : backendStatus === "failed" ? part.error || "执行失败" : null,
    errorCode: part.errorCode ?? null,
    childTaskId: part.child_task_id,
    agent_role: part.agent_role,
    background: part.background !== undefined,
    startedAt: part.startedAt ?? undefined,
  };
  const args = part.args ?? {};
  // 各状态的公共字段；分支只补 result / isError。未完成态（pending / running）不得带 result 或 isError 键。
  const base = {
    type: "tool-call" as const,
    toolCallId: part.toolCallId,
    toolName: part.toolName,
    args: args as ReadonlyJSONObject,
    argsText: JSON.stringify(args, null, 2),
    artifact,
  };

  if (backendStatus === "failed") {
    return {
      ...base,
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
      ...base,
      // See the failed branch above. Never place the backend exception or
      // tool output in this sentinel.
      result: { kind: "tool-terminal", status: "cancelled" },
      isError: false,
    };
  }

  return {
    ...base,
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
        return {
          type: "incomplete",
          reason: "error",
          // 后端错误契约优先：模型 HTTP 失败时 message 可以直接来自响应体。
          // 没有 error 契约或文案为空白时回退到本地通用文案，避免渲染空白错误气泡。
          error: outcome.error?.message?.trim() ? outcome.error.message : "对话运行失败，请检查模型配置或后端状态。",
        };
    }
  }
  if (isActiveRunStatus(context.activity?.status)) return { type: "running" };
  return { type: "complete", reason: "stop" };
}

/** Project one canonical or queued message with its precomputed render context. */
export function toThreadMessageWithRenderContext(
  message: Pick<TransportMessage, "id" | "role" | "parts">,
  context: TransportMessageRenderContext,
  turnLayout: TurnLayout | null = message.role === "assistant" ? projectTurn(message.parts, turnPhaseOf(context.activity), context.outcome, null, context.activity, context.backgroundTasks) : null,
): ThreadMessage {
  const content = message.parts
    .map((part) => {
      switch (part.type) {
        case "text":
          return toTextPart(part, message.role);
        case "tool-call":
          return toToolCallPart(part);
        case "image":
          return { type: "image", image: part.image };
        case "agent-message":
          // 只占下标：回合按 canonical part 下标读内容，来信行直接读 `TurnLayout`。
          return { type: "data", name: part.type, data: null };
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
        custom: { source: context.source },
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
        turnLayout,
        turn: context.turn,
        inherited: context.inherited,
        outcome: context.outcome,
        activity: context.activity,
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

/**
 * 乐观消息与后端 canonical 同形：正文（含胶囊标签）与图片 part 原样投影，不再把文件 token
 * 换成 file part。胶囊由展示侧按同一套标签语法渲染，canonical 到达时内容不挪位。
 */
export function toPendingUserMessage(command: unknown): ThreadMessage | null {
  if (!isUserAddMessageCommand(command)) return null;
  const messageParts: TransportMessagePart[] = command.message.parts.map((part) => ({ ...part }));
  if (messageParts.length === 0) return null;
  // 乐观消息只需要渲染上下文，不构造尚不存在的后端 Run 事实。
  const pending = toThreadMessageWithRenderContext({
    id: `pending-${getOrCreateTransportCommandId(command)}`,
    role: "user",
    parts: messageParts,
  }, { turn: null, inherited: false, source: null, outcome: null, activity: null });
  pending.metadata.custom.pending = true;
  return pending;
}

/** 失败/撤回时能放回输入框的附件：只有图片；正文里的胶囊随文本一起恢复。 */
export function extractUserAddMessageAttachments(command: unknown): CreateAttachment[] {
  if (!isUserAddMessageCommand(command)) return [];
  return command.message.parts.flatMap((part, index) =>
    part.type === "image" ? [toImageAttachment(part, index)] : [],
  );
}
