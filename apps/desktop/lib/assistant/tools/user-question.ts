import type { ToolDecision } from "@/lib/assistant/session-commands";

/** `display_data.kind = "user-question"` 的载荷（后端 `core/tool/handler/question/`）。 */
export type QuestionOption = { label: string; description: string };
export type UserQuestion = { question: string; header: string; options: QuestionOption[]; multiSelect: boolean };
export type QuestionAnswer = string | string[];

export type UserQuestionDisplay = {
  questions: UserQuestion[];
  /** 作答后才有：`approve` 带回答，`reject` 是全部跳过。作废（未作答）时没有。 */
  decision?: "approve" | "reject";
  /** 以问题原文为键；跳过的题不出现。 */
  answers: Record<string, QuestionAnswer>;
  notes: Record<string, string>;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function readQuestion(value: unknown): UserQuestion | null {
  if (!isRecord(value) || typeof value.question !== "string" || typeof value.header !== "string" || !Array.isArray(value.options)) return null;
  const options: QuestionOption[] = [];
  for (const option of value.options) {
    if (!isRecord(option) || typeof option.label !== "string" || typeof option.description !== "string") return null;
    options.push({ label: option.label, description: option.description });
  }
  return { question: value.question, header: value.header, options, multiSelect: value.multi_select === true };
}

/** 校验展示载荷；形状不对返回 null，由注册表回落通用卡片。选项的 `preview` 首版不渲染，不读。 */
export function readUserQuestionDisplay(value: unknown): UserQuestionDisplay | null {
  if (!isRecord(value) || !Array.isArray(value.questions) || value.questions.length === 0) return null;
  const questions: UserQuestion[] = [];
  for (const item of value.questions) {
    const question = readQuestion(item);
    if (question === null) return null;
    questions.push(question);
  }
  const answers: Record<string, QuestionAnswer> = {};
  for (const [question, answer] of Object.entries(isRecord(value.answers) ? value.answers : {})) {
    if (typeof answer === "string") answers[question] = answer;
    else if (Array.isArray(answer)) answers[question] = answer.filter((item): item is string => typeof item === "string");
  }
  const notes: Record<string, string> = {};
  for (const [question, annotation] of Object.entries(isRecord(value.annotations) ? value.annotations : {})) {
    if (isRecord(annotation) && typeof annotation.notes === "string") notes[question] = annotation.notes;
  }
  return {
    questions,
    decision: value.decision === "approve" || value.decision === "reject" ? value.decision : undefined,
    answers,
    notes,
  };
}

/** 一题的作答草稿：选中的选项、「其他」是否选中与填的文字、批注。 */
export type QuestionDraft = { selected: string[]; otherSelected: boolean; other: string; notes: string };

export const EMPTY_QUESTION_DRAFT: QuestionDraft = { selected: [], otherSelected: false, other: "", notes: "" };

/** 点选一个选项：单选替换（并取消「其他」），多选切换。 */
export function toggleOption(question: UserQuestion, draft: QuestionDraft, label: string): QuestionDraft {
  if (!question.multiSelect) return { ...draft, selected: [label], otherSelected: false };
  const selected = draft.selected.includes(label) ? draft.selected.filter((item) => item !== label) : [...draft.selected, label];
  return { ...draft, selected };
}

/** 选中或取消「其他」：单选时与选项互斥。 */
export function toggleOther(question: UserQuestion, draft: QuestionDraft, on: boolean): QuestionDraft {
  return question.multiSelect ? { ...draft, otherSelected: on } : { ...draft, otherSelected: on, selected: on ? [] : draft.selected };
}

/** 草稿对应的回答；没答（没选、或选了「其他」却没填）返回 null。多选的自由文字排在选项之后。 */
export function draftAnswer(question: UserQuestion, draft: QuestionDraft | undefined): QuestionAnswer | null {
  if (!draft) return null;
  const other = draft.otherSelected ? draft.other.trim() : "";
  if (!question.multiSelect) return other || draft.selected[0] || null;
  // 按选项原有顺序给出，与点选先后无关。
  const labels = question.options.map((option) => option.label).filter((label) => draft.selected.includes(label));
  const answer = other && !labels.includes(other) ? [...labels, other] : labels;
  return answer.length > 0 ? answer : null;
}

/**
 * 把各题草稿组装成决定：答了的题进 `answers`（批注只附在答了的题上），一题都没答就是驳回（全部跳过）。
 * 形状由后端 `AskUserAnswers` 校验，这里只负责不产出它必然拒绝的东西。
 */
export function buildQuestionDecision(questions: UserQuestion[], drafts: Readonly<Record<string, QuestionDraft>>): ToolDecision {
  const answers: Record<string, QuestionAnswer> = {};
  const annotations: Record<string, { notes: string }> = {};
  for (const question of questions) {
    const draft = drafts[question.question];
    const answer = draftAnswer(question, draft);
    if (answer === null) continue;
    answers[question.question] = answer;
    const notes = draft?.notes.trim();
    if (notes) annotations[question.question] = { notes };
  }
  if (Object.keys(answers).length === 0) return { decision: "reject" };
  return { decision: "approve", data: { answers, annotations } };
}

/**
 * 提问卡片的预估高度（虚拟列表首次布局用，挂载后以实测为准）：页头色带、每个选项两行、「其他」一行与
 * 按钮行。只按当前显示的那一题算——卡片一次只显示一题；批注默认收起，不计。
 */
export function questionCardHeight(question: UserQuestion | undefined): number {
  return 74 + (question?.options.length ?? 2) * 59 + 41 + 50;
}

/** 回答的展示文字：多选各项用顿号连接。 */
export function answerText(answer: QuestionAnswer): string {
  return typeof answer === "string" ? answer : answer.join("、");
}
