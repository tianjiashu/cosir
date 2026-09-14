import type {
  Attachment,
  AttachmentAdapter,
  CompleteAttachment,
  PendingAttachment,
} from "@assistant-ui/core";

import { requestRaw } from "@/lib/http/client";
import { uploadAttachment, type UploadedImage } from "./attachment-upload";
import {
  getLocalAttachment,
  getLocalAttachmentById,
  LOCAL_FILE_DATA_PREFIX,
  LocalAttachmentUnavailableError,
} from "./local-attachment-registry";

const locatorFor = (assetId: string) => `cosir-attachment://${assetId}`;
const uploadByDigest = new Map<string, Promise<UploadedImage>>();

async function digestFile(file: File): Promise<string> {
  const bytes = await file.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, "0")).join("");
}

export function createAttachmentAdapter(taskId: number): AttachmentAdapter {
  return {
    // assistant-ui uses `*` as the all-file wildcard. `*/*` is parsed as a
    // literal MIME pattern and rejects otherwise valid image/jpeg files.
    accept: "*",
    async add({ file }) {
      const local = getLocalAttachment(file);
      return {
        // The inline composer token is keyed by this ID. Reuse the registry
        // ID so send, restore, and DOM rendering all address one attachment.
        id: local?.id ?? `pending-attachment:${crypto.randomUUID()}`,
        type: file.type.startsWith("image/") ? "image" : "file",
        name: file.name,
        contentType: file.type,
        file,
        status: { type: "requires-action", reason: "composer-send" },
      } satisfies PendingAttachment;
    },
    async send(attachment: PendingAttachment): Promise<CompleteAttachment> {
      const local = getLocalAttachment(attachment.file) ?? getLocalAttachmentById(attachment.id);
      if (local?.kind === "file") {
        return {
          id: local.id,
          type: "file",
          name: local.name,
          contentType: local.contentType,
          status: { type: "complete" },
          content: [{
            type: "file",
            data: `${LOCAL_FILE_DATA_PREFIX}${local.id}`,
            mimeType: local.contentType,
            filename: local.name,
            sourceType: "id",
          }],
        };
      }
      if (!attachment.file.type.startsWith("image/")) {
        throw new LocalAttachmentUnavailableError();
      }
      const digest = await digestFile(attachment.file);
      const cacheKey = `${taskId}:${digest}`;
      let upload = uploadByDigest.get(cacheKey);
      if (!upload) {
        upload = uploadAttachment(taskId, attachment.file);
        uploadByDigest.set(cacheKey, upload);
        void upload.catch(() => {
          if (uploadByDigest.get(cacheKey) === upload) uploadByDigest.delete(cacheKey);
        });
      }
      const uploaded = await upload;
      return {
        id: uploaded.id,
        type: "image",
        name: uploaded.name,
        contentType: uploaded.contentType,
        status: { type: "complete" },
        content: [{ type: "image", image: locatorFor(uploaded.id) }],
      };
    },
    async remove(attachment: Attachment) {
      if (attachment.type !== "image" || attachment.id.startsWith("pending-attachment:")) return;
      const response = await requestRaw(`/tasks/${taskId}/attachments/${encodeURIComponent(attachment.id)}`, {
        method: "DELETE",
      });
      if (!response.ok) throw new Error("删除附件失败");
    },
  };
}

/** Backwards-compatible export for callers that still use the old image-only name. */
export const createImageAttachmentAdapter = createAttachmentAdapter;
