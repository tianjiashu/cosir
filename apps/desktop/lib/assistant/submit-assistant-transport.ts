import { requestRaw } from "@/lib/http/client";
import { uploadAttachment, type UploadedAttachment } from "@/lib/assistant/attachments/attachment-upload";
import { prepareUserCommand } from "@/lib/assistant/prepare-user-command";

export type NewConversationImageAttachment = {
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
};

export class AssistantTransportProtocolError extends Error {
  public constructor(message: string) {
    super(message);
    this.name = "AssistantTransportProtocolError";
  }
}

/**
 * 提交新对话首条命令，并以 2xx 响应头作为后端 accepted barrier。
 *
 * 本函数只负责附件预上传、请求序列化和响应头校验，不拥有 React 页面状态，也不等待
 * SSE 完成；调用方在成功返回后负责导航，在异常时负责 provisional Task 清理。
 */
export async function submitAssistantTransport(
  input: SubmitAssistantTransportInput,
): Promise<void> {
  const uploadedImages: UploadedAttachment[] = [];
  for (const attachment of input.imageAttachments) {
    uploadedImages.push(await uploadAttachment(input.workspaceId, attachment.file));
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
  const response = await requestRaw("/assistant", {
    method: "POST",
    headers: {
      Accept: "text/event-stream",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
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
    }),
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`Status ${response.status}: ${body}`);
  }
  if (response.headers.get("x-cosir-task-id") !== String(input.taskId)) {
    throw new AssistantTransportProtocolError("Assistant Transport 响应的 Task 标识不匹配");
  }
  try {
    await response.body?.cancel();
  } catch {
    // accepted barrier 已经成立，订阅关闭失败不能被解释成业务拒绝。
  }
}
