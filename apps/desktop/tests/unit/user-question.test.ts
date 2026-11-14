import { describe, expect, it } from "vitest";

import {
  answerText, buildQuestionDecision, draftAnswer, EMPTY_QUESTION_DRAFT, readUserQuestionDisplay, toggleOption, toggleOther,
  type QuestionDraft, type UserQuestion,
} from "@/lib/assistant/tools/user-question";

const single: UserQuestion = {
  question: "用哪种存储？", header: "存储", multiSelect: false,
  options: [{ label: "Cookie", description: "后端下发" }, { label: "本地", description: "实现最快" }],
};
const multi: UserQuestion = { ...single, question: "哪些情况失效？", header: "过期", multiSelect: true };

function draft(overrides: Partial<QuestionDraft>): QuestionDraft {
  return { ...EMPTY_QUESTION_DRAFT, ...overrides };
}

describe("readUserQuestionDisplay", () => {
  const questions = [{ question: single.question, header: single.header, options: single.options, multi_select: false }];

  it("等待时只有问题；作答后带决定、回答与批注", () => {
    expect(readUserQuestionDisplay({ kind: "user-question", questions })).toEqual({ questions: [single], decision: undefined, answers: {}, notes: {} });
    expect(readUserQuestionDisplay({
      kind: "user-question", questions, decision: "approve",
      answers: { [single.question]: "Cookie" }, annotations: { [single.question]: { notes: "别动数据库" } },
    })).toMatchObject({ decision: "approve", answers: { [single.question]: "Cookie" }, notes: { [single.question]: "别动数据库" } });
  });

  it("形状不对返回 null，交给通用卡片", () => {
    expect(readUserQuestionDisplay({ kind: "user-question", questions: [] })).toBeNull();
    expect(readUserQuestionDisplay({ kind: "user-question", questions: [{ question: "Q?" }] })).toBeNull();
    expect(readUserQuestionDisplay(null)).toBeNull();
  });
});

describe("作答草稿", () => {
  it("单选：选项互斥，选「其他」后以填的文字为回答", () => {
    let current = toggleOption(single, EMPTY_QUESTION_DRAFT, "Cookie");
    current = toggleOption(single, current, "本地");
    expect(draftAnswer(single, current)).toBe("本地");
    current = { ...toggleOther(single, current, true), other: " 签名头 " };
    expect(current.selected).toEqual([]);
    expect(draftAnswer(single, current)).toBe("签名头");
  });

  it("多选：按选项原有顺序给出，自由文字排在最后", () => {
    let current = toggleOption(multi, EMPTY_QUESTION_DRAFT, "本地");
    current = toggleOption(multi, current, "Cookie");
    current = { ...toggleOther(multi, current, true), other: "改密码后" };
    expect(draftAnswer(multi, current)).toEqual(["Cookie", "本地", "改密码后"]);
    expect(draftAnswer(multi, toggleOption(multi, current, "Cookie"))).toEqual(["本地", "改密码后"]);
  });

  it("没选、或选了「其他」却没填，都算没答", () => {
    expect(draftAnswer(single, undefined)).toBeNull();
    expect(draftAnswer(single, draft({ otherSelected: true, other: "  " }))).toBeNull();
    expect(draftAnswer(multi, EMPTY_QUESTION_DRAFT)).toBeNull();
  });
});

describe("buildQuestionDecision", () => {
  it("答了的题进 answers，批注只附在答了的题上，没答的题不出现", () => {
    expect(buildQuestionDecision([single, multi], {
      [single.question]: draft({ selected: ["Cookie"], notes: " 别动数据库 " }),
      [multi.question]: draft({ notes: "没选但写了批注" }),
    })).toEqual({
      decision: "approve",
      data: { answers: { [single.question]: "Cookie" }, annotations: { [single.question]: { notes: "别动数据库" } } },
    });
  });

  it("一题都没答就是驳回（全部跳过），不带载荷", () => {
    expect(buildQuestionDecision([single, multi], {})).toEqual({ decision: "reject" });
  });
});

it("answerText 把多选各项用顿号连接", () => {
  expect(answerText("Cookie")).toBe("Cookie");
  expect(answerText(["Cookie", "本地"])).toBe("Cookie、本地");
});
