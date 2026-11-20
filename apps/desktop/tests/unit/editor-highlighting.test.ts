import { Text } from "@codemirror/state";
import { beforeAll, describe, expect, it } from "vitest";

import { IncrementalHighlighting, compactLine } from "@/components/code-editor/editor-highlighting";
import { initHighlighting, languageHighlighter, type LanguageHighlighter } from "@/lib/syntax/highlighter";

// 着色引擎是 WASM，应用在 `src/main.tsx` 渲染前初始化，单测在这里做同一件事。
beforeAll(initHighlighting);

/** 整篇一次着色的逐行结果：分段着色必须与它一致。 */
function whole(highlighter: LanguageHighlighter, doc: Text) {
  return highlighter.chunkTokens(doc.toString()).lines.map(compactLine);
}

function highlighted(chunks: IncrementalHighlighting, doc: Text) {
  return Array.from({ length: doc.lines }, (_, index) => chunks.line(index + 1));
}

// 目的：分段接续着色与整篇一次着色逐行一致，编辑后从改动所在段作废重算也一致。
// 潜在缺陷：块注释跨段时下一段丢了语法状态、作废范围算错、文档变短后残留旧行。
describe("incremental editor highlighting", () => {
  // 块注释跨过第 200 行的检查点，中间夹着空行：逐行接续时语法状态不能断。
  const source = Array.from({ length: 900 }, (_, index) => (index === 195 ? "/* starts" : index === 200 ? "" : index === 205 ? "ends */ const x = 1;" : `const v${index} = "s";`));

  it("matches whole-document highlighting across checkpoints and after edits", async () => {
    const highlighter = (await languageHighlighter("typescript"))!;
    const chunks = new IncrementalHighlighting(highlighter);
    let doc = Text.of(source);
    while (chunks.highlightedLines < doc.lines) chunks.advance(doc, doc.lines, 1e9);
    expect(highlighted(chunks, doc)).toEqual(whole(highlighter, doc));

    // 在第 450 行开一个不闭合的块注释：退回第 400 行的检查点重算，之后全部变成注释色。
    doc = doc.replace(doc.line(450).from, doc.line(450).from, Text.of(["/* "]));
    chunks.invalidateFrom(450);
    expect(chunks.highlightedLines).toBe(400);
    chunks.advance(doc, doc.lines, 1e9);
    expect(highlighted(chunks, doc)).toEqual(whole(highlighter, doc));

    // 删到只剩 300 行：退回第 200 行的检查点，重算后与整篇一致。
    doc = doc.slice(0, doc.line(300).to);
    chunks.invalidateFrom(300);
    chunks.advance(doc, doc.lines, 1e9);
    expect(chunks.highlightedLines).toBe(300);
    expect(highlighted(chunks, doc)).toEqual(whole(highlighter, doc));
  });

  it("advances at least one line per call and stops at the target", async () => {
    const chunks = new IncrementalHighlighting((await languageHighlighter("typescript"))!);
    const doc = Text.of(source);
    chunks.advance(doc, 50, 0);
    expect(chunks.highlightedLines).toBe(1);
    chunks.advance(doc, 50, 1e9);
    chunks.advance(doc, 50, 1e9);
    expect(chunks.highlightedLines).toBe(50);
  });

  // 目的：一次推进最多超出预算一行，长行文件不卡界面。
  // 潜在缺陷：按 200 行一段算、段内不看预算（实测一段 49 秒）；JS 正则引擎处理 TS 长行每行约 0.3 秒。
  it("stays within the budget plus one line on long-line files", async () => {
    const chunks = new IncrementalHighlighting((await languageHighlighter("typescript"))!);
    const long = `const v = [${Array.from({ length: 900 }, (_, index) => `"s${index}"`).join(",")}];`.slice(0, 4000);
    const doc = Text.of(Array.from({ length: 400 }, () => long));
    chunks.advance(doc, 1, 0);
    const started = performance.now();
    chunks.advance(doc, 400, 12);
    expect(performance.now() - started).toBeLessThan(80);
    expect(chunks.highlightedLines).toBeGreaterThan(1);
  });
});
