import { answerText, readUserQuestionDisplay, type UserQuestionDisplay } from "@/lib/assistant/tools/user-question";
import { QuestionCard } from "../awaiting/question-card";
import type { CallView } from "../call-view";
import { DetailSurface } from "./detail-parts";

/** 每题的问与答：答了的写回答与批注，没答的标「未回答」。 */
function QuestionAnswers({ display }: { display: UserQuestionDisplay }) {
  return (
    <DetailSurface>
      <dl className="flex flex-col gap-1.5">
        {display.questions.map((question) => {
          const answer = display.answers[question.question];
          const notes = display.notes[question.question];
          return (
            <div key={question.question} className="min-w-0">
              <dt className="text-muted-foreground break-words">{question.question}</dt>
              <dd className="break-words">
                {answer === undefined ? <span className="text-muted-foreground">未回答</span> : answerText(answer)}
                {notes && <span className="text-muted-foreground"> · {notes}</span>}
              </dd>
            </div>
          );
        })}
      </dl>
    </DetailSurface>
  );
}

/**
 * `user-question`（`ask_user`）：等待时就地是提问卡片（`QuestionCard`）；结算后变回一行，元信息区分
 * 「已作答」与「已跳过」（用户主动全部跳过），展开看每题的问与答。作废的提问（用户直接发了消息）
 * 状态是取消，行上显示后端短提示「未作答」，同样可展开看问了什么。
 */
export const userQuestionView: CallView = (data, context) => {
  const display = readUserQuestionDisplay(data);
  if (!display) return null;
  if (context.artifact.awaitingUser) return { awaiting: () => <QuestionCard {...context} /> };
  const answered = display.decision === "approve";
  return {
    meta: display.decision === undefined ? undefined : answered ? "已作答" : "已跳过",
    detail: { render: () => <QuestionAnswers display={display} /> },
  };
};
