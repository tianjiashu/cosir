import { describe, expect, it } from "vitest";

import { serializeCapsule, type QuoteCapsule } from "@/lib/assistant/capsules";
import { mergeDraftText } from "@/lib/assistant/composer/edit-composer-draft";
import { joinQuoteCards, splitQuoteCards } from "@/lib/assistant/composer/quote-cards";

const quote = (turn: number, note?: string): QuoteCapsule => ({
  kind: "quote", text: `第 ${turn} 回合\n原文`, ...(note ? { note } : {}),
});

describe("输入框正文与摘录卡片的拆合", () => {
  it("卡片在前各占一行、正文在后；拆开再合回原样（撤回、草稿都只认正文）", () => {
    const text = joinQuoteCards([quote(2, "是吗"), quote(3)], '整体再看看 <ref path="a.ts" />');
    expect(text).toBe(`${serializeCapsule(quote(2, "是吗"))}\n${serializeCapsule(quote(3))}\n整体再看看 <ref path="a.ts" />`);
    expect(splitQuoteCards(text)).toEqual({ quotes: [quote(2, "是吗"), quote(3)], rest: '整体再看看 <ref path="a.ts" />' });
    expect(joinQuoteCards(splitQuoteCards(text).quotes, splitQuoteCards(text).rest)).toBe(text);
  });

  it("只有卡片、正文为空", () => {
    const text = joinQuoteCards([quote(2)], "");
    expect(splitQuoteCards(text)).toEqual({ quotes: [quote(2)], rest: "" });
  });

  it("正文里不在开头的摘录（如撤回回来的正文里手写的标签）按出现顺序归入卡片", () => {
    const text = `${joinQuoteCards([quote(2)], "正文")}${serializeCapsule(quote(5, "新的"))}`;
    expect(splitQuoteCards(text)).toEqual({ quotes: [quote(2), quote(5, "新的")], rest: "正文" });
  });

  it("没有摘录时正文原样", () => {
    expect(splitQuoteCards("\n第一行")).toEqual({ quotes: [], rest: "\n第一行" });
  });
});

describe("失败恢复时合并草稿", () => {
  it("两边的摘录合进卡片区（恢复的在前），只拼其余正文：恢复正文以没闭合的代码块结尾也不吞掉草稿里的摘录", () => {
    const recovered = joinQuoteCards([quote(1)], "```ts\nconst a = 1;");
    const current = joinQuoteCards([quote(2, "草稿里的")], "接着写");
    const merged = mergeDraftText(recovered, current);
    expect(splitQuoteCards(merged)).toEqual({ quotes: [quote(1), quote(2, "草稿里的")], rest: "```ts\nconst a = 1;\n\n接着写" });
  });

  it("一边为空时不加分隔行", () => {
    expect(mergeDraftText("恢复的", "")).toBe("恢复的");
    expect(mergeDraftText("", joinQuoteCards([quote(2)], "草稿"))).toBe(joinQuoteCards([quote(2)], "草稿"));
    expect(mergeDraftText(joinQuoteCards([quote(1)], ""), "草稿")).toBe(joinQuoteCards([quote(1)], "草稿"));
  });
});
