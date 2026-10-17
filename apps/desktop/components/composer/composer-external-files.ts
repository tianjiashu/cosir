import { isSupportedImageFile, isUnsupportedImageFile } from "@/lib/assistant/attachments/attachment-upload";
import { getLocalAttachment } from "@/lib/assistant/attachments/local-attachment-registry";

export type ComposerFileProblem = {
  name: string;
  reason: "unsupported-image" | "local-path-required" | "add-failed";
};

export type ComposerExternalFileResult<TAttachment> = {
  attachments: readonly TAttachment[];
  problems: readonly ComposerFileProblem[];
};

export function classifyComposerExternalFiles(files: readonly File[]) {
  const accepted: File[] = [];
  const problems: ComposerFileProblem[] = [];

  for (const file of files) {
    if (isUnsupportedImageFile(file)) {
      problems.push({ name: file.name, reason: "unsupported-image" });
    } else if (!isSupportedImageFile(file) && !getLocalAttachment(file)?.path) {
      problems.push({ name: file.name, reason: "local-path-required" });
    } else {
      accepted.push(file);
    }
  }

  return { accepted, problems };
}

export function summarizeComposerFileProblems(problems: readonly ComposerFileProblem[]): string | null {
  if (problems.length === 0) return null;
  const unsupportedCount = problems.filter((problem) => problem.reason === "unsupported-image").length;
  const pathCount = problems.filter((problem) => problem.reason === "local-path-required").length;
  const failedCount = problems.filter((problem) => problem.reason === "add-failed").length;
  return [
    unsupportedCount > 0 && `图片附件仅支持 JPEG 和 PNG，已跳过 ${unsupportedCount} 个文件。`,
    pathCount > 0 && `${pathCount} 个普通文件无法取得本机路径，请使用附件按钮选择文件。`,
    failedCount > 0 && `${failedCount} 个附件添加失败，请重试。`,
  ].filter(Boolean).join(" ");
}
