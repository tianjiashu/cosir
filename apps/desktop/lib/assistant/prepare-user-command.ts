import { isUserAddMessageCommand, type UserAddMessageCommand } from "@/lib/assistant/converter";
import { getLocalAttachmentById } from "@/lib/assistant/attachments/local-attachment-registry";
import { localFileTokenIds } from "@/lib/assistant/attachments/local-file-token";

/**
 * Preserve composer-only ordinary-file tokens at the HTTP boundary and attach
 * the selected local-file descriptors explicitly. The backend resolves paths
 * and persists the binding in Run.extra so a reloaded WebView does not depend
 * on the in-memory local attachment registry.
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
    parts.push({ type: "text", text: part.text });
  }
  const attachments = localFileTokenIds(
    parts.filter((part): part is { type: "text"; text: string } => part.type === "text")
      .map((part) => part.text)
      .join("\n"),
  ).flatMap((id) => {
    const local = getLocalAttachmentById(id);
    if (!local || local.kind !== "file") return [];
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
