import type { InlineComposerAttachment } from "./composer-document";
import { classifyComposerExternalFiles, type ComposerExternalFileResult } from "./composer-external-files";
import { registerLocalFile } from "@/lib/assistant/attachments/local-attachment-registry";
import { frontendLog } from "@/lib/logging/frontend-log";

/**
 * 所有附件入口共用分类与接纳顺序。普通文件登记本机路径后只返回 token，不进入图片附件集合；
 * 图片交给宿主添加。每个异步步骤后确认原编辑会话仍存在，避免迟到结果写入另一草稿。
 */
export async function addComposerFiles(
  files: readonly File[],
  port: { addImage: (file: File) => void | Promise<void>; isCurrent: () => boolean },
): Promise<ComposerExternalFileResult<InlineComposerAttachment>> {
  const { accepted, problems } = classifyComposerExternalFiles(files);
  const attachments: InlineComposerAttachment[] = [];
  for (const item of accepted) {
    if (!port.isCurrent()) return { attachments: [], problems: [] };
    if (item.kind === "file") {
      const { file, path } = item;
      const id = registerLocalFile({ path, name: file.name, contentType: file.type || "application/octet-stream" });
      attachments.push({ id, name: file.name });
      continue;
    }
    try {
      await port.addImage(item.file);
    } catch (error) {
      problems.push({ name: item.file.name, reason: "add-failed" });
      void frontendLog("ERROR", "composer_attachment_intake_failed", "添加图片附件失败", {
        data: { name: item.file.name, contentType: item.file.type }, error,
      });
    }
  }
  return { attachments, problems };
}
