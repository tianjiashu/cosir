import { getUserAddMessageSourceId, isUserAddMessageCommand, type UserAddMessageCommand } from "@/lib/assistant/converter";
import {
  getLocalAttachmentById,
  LocalAttachmentUnavailableError,
} from "@/lib/assistant/attachments/local-attachment-registry";
import { localFileTokenIds } from "@/lib/assistant/attachments/local-file-token";
import { materializeSkillMentions } from "@/lib/assistant/skill-mention-token";

/**
 * Preserve composer-only ordinary-file tokens at the HTTP boundary and attach
 * the selected local-file descriptors explicitly. The backend resolves paths
 * and persists the binding with the message (inbox body, then context
 * metadata), so a reloaded WebView does not depend on the in-memory local
 * attachment registry.
 *
 * 缺失登记的 token 按命令类型分流：
 * - 编辑重跑（带 sourceId）：后端会从目标消息的展示绑定恢复路径，token 原样保留。
 * - 新消息：后端没有可恢复的绑定，必然以「附件不可用」拒绝；这里提前抛
 *   LocalAttachmentUnavailableError（UserFacingError），请求不会发出，调用方据此提示用户重新选择附件。
 *   以前静默丢弃描述符，用户只能看到后端的英文拒绝原因。
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
    parts.push({ type: "text", text: materializeSkillMentions(part.text) });
  }
  const isEdit = getUserAddMessageSourceId(command) !== null;
  const attachments = localFileTokenIds(
    parts.filter((part): part is { type: "text"; text: string } => part.type === "text")
      .map((part) => part.text)
      .join("\n"),
  ).flatMap((id) => {
    const local = getLocalAttachmentById(id);
    if (!local?.path) {
      if (isEdit) return [];
      throw new LocalAttachmentUnavailableError();
    }
    // 图片随 image part 发送，不作为普通文件描述符。
    if (local.kind !== "file") return [];
    return [{
      id: local.id,
      name: local.name,
      contentType: local.contentType,
      path: local.path,
    }];
  });
  if (attachments.length === 0) {
    return { ...command, message: { ...command.message, parts } } satisfies UserAddMessageCommand;
  }
  return { ...command, message: { ...command.message, parts, attachments } } satisfies UserAddMessageCommand;
}
