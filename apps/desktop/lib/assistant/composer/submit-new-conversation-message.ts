import { uploadAttachment, type UploadedAttachment } from "@/lib/assistant/attachments/attachment-upload";
import { sendSessionMessage, type SessionMessageRequest } from "@/lib/assistant/session-commands";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { HttpError } from "@/lib/http/http-error";
import type { TraceId } from "@/lib/logging/trace-id";

type NewConversationImageAttachment = {
  file: File;
  name: string;
};

export type SubmitNewConversationMessageInput = {
  sessionId: number;
  workspaceId: number;
  /** 正文原文；机器可读的 `<file>` / `<ref>` / `<skill>` 标签原样交给后端解析。 */
  text: string;
  imageAttachments: readonly NewConversationImageAttachment[];
  disabledToolGroups: readonly string[];
  modelConfigId: string;
  reasoningEffort: string | null;
  /** 同一次提交的上传与发送请求共用的 trace id。 */
  traceId: TraceId;
};

/**
 * 发送请求越过网络边界后结果未知（连接中断等）：后端可能已经受理并新开 Run。
 * 与 HTTP 非 2xx（后端明确拒绝）不同，调用方不能据此认定命令未提交。
 */
class NewConversationDeliveryUnknownError extends UserFacingError {
  public constructor(cause: unknown) {
    super("无法确认首条消息是否已送达本机后端，对话已保留，请稍后在侧栏中打开查看");
    this.name = "NewConversationDeliveryUnknownError";
    this.cause = cause;
  }
}

/**
 * 判断提交失败后后端是否可能已经受理了命令。
 *
 * 只有「请求未发出（上传失败）」和「HTTP 明确拒绝」两类失败可以断定命令没有落库；
 * 其余都可能已有 Run，调用方不得删除对应 Session。
 */
export function wasCommandPossiblyAccepted(error: unknown): boolean {
  return error instanceof NewConversationDeliveryUnknownError;
}

/**
 * 提交新会话首条消息，以 2xx 作为后端已受理的边界（新 Run 已启动，输出经 Session 长订阅到达）。
 *
 * 本函数只负责图片预上传与请求组装，不拥有 React 页面状态，也不订阅输出；调用方在成功返回后
 * 负责导航，在异常时按 `wasCommandPossiblyAccepted` 决定是否清理 provisional Session。新 Session 没有
 * 活动 Run，后端必然立即新开 Run。异常语义：
 * - 上传失败：原样抛出（HttpError、TimeoutError、UserFacingError），请求未发出。
 * - HTTP 非 2xx：抛 HttpError（含后端结构化原因），后端明确拒绝。
 * - 其余失败：抛 NewConversationDeliveryUnknownError。
 */
export async function submitNewConversationMessage(
  input: SubmitNewConversationMessageInput,
): Promise<void> {
  const uploadedImages: UploadedAttachment[] = [];
  for (const attachment of input.imageAttachments) {
    uploadedImages.push(await uploadAttachment(input.workspaceId, attachment.file, input.traceId));
  }

  const message: SessionMessageRequest["message"] = {
    role: "user",
    parts: [
      ...(input.text ? [{ type: "text" as const, text: input.text }] : []),
      ...uploadedImages.map((image) => ({
        type: "image" as const,
        image: image.locator,
      })),
    ],
  };
  try {
    await sendSessionMessage(input.sessionId, {
      message,
      modelConfigId: input.modelConfigId,
      reasoningEffort: input.reasoningEffort,
      disabledToolGroups: [...input.disabledToolGroups],
    }, { traceId: input.traceId });
  } catch (error) {
    if (error instanceof HttpError) throw error;
    throw new NewConversationDeliveryUnknownError(error);
  }
}
