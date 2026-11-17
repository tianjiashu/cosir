import { describe, expect, it } from "vitest";

import { mermaidDocument } from "@/lib/workspace-files/mermaid-document";

// 潜在缺陷：.mmd 正文里出现 ``` 时提前闭合围栏，后半段被当成普通 Markdown 渲染。
describe("mermaidDocument", () => {
  it("包成一个 mermaid 代码块，围栏比正文里最长的反引号串多一个", () => {
    expect(mermaidDocument("graph TD\n  A-->B\n")).toBe("```mermaid\ngraph TD\n  A-->B\n```\n");
    expect(mermaidDocument("graph TD\n  A[\"```\"]-->B")).toBe("````mermaid\ngraph TD\n  A[\"```\"]-->B\n````\n");
  });
});
