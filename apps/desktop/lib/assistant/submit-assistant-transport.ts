import { requestRaw } from "@/lib/http/client";
import { parseHttpError } from "@/lib/http/errors";
import { uploadAttachment, type UploadedAttachment } from "@/lib/assistant/attachments/attachment-upload";
import { prepareUserCommand } from "@/lib/assistant/prepare-user-command";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import type { TraceId } from "@/lib/trace";

type NewConversationImageAttachment = {
  file: File;
  name: string;
};

export type SubmitAssistantTransportInput = {
  taskId: number;
  workspaceId: number;
  commandId: string;
  text: string;
  imageAttachments: readonly NewConversationImageAttachment[];
  banTools: readonly string[];
  modelConfigId: number;
  reasoningEffort: string | null;
  /** 同一次提交的上传、POST /assistant 共用的 trace id。 */
  traceId: TraceId;
};

class AssistantTransportProtocolError extends Error {
  public constructor(message: string) {
    super(message);
    this.name = "AssistantTransportProtocolError";
  }
}

/**
 * POST /assistant 在网络层失败（连接中断、请求被取消等）：请求可能已被后端受理，也可能没有。
 * 与 HTTP 非 2xx（后端明确拒绝）不同，调用方不能据此认定命令未提交。
 */
class AssistantTransportDeliveryUnknownError extends UserFacingError {
  public constructor(cause: unknown) {
    super("无法确认首条消息是否已送达本机后端，对话已保留，请稍后在侧栏中打开查看");
    this.name = "AssistantTransportDeliveryUnknownError";
    this.cause = cause;
  }
}

/**
 * 判断提交失败后后端是否可能已经受理了命令。
 *
 * 只有「请求未发出（上传失败、附件失效）」和「HTTP 明确拒绝」两类失败可以断定命令没有落库；
 * 其余（已越过 accepted barrier 但标识不符、网络层结果未知）都可能已有 Run，调用方不得删除对应 Task。
 */
export function wasCommandPossiblyAccepted(error: unknown): boolean {
  return error instanceof AssistantTransportProtocolError || error instanceof AssistantTransportDeliveryUnknownError;
}

/**
 * 提交新对话首条命令，并以 2xx 响应头作为后端 accepted barrier。
 *
 * 本函数只负责附件预上传、请求序列化和响应头校验，不拥有 React 页面状态，也不等待
 * SSE 完成；调用方在成功返回后负责导航，在异常时按 `wasCommandPossiblyAccepted`
 * 决定是否清理 provisional Task。异常语义：
 * - 上传失败 / 附件失效：原样抛出（HttpError、TimeoutError、UserFacingError），请求未发出。
 * - HTTP 非 2xx：抛 HttpError（含后端结构化原因），后端明确拒绝。
 * - 网络层失败：抛 AssistantTransportDeliveryUnknownError。
 * - 2xx 但 Task 标识不符：抛 AssistantTransportProtocolError。
 */
export async function submitAssistantTransport(
  input: SubmitAssistantTransportInput,
): Promise<void> {
  const uploadedImages: UploadedAttachment[] = [];
  for (const attachment of input.imageAttachments) {
    uploadedImages.push(await uploadAttachment(input.workspaceId, attachment.file, undefined, { traceId: input.traceId }));
  }

  const command = prepareUserCommand({
    type: "add-message",
    commandId: input.commandId,
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
  });
  const body = JSON.stringify({
    taskId: input.taskId,
    workspaceId: input.workspaceId,
    threadId: `task-${input.taskId}`,
    modelConfigId: input.modelConfigId,
    reasoningEffort: input.reasoningEffort,
    commands: [
      command,
      {
        type: "custom",
        name: "ban-tools",
        commandId: `${input.commandId}-ban-tools`,
        payload: { ban_tools: [...input.banTools] },
      },
    ],
  });
  let response: Response;
  try {
    response = await requestRaw("/assistant", {
      method: "POST",
      traceId: input.traceId,
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body,
    });
  } catch (error) {
    throw new AssistantTransportDeliveryUnknownError(error);
  }

  if (!response.ok) throw await parseHttpError(response);
  if (response.headers.get("x-cosir-task-id") !== String(input.taskId)) {
    throw new AssistantTransportProtocolError("Assistant Transport 响应的 Task 标识不匹配");
  }
  try {
    await response.body?.cancel();
  } catch {
    // accepted barrier 已经成立，订阅关闭失败不能被解释成业务拒绝。
  }
}
