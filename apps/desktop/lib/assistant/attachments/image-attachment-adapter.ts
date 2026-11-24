import type {
  AttachmentAdapter,
  CompleteAttachment,
  PendingAttachment,
} from "@assistant-ui/core";

import { isSupportedImageFile } from "./attachment-upload";
import { holdPendingImage } from "./pending-images";
import { UserFacingError } from "@/lib/errors/user-facing-error";

/**
 * 输入框的图片附件：只负责准入与把待发图片交给提交。
 *
 * 不在这里上传：assistant-ui 的 `send` 把上传放在输入框里等，上传中的消息就归属于随页面销毁的输入框，切走
 * Session 后失败放不回原文、成功后草稿里又留着已发的图（见 `pending-images`）。这里把文件登记成待上传图片、
 * 立即交出完整附件，消息随即离开输入框，上传由 Session 级的提交在 POST 前完成。
 */
export function createAttachmentAdapter(): AttachmentAdapter {
  return {
    // 完整历史图片使用 image/*；原始 File 的格式限制在 add 边界处理。
    accept: "image/*,.png,.jpg,.jpeg",
    async add({ file }) {
      if (!isSupportedImageFile(file)) throw new UserFacingError("图片附件仅支持 JPEG 和 PNG 格式");
      return {
        id: `pending-attachment:${crypto.randomUUID()}`,
        type: "image",
        name: file.name,
        contentType: file.type,
        file,
        status: { type: "requires-action", reason: "composer-send" },
      } satisfies PendingAttachment;
    },
    async send(attachment: PendingAttachment): Promise<CompleteAttachment> {
      return {
        // UI 身份贯穿加入、移除与发送；待上传图片的地址只在 content 里。
        id: attachment.id,
        type: "image",
        name: attachment.name,
        contentType: attachment.contentType,
        status: { type: "complete" },
        content: [{ type: "image", image: holdPendingImage(attachment.file) }],
      };
    },
    async remove() {
      // 图片是 workspace 级永久资源；移除动作只改变当前 Composer 的引用。
      // Run 编辑提交时会通过新的 image_asset_ids 重建 image_paths，不删除物理文件。
    },
  };
}
