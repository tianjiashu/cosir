import type { InlineComposerAttachment } from "./composer-document";
import { classifyComposerExternalFiles, type ComposerExternalFileResult } from "./composer-external-files";
import { MAX_IMAGES_PER_MESSAGE } from "@/lib/assistant/attachments/attachment-upload";
import { registerLocalFile } from "@/lib/assistant/attachments/local-attachment-registry";
import { frontendLog } from "@/lib/logging/frontend-log";

/** 输入框的图片集合：新对话草稿或 assistant-ui composer 各自提供添加与当前数量。 */
export type ComposerImageSink = {
  add: (file: File) => void | Promise<void>;
  count: () => number;
};

/**
 * 所有附件入口共用分类与接纳顺序。普通文件登记本机路径后只返回 token，不进入图片附件集合；
 * 图片交给宿主添加，超出单条消息图片上限的按顺序跳过并记为问题。每个异步步骤后确认原编辑会话
 * 仍存在，避免迟到结果写入另一草稿。
 */
export async function addComposerFiles(
  files: readonly File[],
  port: { images: ComposerImageSink; isCurrent: () => boolean },
): Promise<ComposerExternalFileResult<InlineComposerAttachment>> {
  const { accepted, problems } = classifyComposerExternalFiles(files);
  const attachments: InlineComposerAttachment[] = [];
  // 开始时取一次余量：宿主的数量不一定在每次添加后同步更新（assistant-ui 的添加是异步的）。
  let remainingImages = MAX_IMAGES_PER_MESSAGE - port.images.count();
  for (const item of accepted) {
    if (!port.isCurrent()) return { attachments: [], problems: [] };
    if (item.kind === "file") {
      const { file, path } = item;
      const id = registerLocalFile({ path, name: file.name, contentType: file.type || "application/octet-stream" });
      attachments.push({ id, name: file.name });
      continue;
    }
    if (remainingImages <= 0) {
      problems.push({ name: item.file.name, reason: "image-limit" });
      continue;
    }
    try {
      await port.images.add(item.file);
      remainingImages -= 1;
    } catch (error) {
      problems.push({ name: item.file.name, reason: "add-failed" });
      void frontendLog("ERROR", "composer_attachment_intake_failed", "添加图片附件失败", {
        data: { name: item.file.name, contentType: item.file.type }, error,
      });
    }
  }
  return { attachments, problems };
}
