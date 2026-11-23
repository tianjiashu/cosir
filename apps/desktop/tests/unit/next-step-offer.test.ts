import { describe, expect, it } from "vitest";

import { readNextStepOffer } from "@/lib/assistant/tools/next-step-offer";

// 目的：展示数据来自持久化的工具结果，形状不对时宁可不画卡片，也不能让回合渲染出错。
describe("下一步建议的展示数据", () => {
  it("要有问题、回顾和至少一个按钮", () => {
    expect(readNextStepOffer({ kind: "next-step", ask: "继续吗？", replies: ["好，继续", " ", 3, "先不用"], recap: "做完了。" }))
      .toEqual({ ask: "继续吗？", replies: ["好，继续", "先不用"], recap: "做完了。" });
    expect(readNextStepOffer({ kind: "next-step", ask: "继续吗？", replies: [], recap: "做完了。" })).toBeNull();
    expect(readNextStepOffer({ kind: "next-step", replies: ["好"], recap: "做完了。" })).toBeNull();
    expect(readNextStepOffer({ kind: "next-step", ask: "继续吗？", replies: ["好"] })).toBeNull();
    expect(readNextStepOffer({ kind: "user-question", ask: "继续吗？", replies: ["好"], recap: "做完了。" })).toBeNull();
    expect(readNextStepOffer(null)).toBeNull();
  });
});
