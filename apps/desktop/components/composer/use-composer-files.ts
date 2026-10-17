import { addComposerFiles } from "./assistant-composer-attachments";
import { summarizeComposerFileProblems } from "./composer-external-files";
import { useComposerFeedback } from "./composer-feedback";
import { insertAttachments, useComposerInsertion } from "./composer-insertion-context";
import { frontendLog } from "@/lib/logging/frontend-log";

/** 原生选择、粘贴、拖放使用同一接纳结果；反馈和 token 只交给仍可编辑的原会话。 */
export function useComposerFiles(addImage: (file: File) => void | Promise<void>) {
  const insertion = useComposerInsertion();
  const feedback = useComposerFeedback();
  return async (files: readonly File[]) => {
    const result = await addComposerFiles(files, { addImage, isCurrent: insertion.isAvailable });
    if (!insertion.isAvailable()) return;
    insertAttachments(insertion.dispatch, result.attachments);
    const message = summarizeComposerFileProblems(result.problems);
    if (message) {
      feedback.show(message);
      void frontendLog("WARNING", "composer_files_rejected", "部分附件未接纳", { data: { problems: result.problems } });
    } else feedback.clear();
  };
}
