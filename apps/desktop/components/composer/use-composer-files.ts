import { addComposerFiles, type ComposerImageSink } from "./assistant-composer-attachments";
import { summarizeComposerFileProblems } from "./composer-external-files";
import { useComposerFeedback } from "./composer-feedback";
import { insertCapsules, useComposerInsertion } from "./composer-insertion-context";
import { frontendLog } from "@/lib/logging/frontend-log";

/** 附件按钮、粘贴、拖放的唯一接纳入口；反馈和胶囊只交给仍可编辑的原会话，迟到结果只记录。 */
export function useComposerFiles(images: ComposerImageSink) {
  const insertion = useComposerInsertion();
  const feedback = useComposerFeedback();
  return async (files: readonly File[]) => {
    const result = await addComposerFiles(files, { images, isCurrent: insertion.isAvailable });
    if (!insertion.isAvailable()) {
      void frontendLog("INFO", "composer_attachment_selection_discarded", "原输入会话已关闭或禁用，忽略迟到的附件", {
        data: { count: files.length },
      });
      return;
    }
    insertCapsules(insertion.dispatch, result.capsules);
    const message = summarizeComposerFileProblems(result.problems);
    if (message) {
      feedback.show(message);
      void frontendLog("WARNING", "composer_files_rejected", "部分附件未接纳", { data: { problems: result.problems } });
    } else feedback.clear();
  };
}
