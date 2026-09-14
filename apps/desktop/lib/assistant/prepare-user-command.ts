import { isUserAddMessageCommand, type UserAddMessageCommand } from "@/lib/assistant/converter";
import {
  getLocalAttachmentById,
  LocalAttachmentUnavailableError,
} from "@/lib/assistant/attachments/local-attachment-registry";
import { LOCAL_FILE_TOKEN } from "@/lib/assistant/attachments/local-file-token";

/**
 * Convert composer-only ordinary-file tokens into local paths at the HTTP
 * boundary. The assistant-ui converter supplies text/image parts only, so the
 * token set in text is the source of truth for ordinary attachments.
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
    const text = part.text.replace(LOCAL_FILE_TOKEN, (_token, id: string) => {
      const path = getLocalAttachmentById(id)?.path;
      if (!path) throw new LocalAttachmentUnavailableError();
      return path;
    });
    parts.push({ type: "text", text });
  }
  return { ...command, message: { ...command.message, parts } } satisfies UserAddMessageCommand;
}
