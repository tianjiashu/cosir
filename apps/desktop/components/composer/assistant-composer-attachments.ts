import type { FileCapsule } from "@/lib/assistant/capsules";
import { classifyComposerExternalFiles, type ComposerExternalFileResult } from "./composer-external-files";
import { MAX_IMAGES_PER_MESSAGE } from "@/lib/assistant/attachments/attachment-upload";
import { frontendLog } from "@/lib/logging/frontend-log";

/** 输入框的图片集合：新对话草稿或 assistant-ui composer 各自提供添加与当前数量。 */
export type ComposerImageSink = {
  add: (file: File) => void | Promise<void>;
  count: () => number;
};

/**
 * 所有附件入口共用分类与接纳顺序。本机普通文件直接成为 `<file>` 胶囊（路径就是正文里的指针，
 * 后端按路径读取，前端不再登记附件元数据）；图片交给宿主添加，超出单条消息图片上限的按顺序跳过
 * 并记为问题。每个异步步骤后确认原编辑会话仍存在，避免迟到结果写入另一草稿。
 */
export async function addComposerFiles(
  files: readonly File[],
  port: { images: ComposerImageSink; isCurrent: () => boolean },
): Promise<ComposerExternalFileResult<FileCapsule>> {
  const { accepted, problems } = classifyComposerExternalFiles(files);
  const capsules: FileCapsule[] = [];
  // 开始时取一次余量：宿主的数量不一定在每次添加后同步更新（assistant-ui 的添加是异步的）。
  let remainingImages = MAX_IMAGES_PER_MESSAGE - port.images.count();
  for (const item of accepted) {
    if (!port.isCurrent()) return { capsules: [], problems: [] };
    if (item.kind === "file") {
      capsules.push({ kind: "file", path: item.path });
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
  return { capsules, problems };
}
