import { uploadAttachment, type UploadedAttachment } from "@/lib/assistant/attachments/attachment-upload";
import type { UserAddMessageCommand } from "@/lib/assistant/converter";
import { prepareUserCommand } from "@/lib/assistant/prepare-user-command";
import { sendTaskMessage, type TaskMessageRequest } from "@/lib/assistant/task-commands";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { HttpError } from "@/lib/http/errors";
import type { TraceId } from "@/lib/trace";

type NewConversationImageAttachment = {
  file: File;
  name: string;
};

export type SubmitNewConversationMessageInput = {
  taskId: number;
  workspaceId: number;
  text: string;
  imageAttachments: readonly NewConversationImageAttachment[];
  banTools: readonly string[];
  modelConfigId: number;
  reasoningEffort: string | null;
  /** 同一次提交的上传与发送请求共用的 trace id。 */
  traceId: TraceId;
};

/**
 * 发送请求越过网络边界后结果未知（连接中断、2xx 回执无法解析等）：后端可能已经受理并新开 Run。
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
 * 只有「请求未发出（上传失败、附件失效）」和「HTTP 明确拒绝」两类失败可以断定命令没有落库；
 * 其余都可能已有 Run，调用方不得删除对应 Task。
 */
export function wasCommandPossiblyAccepted(error: unknown): boolean {
  return error instanceof NewConversationDeliveryUnknownError;
}

/**
 * 提交新对话首条消息，以 2xx 回执作为后端 accepted barrier。
 *
 * 本函数只负责附件预上传与请求序列化，不拥有 React 页面状态，也不订阅输出；调用方在成功返回后
 * 负责导航，在异常时按 `wasCommandPossiblyAccepted` 决定是否清理 provisional Task。新 Task 没有
 * 活动 Run，后端必然新开 Run，`delivery` 不起作用。异常语义：
 * - 上传失败 / 附件失效：原样抛出（HttpError、TimeoutError、UserFacingError），请求未发出。
 * - HTTP 非 2xx：抛 HttpError（含后端结构化原因），后端明确拒绝。
 * - 其余失败：抛 NewConversationDeliveryUnknownError。
 */
export async function submitNewConversationMessage(
  input: SubmitNewConversationMessageInput,
): Promise<void> {
  const uploadedImages: UploadedAttachment[] = [];
  for (const attachment of input.imageAttachments) {
    uploadedImages.push(await uploadAttachment(input.workspaceId, attachment.file, undefined, { traceId: input.traceId }));
  }

  const command = prepareUserCommand({
    type: "add-message",
    message: {
      role: "user",
      parts: [
        ...(input.text ? [{ type: "text" as const, text: input.text }] : []),
        ...uploadedImages.map((image) => ({
          type: "image" as const,
          image: image.locator,
        })),
      ],
    },
  }) as UserAddMessageCommand;
  try {
    await sendTaskMessage(input.taskId, {
      message: command.message as TaskMessageRequest["message"],
      modelConfigId: input.modelConfigId,
      reasoningEffort: input.reasoningEffort,
      banTools: [...input.banTools],
      delivery: "immediate",
    }, { traceId: input.traceId });
  } catch (error) {
    if (error instanceof HttpError) throw error;
    throw new NewConversationDeliveryUnknownError(error);
  }
}
