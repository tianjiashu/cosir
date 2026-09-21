"use client";

import { MessagePrimitive, useAuiState } from "@assistant-ui/react";
import { Image } from "@/components/image";
import { UserMessageFilePart } from "@/components/assistant-ui/elements/attachment.aui";

/**
 * Render non-text parts of a user message as a separate attachment surface.
 *
 * Images use the shared Image element so loading, failed-resource handling,
 * transport locator resolution, and click-to-zoom remain consistent with the
 * rest of the application. This component owns only the message-level layout:
 * attachments are right-aligned above the text bubble and never participate in
 * the bubble's text wrapping. It does not upload, mutate, or persist files.
 */
export function UserMessageAttachments() {
  const hasAttachments = useAuiState((state) => state.message.parts.some(
    (part) => part.type === "image" || part.type === "file",
  ));

  if (!hasAttachments) return null;

  return (
    <div
      data-slot="user-message-attachments"
      className="flex max-w-full flex-wrap justify-end gap-2"
    >
      <MessagePrimitive.Parts>{({ part }) => {
        switch (part.type) {
          case "image":
            return (
              <Image
                type="image"
                image={part.image}
                status={{ type: "complete" }}
                display="message-thumbnail"
              />
            );
          case "file":
            return (
              <UserMessageFilePart
                filename={part.filename ?? "附件"}
                mimeType={part.mimeType}
              />
            );
          default:
            return null;
        }
      }}</MessagePrimitive.Parts>
    </div>
  );
}
