import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react";
import { useCallback, useState, type KeyboardEvent } from "react";

import { ToolIcon } from "@/components/conversation/tools/tool-icons";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Kbd } from "@/components/ui/kbd";
import { Textarea } from "@/components/ui/textarea";
import { commandPending, decisionTarget, useCommandRequest } from "@/lib/assistant/command-request-store";
import { submitToolDecision } from "@/lib/assistant/session-commands";
import { toolHeading, toolIconName } from "@/lib/assistant/tools/tool-catalog";
import {
  buildQuestionDecision, draftAnswer, EMPTY_QUESTION_DRAFT, readUserQuestionDisplay, toggleOption, toggleOther,
  type QuestionDraft, type UserQuestion,
} from "@/lib/assistant/tools/user-question";
import {
  clearQuestionDraft, setQuestionDraft, setQuestionIndex, useQuestionDraftStore, useQuestionProgress,
} from "@/lib/assistant/turn/question-draft-store";
import { HttpError } from "@/lib/http/http-error";
import { formatKeys } from "@/lib/keyboard/shortcuts";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { cn } from "@/lib/utils";
import type { CallViewContext } from "../call-view";
import { AwaitingFrame } from "./awaiting-frame";

const CHOICE_CLASS = "accent-primary mt-0.5 size-4 shrink-0 cursor-pointer focus-ring";
/** 选项行：整行可点，行间以分隔线隔开；选中行带底色。 */
const OPTION_ROW_CLASS = "border-border/60 flex min-w-0 cursor-pointer items-start gap-3 border-t px-4 py-2.5 hover:bg-muted/50 has-checked:bg-muted/60";
/** 页头色带与卡片边框同一个信息蓝：问题是卡片的标题，选项区用普通底色，读起来不像一整块告警。 */
const HEADER_CLASS = "flex min-w-0 items-start gap-3 bg-[color-mix(in_oklab,var(--info)_7%,transparent)] px-4 py-3";

/** 提交各题草稿：答了的题作为回答，一题都没答就是全部跳过。结果随 snapshot 回推，卡片收成一行调用。 */
async function submitAnswers(sessionId: number, toolCallId: string, questions: UserQuestion[]): Promise<void> {
  const drafts = useQuestionDraftStore.getState().sessions[sessionId]?.[toolCallId]?.drafts ?? {};
  const decision = buildQuestionDecision(questions, drafts);
  try {
    await submitToolDecision(sessionId, toolCallId, decision);
    clearQuestionDraft(sessionId, toolCallId);
    void frontendLog("INFO", "user_question_answered", "用户回答了提问", { data: { sessionId, toolCallId, decision: decision.decision } });
  } catch (error) {
    // 409：提问已作答或已作废（如刚发出的消息让它作废），结果随 snapshot 同步，不打扰用户。
    if (!(error instanceof HttpError && error.status === 409)) {
      void frontendLog("WARNING", "user_question_answer_failed", "提交回答失败", { data: { sessionId, toolCallId }, error });
      notify.error("提交回答失败", { error, fallback: "请稍后重试" });
    }
  }
}

function Choice({ multi, name, checked, onChange }: { multi: boolean; name: string; checked: boolean; onChange: (checked: boolean) => void }) {
  return multi
    ? <Checkbox className="mt-0.5" checked={checked} onChange={(event) => onChange(event.target.checked)} />
    : <input type="radio" name={name} className={CHOICE_CLASS} checked={checked} onChange={(event) => onChange(event.target.checked)} />;
}

/** 一题的可作答形态：选项与固定的「其他」一行（选中即可输入）。问题文字在卡片页头，这里只给读屏一个组名。 */
function QuestionForm({ question, draft, name, onChange }: { question: UserQuestion; draft: QuestionDraft; name: string; onChange: (change: (draft: QuestionDraft) => QuestionDraft) => void }) {
  return (
    <fieldset aria-label={question.question} className="flex min-w-0 flex-col">
      {question.options.map((option) => (
        <label key={option.label} className={OPTION_ROW_CLASS}>
          <Choice
            multi={question.multiSelect}
            name={name}
            checked={draft.selected.includes(option.label)}
            onChange={() => onChange((current) => toggleOption(question, current, option.label))}
          />
          <span className="min-w-0">
            <span className="block text-sm break-words">{option.label}</span>
            <span className="text-muted-foreground mt-0.5 block break-words">{option.description}</span>
          </span>
        </label>
      ))}
      <label className={OPTION_ROW_CLASS}>
        <Choice
          multi={question.multiSelect}
          name={name}
          checked={draft.otherSelected}
          onChange={(checked) => onChange((current) => toggleOther(question, current, checked))}
        />
        <span className="min-w-0 flex-1">
          <span className="block text-sm">其他</span>
          {draft.otherSelected && (
            <input
              type="text"
              // 选中「其他」就是为了填它。
              autoFocus
              aria-label="其他回答"
              placeholder="输入你的回答"
              value={draft.other}
              onChange={(event) => onChange((current) => ({ ...current, other: event.target.value }))}
              className="border-input bg-background placeholder:text-muted-foreground focus-visible:border-ring mt-1.5 h-8 w-full rounded-md border px-2 text-sm outline-none"
            />
          )}
        </span>
      </label>
    </fieldset>
  );
}

/** 只读面（阅读器）只展示问了什么，不提供作答。 */
function QuestionPreview({ question }: { question: UserQuestion }) {
  return (
    <ul className="flex min-w-0 flex-col">
      {question.options.map((option) => (
        <li key={option.label} className="border-border/60 border-t px-4 py-2.5">
          <span className="block text-sm break-words">{option.label}</span>
          <span className="text-muted-foreground mt-0.5 block break-words">{option.description}</span>
        </li>
      ))}
    </ul>
  );
}

/**
 * 提问（`ask_user`）等待作答时的卡片，照 Claude Code 的提问卡片：页头色带里是短标签（`header`）与问题，
 * 多题时右侧「1 / 3」与左右箭头翻页；每次显示一题，选项各占一行，批注收在「添加批注」后面；「跳过」不答这一题，「下一题」到下一题，最后一题变为「提交」
 * （⌘↩ / Ctrl+Enter）。一题都没答就提交等于全部跳过。选中不等于提交：提交后 Run 接着干，收不回来。
 *
 * 作答进度（翻到第几题、各题草稿）在 `question-draft-store`，不在组件里：虚拟列表卸载再挂载不丢。
 * 输入框照常可用——用户不理卡片直接发消息，提问由后端作废，卡片随 snapshot 收成「未作答」的一行。
 */
export function QuestionCard({ sessionId, toolCallId, toolName, args, artifact, interactive }: CallViewContext) {
  const display = readUserQuestionDisplay(artifact.display_data);
  const progress = useQuestionProgress(sessionId, toolCallId);
  const submitting = commandPending(useCommandRequest(sessionId, decisionTarget(toolCallId)));
  const { title } = toolHeading(toolName, args, artifact.intent);
  const questions = display?.questions ?? [];
  const index = Math.min(progress.index, Math.max(questions.length - 1, 0));
  const question = questions[index];
  const last = index === questions.length - 1;
  const draft = (question && progress.drafts[question.question]) || EMPTY_QUESTION_DRAFT;
  const answered = question !== undefined && draftAnswer(question, draft) !== null;

  // 用户点过「添加批注」的那一题（换题后回到收起）。
  const [notesFor, setNotesFor] = useState<string | null>(null);
  const notesRequested = question !== undefined && notesFor === question.question;
  const setNotesRequested = (on: boolean) => setNotesFor(on && question ? question.question : null);

  const advance = useCallback(() => {
    if (last) void submitAnswers(sessionId, toolCallId, questions);
    else setQuestionIndex(sessionId, toolCallId, index + 1);
  }, [index, last, questions, sessionId, toolCallId]);

  if (question === undefined) return null;

  const skip = () => {
    // 跳过就是不答这一题：清掉已选的，批注也不带。
    setQuestionDraft(sessionId, toolCallId, question.question, () => EMPTY_QUESTION_DRAFT);
    advance();
  };
  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key !== "Enter" || !(event.metaKey || event.ctrlKey) || !answered || submitting) return;
    event.preventDefault();
    advance();
  };

  // 批注默认收起：多数回答不需要它。写过内容就一直展开（草稿在 store，卸载再挂载仍在）。
  const notesOpen = notesRequested || draft.notes !== "";

  return (
    <AwaitingFrame toolCallId={toolCallId} title={title} className="bg-background max-w-[40rem] overflow-hidden p-0">
      <div data-slot="question-card" className="flex min-w-0 flex-col" onKeyDown={interactive ? onKeyDown : undefined}>
        <div className={HEADER_CLASS}>
          <div className="min-w-0 flex-1">
            <div className="text-info flex items-center gap-1.5 text-xs">
              <ToolIcon name={toolIconName(toolName)} className="size-3.5" aria-hidden="true" />
              <span data-slot="question-header" className="truncate">{question.header}</span>
              {question.multiSelect && <span className="text-muted-foreground shrink-0">· 可多选</span>}
            </div>
            <p className="text-chat mt-1 leading-6 font-semibold break-words">{question.question}</p>
          </div>
          {questions.length > 1 && (
            <div className="text-muted-foreground flex shrink-0 items-center gap-0.5">
              <Button type="button" size="icon-xs" variant="ghost" aria-label="上一题" disabled={index === 0} onClick={() => setQuestionIndex(sessionId, toolCallId, index - 1)}>
                <ChevronLeftIcon />
              </Button>
              <span data-slot="question-position" className="px-0.5 tabular-nums">{index + 1} / {questions.length}</span>
              <Button type="button" size="icon-xs" variant="ghost" aria-label="下一题" disabled={last} onClick={() => setQuestionIndex(sessionId, toolCallId, index + 1)}>
                <ChevronRightIcon />
              </Button>
            </div>
          )}
        </div>
        {interactive ? (
          <>
            <QuestionForm
              // 换题时重建：单选的 name、「其他」输入框的自动聚焦都按题。
              key={question.question}
              question={question}
              draft={draft}
              name={`${toolCallId}:${index}`}
              onChange={(change) => setQuestionDraft(sessionId, toolCallId, question.question, change)}
            />
            {notesOpen && (
              <div className="border-border/60 border-t px-4 py-2.5">
                <Textarea
                  autoFocus={notesRequested}
                  aria-label="批注"
                  placeholder="给这一题的回答加一句说明"
                  value={draft.notes}
                  onChange={(event) => setQuestionDraft(sessionId, toolCallId, question.question, (current) => ({ ...current, notes: event.target.value }))}
                  className="bg-background min-h-8 px-2 py-1 text-sm md:text-sm"
                />
              </div>
            )}
            <div className="border-border/60 flex items-center gap-1.5 border-t px-4 py-2.5">
              {!notesOpen && (
                <button type="button" className="text-muted-foreground hover:text-foreground focus-ring rounded-sm" onClick={() => setNotesRequested(true)}>
                  添加批注
                </button>
              )}
              <span className="flex-1" aria-hidden="true" />
              <Button type="button" size="sm" variant="outline" disabled={submitting} onClick={skip}>跳过</Button>
              <Button type="button" size="sm" disabled={!answered || submitting} onClick={advance} className={cn(submitting && "cursor-progress")}>
                {submitting ? "提交中…" : last ? "提交" : "下一题"}
                {!submitting && <Kbd className="bg-transparent text-current opacity-70">{formatKeys("mod+enter")}</Kbd>}
              </Button>
            </div>
          </>
        ) : <QuestionPreview question={question} />}
      </div>
    </AwaitingFrame>
  );
}
