import { EditorState } from "@codemirror/state";
import { describe, expect, it } from "vitest";

import { minimalChange } from "@/components/code-editor/minimal-change";

const apply = (state: EditorState, next: string) => {
  const changes = minimalChange(state, next);
  return changes ? state.update({ changes }).state.doc.toString() : state.doc.toString();
};

// 目的：外部改写内容时只替换变了的那段，换行按编辑器规则归一。
// 潜在缺陷：整篇替换让着色、选区与折叠全部重来；CRLF 文件被切在 \r\n 中间、凭空多出空行。
describe("minimal change", () => {
  const state = EditorState.create({ doc: "a\nb\nc\nd" });

  it("keeps the common head and tail", () => {
    expect(minimalChange(state, "a\nB\nc\nd")).toEqual({ from: 2, to: 3, insert: "B" });
    expect(minimalChange(state, "a\nb\nb\nc\nd")).toEqual({ from: 4, to: 4, insert: "b\n" });
    expect(apply(state, "")).toBe("");
    expect(apply(EditorState.create({ doc: "" }), "x\ny")).toBe("x\ny");
  });

  it("treats CRLF content as the same document", () => {
    const crlf = EditorState.create({ doc: "a\r\nb\r\nc" });
    expect(crlf.doc.lines).toBe(3);
    expect(minimalChange(crlf, "a\r\nb\r\nc")).toBeNull();
    expect(minimalChange(state, "a\r\nb\r\nc\r\nd")).toBeNull();
    expect(apply(crlf, "a\r\nB\r\nc")).toBe("a\nB\nc");
  });
});
