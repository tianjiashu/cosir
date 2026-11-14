import { describe, expect, it } from "vitest";

import { readDeliverables, readNextStep } from "@/components/conversation/tools/reply-presentation/reply-presentation-display";

// 目的：展示数据来自持久化的工具结果，形状不对时宁可不画卡片，也不能让回合渲染出错。
describe("回复呈现的展示数据", () => {
  it("交付物丢弃形状不对的条目，一个都不剩时不画卡片", () => {
    expect(readDeliverables({ kind: "deliverables", files: [
      { path: "docs/a.md", absolute_path: "/w/docs/a.md", caption: "结论在第 2 节" },
      { path: "b.md", absolute_path: "/w/b.md", caption: null },
      { path: "c.md" },
      "d.md",
    ] })).toEqual([
      { path: "docs/a.md", absolutePath: "/w/docs/a.md", caption: "结论在第 2 节" },
      { path: "b.md", absolutePath: "/w/b.md", caption: null },
    ]);
    expect(readDeliverables({ kind: "deliverables", files: [{ path: "c.md" }] })).toBeNull();
    expect(readDeliverables({ kind: "file-changes", files: [] })).toBeNull();
    expect(readDeliverables(null)).toBeNull();
  });

  it("下一步建议要有问题和至少一个按钮", () => {
    expect(readNextStep({ kind: "next-step", ask: "继续吗？", replies: ["好，继续", " ", 3, "先不用"] }))
      .toEqual({ ask: "继续吗？", replies: ["好，继续", "先不用"] });
    expect(readNextStep({ kind: "next-step", ask: "继续吗？", replies: [] })).toBeNull();
    expect(readNextStep({ kind: "next-step", replies: ["好"] })).toBeNull();
    expect(readNextStep({ kind: "user-question", ask: "继续吗？", replies: ["好"] })).toBeNull();
  });
});
