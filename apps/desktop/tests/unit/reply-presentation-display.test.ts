import { describe, expect, it } from "vitest";

import { readNextStep } from "@/components/conversation/tools/reply-presentation/reply-presentation-display";

// 目的：展示数据来自持久化的工具结果，形状不对时宁可不画卡片，也不能让回合渲染出错。
describe("回复呈现的展示数据", () => {
  it("下一步建议要有问题和至少一个按钮", () => {
    expect(readNextStep({ kind: "next-step", ask: "继续吗？", replies: ["好，继续", " ", 3, "先不用"] }))
      .toEqual({ ask: "继续吗？", replies: ["好，继续", "先不用"] });
    expect(readNextStep({ kind: "next-step", ask: "继续吗？", replies: [] })).toBeNull();
    expect(readNextStep({ kind: "next-step", replies: ["好"] })).toBeNull();
    expect(readNextStep({ kind: "user-question", ask: "继续吗？", replies: ["好"] })).toBeNull();
  });
});
