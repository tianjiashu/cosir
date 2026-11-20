import { renderToStaticMarkup } from "react-dom/server";
import { markEdits, pickRanges, tokenize } from "react-diff-view";
import { parseDiff } from "react-diff-view";
import { beforeAll, describe, expect, it } from "vitest";

import { FileDiff, maxLineNumber } from "@/components/conversation/tools/file-diff";
import { readFileChanges } from "@/lib/assistant/tools/file-changes";
import { diffSyntaxRanges, diffTokens } from "@/lib/syntax/diff-tokens";
import { initHighlighting, languageHighlighter } from "@/lib/syntax/highlighter";

// 着色引擎是 WASM，应用在 `src/main.tsx` 渲染前初始化，单测在这里做同一件事。
beforeAll(initHighlighting);

const PATCH = `diff --git a/a.ts b/a.ts
--- a/a.ts
+++ b/a.ts
@@ -10,3 +10,4 @@ class Store {
 const a = 1;
-let b = "x";
+/* one
+two */ let b = "y";
 done();
@@ -80,2 +81,2 @@ class Store {
-  remove() {}
+  remove(id) {}
 }
`;

const hunks = () => parseDiff(PATCH)[0].hunks;

// 目的：着色区间用文件里的真实行号与本行列号，块注释跨行时下一行接着按注释着色；颜色来自着色服务（GitHub 主题）。
// 潜在缺陷：用段内序号当行号、跨行状态断掉、与代码块不是同一套颜色。
describe("diff syntax ranges", () => {
  it("maps colored tokens to real line numbers on each side", async () => {
    const highlighter = (await languageHighlighter("typescript"))!;
    const [oldRanges, newRanges] = diffSyntaxRanges(hunks(), highlighter);
    const colorAt = (ranges: typeof oldRanges, lineNumber: number, start: number) =>
      ranges.find((range) => range.lineNumber === lineNumber && range.start === start)?.properties.style;

    expect(colorAt(oldRanges, 10, 0)).toMatchObject({ color: "#CF222E", "--shiki-dark": "#FF7B72" });
    expect(colorAt(oldRanges, 11, 8)?.color).toBe("#0A3069");
    expect(colorAt(newRanges, 11, 0)?.color).toBe("#6E7781");
    expect(colorAt(newRanges, 12, 0)?.color).toBe("#6E7781");
    expect(colorAt(newRanges, 13, 0)?.color).toBe("#8250DF");
    expect(newRanges.every((range) => range.length > 0)).toBe(true);
  });
});

// 目的：两段改动之间看得出跳过了多少行、在哪段代码里；潜在缺陷：两段贴在一起读成连续代码。
describe("file diff hunk gap", () => {
  it("separates hunks with the skipped line count and context", () => {
    const change = readFileChanges([{ path: "a.ts", status: "modified", patch: PATCH, insertions: 3, deletions: 2 }])[0];
    const html = renderToStaticMarkup(<FileDiff change={change} />);
    expect(html.match(/跳过/g)).toHaveLength(1);
    expect(html).toContain("⋯ 跳过 67 行");
    expect(html).toContain("class Store {");
  });
});

// 目的：diff 高亮的计算量跟着改动走、不跟着行号走，且结果与整份计算一致；潜在缺陷：改动在大文件末尾时展开卡顿。
describe("diff tokens", () => {
  it("matches whole-file tokenize, line by line", async () => {
    const highlighter = (await languageHighlighter("typescript"))!;
    const whole = tokenize(hunks(), { enhancers: [pickRanges(...diffSyntaxRanges(hunks(), highlighter)), markEdits(hunks(), { type: "block" })] });
    const byHunk = diffTokens(hunks(), highlighter);
    // 区间节点带着计算时的行号（按段算时是平移后的），渲染不用它，比对时去掉。
    const rendered = (value: unknown) => JSON.parse(JSON.stringify(value, (key, field) => (key === "lineNumber" ? undefined : field)));
    for (const side of ["old", "new"] as const) {
      whole[side].forEach((line, index) => {
        if (line.length && !(line.length === 1 && line[0].type === "text" && !line[0].value)) expect(rendered(byHunk[side][index]), `${side} ${index + 1}`).toEqual(rendered(line));
      });
    }
  });

  it("stays cheap for a change deep in a large file", () => {
    const deep = parseDiff(`diff --git a/a.ts b/a.ts\n--- a/a.ts\n+++ b/a.ts\n@@ -200000,3 +200000,3 @@\n a\n-foo(a);\n+bar(b);\n b\n`)[0].hunks;
    const started = performance.now();
    const tokens = diffTokens(deep, null);
    expect(performance.now() - started).toBeLessThan(50);
    expect(JSON.stringify(tokens.new[200000])).toContain("edit");
  });

  it("sizes the gutter by the last line number", () => {
    const ending99 = parseDiff(`diff --git a/a.ts b/a.ts\n--- a/a.ts\n+++ b/a.ts\n@@ -97,3 +97,3 @@\n a\n-x\n+y\n b\n`)[0].hunks;
    expect(maxLineNumber(ending99)).toBe(99);
  });
});
