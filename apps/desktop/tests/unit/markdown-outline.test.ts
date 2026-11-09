import { describe, expect, it } from "vitest";
import type { Element, Root } from "hast";

import { markdownOutline, rehypeSourceLines } from "@/lib/markdown-outline";

function element(tagName: string, start: number, end: number, children: Element["children"] = []): Element {
  return {
    type: "element",
    tagName,
    properties: {},
    position: { start: { line: start, column: 1, offset: 0 }, end: { line: end, column: 1, offset: 0 } },
    children,
  };
}

describe("rehypeSourceLines", () => {
  it("给块级元素写上起止源码行，行内元素不标", () => {
    const inline = element("span", 1, 1);
    const tree: Root = {
      type: "root",
      children: [
        element("h2", 1, 1, [inline]),
        element("p", 3, 5),
        element("pre", 7, 9),
        element("div", 11, 11),
      ],
    };
    rehypeSourceLines()(tree);
    const blocks = tree.children as Element[];
    expect(blocks[0].properties).toEqual({ dataSourceLine: 1, dataSourceLineEnd: 1 });
    expect(blocks[1].properties).toEqual({ dataSourceLine: 3, dataSourceLineEnd: 5 });
    expect(blocks[2].properties).toEqual({ dataSourceLine: 7, dataSourceLineEnd: 9 });
    // 不在块级白名单里的元素不加属性，避免选区定位误用容器行号。
    expect(blocks[3].properties).toEqual({});
    expect(inline.properties).toEqual({});
  });

  it("标题行为不变：大纲跳转按标题行号定位", () => {
    const text = "# 标题\n\n第一段\n\n## 二级标题";
    const outline = markdownOutline(text);
    expect(outline).toEqual([
      { depth: 1, text: "标题", line: 1 },
      { depth: 2, text: "二级标题", line: 5 },
    ]);
  });
});
