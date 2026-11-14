import { create } from "zustand";

import { EMPTY_QUESTION_DRAFT, type QuestionDraft } from "@/lib/assistant/tools/user-question";

/** 一次提问的作答进度：翻到第几题、各题草稿（以问题原文为键）。 */
export type QuestionProgress = { index: number; drafts: Readonly<Record<string, QuestionDraft>> };

const EMPTY_PROGRESS: QuestionProgress = { index: 0, drafts: {} };

type QuestionDraftState = {
  /** sessionId → toolCallId → 进度。 */
  sessions: Readonly<Record<number, Readonly<Record<string, QuestionProgress>>>>;
};

/**
 * 提问卡片的作答草稿，按 Session 与调用分桶，只在内存里。
 *
 * 为什么不放组件局部 state：对话是虚拟列表，用户往上翻去核对上下文时卡片会被卸载，答到一半的内容就丢了；
 * 切到别的会话再回来也一样。不进 localStorage：草稿不是偏好，也不该跨重启。提交成功后清掉；被作废的提问
 * 留下的草稿不再有人读，随页面生命周期释放。
 */
export const useQuestionDraftStore = create<QuestionDraftState>(() => ({ sessions: {} }));

export function useQuestionProgress(sessionId: number, toolCallId: string): QuestionProgress {
  return useQuestionDraftStore((state) => state.sessions[sessionId]?.[toolCallId] ?? EMPTY_PROGRESS);
}

function update(sessionId: number, toolCallId: string, change: (progress: QuestionProgress) => QuestionProgress): void {
  useQuestionDraftStore.setState((state) => {
    const bucket = state.sessions[sessionId] ?? {};
    return { sessions: { ...state.sessions, [sessionId]: { ...bucket, [toolCallId]: change(bucket[toolCallId] ?? EMPTY_PROGRESS) } } };
  });
}

export function setQuestionIndex(sessionId: number, toolCallId: string, index: number): void {
  update(sessionId, toolCallId, (progress) => ({ ...progress, index }));
}

export function setQuestionDraft(sessionId: number, toolCallId: string, question: string, change: (draft: QuestionDraft) => QuestionDraft): void {
  update(sessionId, toolCallId, (progress) => ({
    ...progress,
    drafts: { ...progress.drafts, [question]: change(progress.drafts[question] ?? EMPTY_QUESTION_DRAFT) },
  }));
}

export function clearQuestionDraft(sessionId: number, toolCallId: string): void {
  useQuestionDraftStore.setState((state) => {
    const bucket = state.sessions[sessionId];
    if (!bucket || !(toolCallId in bucket)) return state;
    const rest = Object.fromEntries(Object.entries(bucket).filter(([id]) => id !== toolCallId));
    return { sessions: { ...state.sessions, [sessionId]: rest } };
  });
}
