import type {
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
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { newTraceId, type TraceId } from "@/lib/trace";

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
    /**
     * 发送前把待发附件解析为完整附件；失败时在这里提示用户。
     *
     * assistant-ui 的 `composer.send()` 公共 API 不返回 Promise，调用方无法 await 到附件失败，
     * 因此失败的唯一可靠观察点就是适配器本身：记录日志、toast 提示，再原样抛出让 assistant-ui
     * 中止本次发送——它会把文本放回输入框、保留待发附件并解除发送中状态，用户可以直接重发。
     * 上传有超时（attachment-upload），所以不会出现永不结束的发送状态。
     */
    async send(attachment: PendingAttachment): Promise<CompleteAttachment> {
      const traceId = newTraceId();
      try {
        return await resolveAttachment(workspaceId, attachment, traceId);
      } catch (error) {
        void frontendLog("ERROR", "composer_attachment_send_failed", "发送前附件解析失败，本次发送已中止", {
          traceId,
          data: { workspaceId, attachmentId: attachment.id, attachmentType: attachment.type },
          error,
        });
        notify.error(`附件「${attachment.name}」发送失败`, { error, fallback: "附件上传失败，请重试" });
        throw error;
      }
    },
    async remove() {
      // 图片是 workspace 级永久资源；移除动作只改变当前 Composer 的引用。
      // Run 编辑提交时会通过新的 image_asset_ids 重建 image_paths，不删除物理文件。
    },
  };
}

async function resolveAttachment(
  workspaceId: number,
  attachment: PendingAttachment,
  traceId: TraceId,
): Promise<CompleteAttachment> {
  const local = getLocalAttachment(attachment.file) ?? getLocalAttachmentById(attachment.id);
  if (isUnsupportedImageFile(attachment.file)) {
    throw new UserFacingError("图片附件仅支持 JPEG 和 PNG 格式");
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
    upload = uploadAttachment(workspaceId, attachment.file, undefined, { traceId });
    uploadByDigest.set(cacheKey, upload);
    // 失败（含超时）的上传不能留在缓存里，否则重发会直接拿到同一个失败结果。
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
}
