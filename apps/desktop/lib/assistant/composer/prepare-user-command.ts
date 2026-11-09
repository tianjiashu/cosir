import { isUserAddMessageCommand, type UserAddMessageCommand } from "@/lib/assistant/transport-message-converter";
import {
  getLocalAttachmentById,
  LocalAttachmentUnavailableError,
} from "@/lib/assistant/attachments/local-attachment-registry";
import { localFileTokenIds } from "@/lib/assistant/attachments/local-file-token";

/**
 * Preserve composer-only ordinary-file tokens at the HTTP boundary and attach
 * the selected local-file descriptors explicitly. The backend resolves paths
 * and persists the binding with the message (message_queue body, then context
 * metadata), so a reloaded WebView does not depend on the in-memory local
 * attachment registry.
 *
 * 每个 token 都必须有带路径的登记：新选的文件在选择时登记，放回输入框的历史附件按快照里的路径
 * 登记（`restoreLocalFile`）。缺失登记时后端必然以「附件不可用」拒绝，这里提前抛
 * LocalAttachmentUnavailableError（UserFacingError），请求不会发出，调用方据此提示用户重新选择附件。
 */
export function prepareUserCommand(command: unknown): unknown {
  if (!isUserAddMessageCommand(command)) return command;
  const parts: Array<{ type: "text"; text: string } | { type: "image"; image: string }> = [];
  for (const part of command.message.parts) {
    if (part.type === "file") continue;
    if (part.type === "image") {
      parts.push(part);
      continue;
    }
    // Skill token 原样发送：后端据此校验 Skill 并把正文固化进模型输入。
    parts.push({ type: "text", text: part.text });
  }
  const attachments = localFileTokenIds(
    parts.filter((part): part is { type: "text"; text: string } => part.type === "text")
      .map((part) => part.text)
      .join("\n"),
  ).map((id) => {
    const local = getLocalAttachmentById(id);
    if (!local) throw new LocalAttachmentUnavailableError();
    return { id: local.id, name: local.name, contentType: local.contentType, path: local.path };
  });
  if (attachments.length === 0) {
    return { ...command, message: { ...command.message, parts } } satisfies UserAddMessageCommand;
  }
  return { ...command, message: { ...command.message, parts, attachments } } satisfies UserAddMessageCommand;
}
