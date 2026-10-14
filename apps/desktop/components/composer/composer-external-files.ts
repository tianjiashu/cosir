import { isUnsupportedImageFile } from "@/lib/assistant/attachments/attachment-upload";

export type ComposerFileProblem = {
  name: string;
  reason: "unsupported-image" | "add-failed";
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
    } else {
      accepted.push(file);
    }
  }

  return { accepted, problems };
}

export function summarizeComposerFileProblems(problems: readonly ComposerFileProblem[]): string | null {
  if (problems.length === 0) return null;
  const unsupportedCount = problems.filter((problem) => problem.reason === "unsupported-image").length;
  const failedCount = problems.length - unsupportedCount;
  if (unsupportedCount > 0 && failedCount > 0) {
    return `${unsupportedCount} 个图片格式不支持，${failedCount} 个附件添加失败。`;
  }
  if (unsupportedCount > 0) return `图片附件仅支持 JPEG 和 PNG，已跳过 ${unsupportedCount} 个文件。`;
  return `${failedCount} 个附件添加失败，请重试。`;
}
