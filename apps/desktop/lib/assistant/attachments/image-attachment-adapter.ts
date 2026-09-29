import type {
  Attachment,
  AttachmentAdapter,
  CompleteAttachment,
  PendingAttachment,
} from "@assistant-ui/core";

import {
  isSupportedImageFile,
  isUnsupportedImageFile,
  uploadAttachment,
  type UploadedAttachment,
} from "./attachment-upload";
import {
  getLocalAttachment,
  getLocalAttachmentById,
  LOCAL_FILE_DATA_PREFIX,
  LocalAttachmentUnavailableError,
} from "./local-attachment-registry";
import { LOCAL_IMAGE_LOCATOR_PREFIX } from "./local-file-token";

const locatorFor = (assetId: string) => `${LOCAL_IMAGE_LOCATOR_PREFIX}${assetId}`;
const uploadByDigest = new Map<string, Promise<UploadedAttachment>>();

async function digestFile(file: File): Promise<string> {
  const bytes = await file.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, "0")).join("");
}

export function createAttachmentAdapter(workspaceId: number): AttachmentAdapter {
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
        type: isSupportedImageFile(file) ? "image" : "file",
        name: file.name,
        contentType: file.type,
        file,
        status: { type: "requires-action", reason: "composer-send" },
      } satisfies PendingAttachment;
    },
    async send(attachment: PendingAttachment): Promise<CompleteAttachment> {
      const local = getLocalAttachment(attachment.file) ?? getLocalAttachmentById(attachment.id);
      if (isUnsupportedImageFile(attachment.file)) {
        throw new Error("图片附件仅支持 JPEG 和 PNG 格式");
      }
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
      if (!isSupportedImageFile(attachment.file)) {
        throw new LocalAttachmentUnavailableError();
      }
      const digest = await digestFile(attachment.file);
      const cacheKey = `${workspaceId}:${digest}`;
      let upload = uploadByDigest.get(cacheKey);
      if (!upload) {
        upload = uploadAttachment(workspaceId, attachment.file);
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
    async remove(_attachment: Attachment) {
      // 图片是 workspace 级永久资源；移除动作只改变当前 Composer 的引用。
      // Run 编辑提交时会通过新的 image_asset_ids 重建 image_paths，不删除物理文件。
    },
  };
}
