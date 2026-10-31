import { isSupportedImageFile, isUnsupportedImageFile, MAX_IMAGES_PER_MESSAGE } from "@/lib/assistant/attachments/attachment-upload";
import { desktopHost } from "@/lib/desktop-host";

export type ComposerFileProblem = {
  name: string;
  reason: "unsupported-image" | "local-path-required" | "add-failed" | "image-limit";
};

export type ComposerExternalFileResult<TAttachment> = {
  attachments: readonly TAttachment[];
  problems: readonly ComposerFileProblem[];
};

type AcceptedComposerFile =
  | { kind: "image"; file: File }
  | { kind: "file"; file: File; path: string };

/**
 * 附件按钮、粘贴、拖放共用的分类：支持的图片按字节上传；其余文件必须有本机路径（后端按路径读取）。
 *
 * 没有路径的普通文件只会是内存里的数据（如剪贴板里复制的内容、浏览器开发模式），只能拒绝。
 */
export function classifyComposerExternalFiles(files: readonly File[]) {
  const accepted: AcceptedComposerFile[] = [];
  const problems: ComposerFileProblem[] = [];

  for (const file of files) {
    if (isUnsupportedImageFile(file)) {
      problems.push({ name: file.name, reason: "unsupported-image" });
    } else if (isSupportedImageFile(file)) {
      accepted.push({ kind: "image", file });
    } else {
      const path = desktopHost?.files.pathOf(file) ?? "";
      if (path) accepted.push({ kind: "file", file, path });
      else problems.push({ name: file.name, reason: "local-path-required" });
    }
  }

  return { accepted, problems };
}

export function summarizeComposerFileProblems(problems: readonly ComposerFileProblem[]): string | null {
  if (problems.length === 0) return null;
  const unsupportedCount = problems.filter((problem) => problem.reason === "unsupported-image").length;
  const pathCount = problems.filter((problem) => problem.reason === "local-path-required").length;
  const failedCount = problems.filter((problem) => problem.reason === "add-failed").length;
  const limitCount = problems.filter((problem) => problem.reason === "image-limit").length;
  return [
    unsupportedCount > 0 && `图片附件仅支持 JPEG 和 PNG，已跳过 ${unsupportedCount} 个文件。`,
    pathCount > 0 && `${pathCount} 个文件不是本机文件（如剪贴板中的内容），无法作为附件。`,
    failedCount > 0 && `${failedCount} 个附件添加失败，请重试。`,
    limitCount > 0 && `一条消息最多 ${MAX_IMAGES_PER_MESSAGE} 张图片，已跳过 ${limitCount} 张。`,
  ].filter(Boolean).join(" ");
}
